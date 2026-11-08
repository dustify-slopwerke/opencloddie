"""HTTP listeners.

Two listeners share one process and one database:

* web  - internal only; nginx + Authelia add `Remote-User`.  Serves the UI,
         account management, and browser terminal WebSockets.
* api  - public, API-key authenticated; serves the Anthropic-compatible
         gateway, the CLI session API, and the agent WebSocket.

The agent connections live in this process, so the web listener can bridge a
browser terminal straight to a client's tmux-backed session.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import re
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .auth import ApiIdentity, AuthError, WebIdentity, api_identity, web_identity
from .config import APP_NAME
from .crypto import decrypt_secret, encrypt_secret
from .gateway import Gateway, GatewayError, error_body
from .models import context_tokens_for, model_ids, normalize_model
from .sessions import AgentHub, AgentUnavailable
from .util import (
    human_age,
    iso,
    json_dumps,
    mask_secret,
    redact_query,
)
from .websocket import OP_BINARY, OP_TEXT, WSClosed, WebSocket, WebSocketError, accept_key

log = logging.getLogger("opencloddie.http")
access_log = logging.getLogger("opencloddie.access")

VERSION = "0.1.0"
MAX_BODY = 32 * 1024 * 1024
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}
SAFE_STATIC = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")


class App:
    def __init__(self, cfg: dict[str, Any]):
        from .db import Database

        self.cfg = cfg
        self.db = Database(cfg["database"]["path"], cfg["secrets"]["master"])
        self.db.mark_all_sessions_offline()
        self.db.purge_offline_sessions(90)
        self.gateway = Gateway(cfg, self.db)
        self.hub = AgentHub(self)
        self.started_at = time.time()
        self.web_root = Path(__file__).resolve().parent / "web"
        self.client_script = Path(__file__).resolve().parent.parent / "bin" / "opencloddie"
        self.models = cfg["gateway"]["models"]
        self.model_ids = model_ids(self.models)
        self.default_model = cfg["gateway"]["default_model"]

    def public_urls(self, headers: Any) -> dict[str, str]:
        web = str(self.cfg["server"].get("public_web_url") or "").rstrip("/")
        api = str(self.cfg["server"].get("public_api_url") or "").rstrip("/")
        proto = headers.get("X-Forwarded-Proto") or "http"
        host = headers.get("Host") or "localhost"
        hostname = urlsplit(f"//{host}").hostname or "localhost"
        if self.cfg["server"].get("behind_proxy", False):
            base = f"{proto}://{host}"
            return {"web": web or base, "api": api or base}
        # Local/split-port mode: derive each listener's own origin.
        return {
            "web": web or f"http://{hostname}:{self.cfg['server']['web_port']}",
            "api": api or f"http://{hostname}:{self.cfg['server']['api_port']}",
        }

    def user_public(self, user: dict[str, Any]) -> dict[str, Any]:
        return {
            "username": user["username"],
            "name": user.get("name") or user["username"],
            "email": user.get("email") or "",
            "groups": json.loads(user.get("groups") or "[]"),
            "default_model": normalize_model(
                self.models, user.get("default_model"), self.default_model
            ),
        }


class OpenCloddieHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], app: App, mode: str):
        self.app = app
        self.mode = mode
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"
    sys_version = ""
    timeout = 300  # keep-alive idle timeout

    # ------------------------------------------------------------- plumbing
    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    @property
    def mode(self) -> str:
        return self.server.mode  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            message = redact_query(fmt % args)
        except Exception:  # noqa: BLE001
            message = str(fmt)
        access_log.info("%s %s", self.client_address[0], message)

    def handle_one_request(self) -> None:
        self._response_started = False
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            self.close_connection = True
            log.exception("unhandled error while serving request")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -------------------------------------------------------------- helpers
    def _path_query(self) -> tuple[str, str]:
        parts = urlsplit(self.path)
        return parts.path, parts.query

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def _read_body(self, limit: int = MAX_BODY) -> bytes:
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            return self._read_chunked(limit)
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AuthError(400, "invalid Content-Length", "invalid_request_error") from exc
        if length < 0 or length > limit:
            raise AuthError(413, "request body too large", "invalid_request_error")
        data = self.rfile.read(length)
        if len(data) != length:
            raise AuthError(400, "incomplete request body", "invalid_request_error")
        return data

    def _read_chunked(self, limit: int) -> bytes:
        data = bytearray()
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError as exc:
                raise AuthError(400, "invalid chunked encoding") from exc
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            if len(data) + size > limit:
                raise AuthError(413, "request body too large")
            data += self.rfile.read(size)
            self.rfile.read(2)
        return bytes(data)

    def _json_body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        raw = self._read_body(limit)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError(400, "request body must be valid JSON", "invalid_request_error") from exc
        if not isinstance(parsed, dict):
            raise AuthError(400, "request body must be a JSON object", "invalid_request_error")
        return parsed

    def _common_headers(self, *, html: bool = False, cache: str = "no-store") -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", cache)
        if html:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self' data:; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )

    def _send_json(
        self,
        status: int,
        obj: Any,
        *,
        headers: dict[str, str] | None = None,
        html: bool = False,
    ) -> None:
        body = json_dumps(obj)
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._common_headers(html=html)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_api_error(
        self, status: int, message: str, error_type: str = "api_error", headers: dict[str, str] | None = None
    ) -> None:
        self._send_json(status, {"ok": False, "error": {"message": message, "type": error_type}}, headers=headers)

    def _send_anthropic_error(
        self, status: int, message: str, error_type: str = "api_error", headers: dict[str, str] | None = None
    ) -> None:
        body = error_body(message, error_type)
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._common_headers()
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._common_headers(html=True)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -------------------------------------------------------------- routing
    def _dispatch(self, method: str) -> None:
        try:
            if self.mode == "api":
                self._dispatch_api(method)
            else:
                self._dispatch_web(method)
        except AuthError as exc:
            if getattr(self, "_response_started", False):
                self.close_connection = True
                return
            path, _ = self._path_query()
            if self.mode == "web" and path in ("", "/", "/index.html"):
                return self._send_html(
                    exc.status,
                    _auth_page(exc.message, self.app.cfg["web"].get("auth_portal_url") or ""),
                )
            if self._is_gateway_path():
                self._send_anthropic_error(exc.status, exc.message, exc.error_type)
            else:
                self._send_api_error(exc.status, exc.message, exc.error_type)
        except GatewayError as exc:
            if getattr(self, "_response_started", False):
                self.close_connection = True
                return
            self._send_anthropic_error(exc.status, exc.message, exc.error_type, headers=exc.headers)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.error("request failed: %s", traceback.format_exc())
            if getattr(self, "_response_started", False):
                self.close_connection = True
                return
            try:
                self._send_api_error(500, "internal server error")
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def _is_gateway_path(self) -> bool:
        path, _ = self._path_query()
        return path.startswith("/v1/") or path.startswith("/gateway/")

    # ------------------------------------------------------------- web mode
    def _dispatch_web(self, method: str) -> None:
        path, _ = self._path_query()

        if path == "/healthz":
            return self._send_json(200, {"ok": True, "version": VERSION, "mode": "web"})

        if path.startswith("/api/"):
            identity = web_identity(self, self.app)
            if method not in ("GET", "HEAD", "OPTIONS"):
                self._check_csrf()
            return self._route_web_api(method, path, identity)

        # The shell page requires authentication; static assets do not (nginx
        # already gates everything on this listener).
        if path in ("/", "/index.html"):
            web_identity(self, self.app)
            return self._serve_static("index.html", head=method == "HEAD")

        if path.startswith("/vendor/") or path in (
            "/app.js",
            "/style.css",
            "/favicon.svg",
            "/manifest.webmanifest",
        ):
            rel = path.lstrip("/")
            return self._serve_static(rel, head=method == "HEAD")

        if path == "/install/opencloddie":
            return self._serve_client_script()

        return self._send_api_error(404, "not found", "not_found")

    def _check_csrf(self) -> None:
        """State-changing web calls must come from our own UI.

        Authelia uses a cookie, so a cross-site request could otherwise carry a
        valid identity.  Requiring a custom header forces a CORS preflight that
        this server never approves, and Origin/Sec-Fetch checks catch the rest.
        """
        if (self.headers.get("X-OpenCloddie-Client") or "") != "web":
            raise AuthError(403, "missing X-OpenCloddie-Client header", "forbidden")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            raise AuthError(403, "cross-site request rejected", "forbidden")
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            if urlsplit(origin).hostname != urlsplit(f"//{host}").hostname:
                raise AuthError(403, "cross-origin request rejected", "forbidden")

    def _route_web_api(self, method: str, path: str, identity: WebIdentity) -> None:
        app = self.app
        user = app.db.user_by_id(identity.user_id) or identity.user

        if path == "/api/me" and method in ("GET", "HEAD"):
            urls = app.public_urls(self.headers)
            return self._send_json(
                200,
                {
                    "ok": True,
                    "user": app.user_public(user),
                    "opencode_key_set": bool(user.get("opencode_key_enc")),
                    "opencode_key_hint": user.get("opencode_key_hint") or "",
                    "models": app.models,
                    "default_model": normalize_model(
                        app.models, user.get("default_model"), app.default_model
                    ),
                    "urls": urls,
                    "install": {
                        "command": (
                            f"curl -fsSL {urls['api']}/install/opencloddie "
                            "-o ~/.local/bin/opencloddie && chmod +x ~/.local/bin/opencloddie"
                        ),
                        "login": (
                            f"opencloddie login --server {urls['api']} --api-key <your-key>"
                        ),
                    },
                },
            )

        if path == "/api/models" and method in ("GET", "HEAD"):
            return self._send_json(200, {"ok": True, "models": app.models})

        if path == "/api/stats" and method in ("GET", "HEAD"):
            stats = app.db.stats(identity.user_id)
            stats.update(app.hub.stats())
            return self._send_json(200, {"ok": True, "stats": stats})

        if path == "/api/system" and method in ("GET", "HEAD"):
            urls = app.public_urls(self.headers)
            return self._send_json(
                200,
                {
                    "ok": True,
                    "version": VERSION,
                    "uptime_seconds": int(time.time() - app.started_at),
                    "urls": urls,
                    "models": app.models,
                    "gateway": {
                        "upstream": app.cfg["gateway"]["upstream"],
                        "auth_style": app.cfg["gateway"]["auth_style"],
                    },
                    "hub": app.hub.stats(),
                },
            )

        if path == "/api/opencode-key":
            if method in ("PUT", "POST"):
                body = self._json_body()
                key = str(body.get("key") or "").strip()
                if not key:
                    raise AuthError(400, "OpenCode key is required", "invalid_request_error")
                if len(key) > 512:
                    raise AuthError(400, "OpenCode key is too long", "invalid_request_error")
                encrypted = encrypt_secret(key, app.cfg["secrets"]["master"])
                app.db.set_opencode_key(identity.user_id, encrypted, mask_secret(key, 4))
                return self._send_json(200, {"ok": True, "opencode_key_hint": mask_secret(key, 4)})
            if method == "DELETE":
                app.db.clear_opencode_key(identity.user_id)
                return self._send_json(200, {"ok": True})
            raise AuthError(405, "method not allowed")

        if path == "/api/opencode-key/test" and method == "POST":
            body = self._json_body()
            key = str(body.get("key") or "").strip()
            if not key:
                encrypted = app.db.user_secret(identity.user_id)
                if not encrypted:
                    raise AuthError(400, "no OpenCode key configured", "invalid_request_error")
                try:
                    key = decrypt_secret(encrypted, app.cfg["secrets"]["master"])
                except ValueError as exc:
                    raise AuthError(400, "stored key cannot be decrypted; re-save it") from exc
            model = normalize_model(
                app.models, user.get("default_model"), app.default_model
            )
            result = app.gateway.test_opencode_key(key, model)
            return self._send_json(200, {"ok": bool(result["ok"]), "result": result})

        if path == "/api/settings" and method in ("PUT", "POST"):
            body = self._json_body()
            model = normalize_model(app.models, body.get("default_model"), app.default_model)
            app.db.set_default_model(identity.user_id, model)
            return self._send_json(200, {"ok": True, "default_model": model})

        if path == "/api/keys":
            if method in ("GET", "HEAD"):
                keys = [self._public_key(k) for k in app.db.list_api_keys(identity.user_id)]
                return self._send_json(200, {"ok": True, "keys": keys})
            if method == "POST":
                body = self._json_body()
                name = str(body.get("name") or "CLI key").strip()[:80] or "CLI key"
                model = normalize_model(
                    app.models,
                    body.get("model"),
                    normalize_model(app.models, user.get("default_model"), app.default_model),
                )
                row, token = app.db.create_api_key(identity.user_id, name, model)
                data = self._public_key(row)
                data["token"] = token
                return self._send_json(
                    201,
                    {
                        "ok": True,
                        "key": data,
                        "notice": "Copy this key now; it is never shown again.",
                    },
                )
            raise AuthError(405, "method not allowed")

        match = re.fullmatch(r"/api/keys/([A-Za-z0-9_-]+)", path)
        if match:
            key_id = match.group(1)
            if method == "PATCH":
                body = self._json_body()
                name = body.get("name")
                model = body.get("model")
                if name is not None:
                    name = str(name).strip()[:80]
                if model is not None:
                    model = normalize_model(app.models, str(model), app.default_model)
                revoked = body.get("revoked")
                row = app.db.update_api_key(
                    identity.user_id,
                    key_id,
                    name=name,
                    model=model,
                    revoked=bool(revoked) if revoked is not None else None,
                )
                if not row:
                    raise AuthError(404, "key not found", "not_found")
                return self._send_json(200, {"ok": True, "key": self._public_key(row)})
            if method == "DELETE":
                if not app.db.delete_api_key(identity.user_id, key_id):
                    raise AuthError(404, "key not found", "not_found")
                return self._send_json(200, {"ok": True})
            raise AuthError(405, "method not allowed")

        if path == "/api/sessions" and method in ("GET", "HEAD"):
            sessions = [
                self._public_session(s)
                for s in app.db.list_sessions(identity.user_id)
            ]
            return self._send_json(200, {"ok": True, "sessions": sessions})

        match = re.fullmatch(r"/api/sessions/([A-Za-z0-9_-]+)", path)
        if match:
            session_id = match.group(1)
            session = app.db.get_session(session_id)
            if not session or session["user_id"] != identity.user_id:
                raise AuthError(404, "session not found", "not_found")
            if method == "DELETE":
                app.hub.close_session(session_id, kill=True)
                app.db.delete_session(session_id, identity.user_id)
                return self._send_json(200, {"ok": True})
            if method in ("GET", "HEAD"):
                return self._send_json(200, {"ok": True, "session": self._public_session(session)})
            raise AuthError(405, "method not allowed")

        match = re.fullmatch(r"/api/sessions/([A-Za-z0-9_-]+)/ws", path)
        if match:
            session_id = match.group(1)
            session = app.db.get_session(session_id)
            if not session or session["user_id"] != identity.user_id:
                raise AuthError(404, "session not found", "not_found")
            return self._web_terminal_ws(session)

        raise AuthError(404, "not found", "not_found")

    def _public_key(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "prefix": row["prefix"],
            "model": row["model"],
            "created_at": row["created_at"],
            "last_used_at": row.get("last_used_at"),
            "revoked": bool(row.get("revoked_at")),
            "revoked_at": row.get("revoked_at"),
        }

    def _public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        agent = self.app.hub.get_for_session(session["id"])
        return {
            "id": session["id"],
            "name": session["name"],
            "hostname": session.get("hostname") or "",
            "cwd": session.get("cwd") or "",
            "model": session.get("model") or "",
            # The hub is the source of truth; the DB flag is a fallback for
            # historical rows and is only used when no agent object exists.
            "online": agent is not None,
            "created_at": session.get("created_at"),
            "last_seen_at": session.get("last_seen_at"),
            "last_seen": human_age(session.get("last_seen_at")),
            "viewers": len(agent.channels) if agent else 0,
        }

    # -------------------------------------------------------------- ws: web
    def _check_ws_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        host = self.headers.get("Host") or ""
        if urlsplit(origin).hostname != urlsplit(f"//{host}").hostname:
            raise AuthError(403, "cross-origin websocket rejected", "forbidden")

    def _accept_websocket(self) -> WebSocket:
        key = self.headers.get("Sec-WebSocket-Key") or ""
        upgrade = (self.headers.get("Upgrade") or "").lower()
        connection = (self.headers.get("Connection") or "").lower()
        if upgrade != "websocket" or "upgrade" not in connection or not key:
            raise AuthError(400, "websocket upgrade required", "invalid_request_error")
        if (self.headers.get("Sec-WebSocket-Version") or "") != "13":
            raise AuthError(400, "unsupported websocket version", "invalid_request_error")
        self._response_started = True
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key(key).encode("ascii") + b"\r\n\r\n"
        )
        self.wfile.flush()
        self.close_connection = True
        # A terminal may be idle for a long time; don't inherit the HTTP
        # keep-alive timeout on the upgraded socket.
        self.connection.settimeout(None)
        return WebSocket(self.rfile, self.wfile, self.connection)

    def _web_terminal_ws(self, session: dict[str, Any]) -> None:
        self._check_ws_origin()
        ws = self._accept_websocket()
        session_id = session["id"]
        query = self._query()
        cols = _int_param(query, "cols", 120)
        rows = _int_param(query, "rows", 32)
        agent = self.app.hub.get_for_session(session_id)
        if agent is None:
            try:
                ws.send_text(json.dumps({"t": "error", "message": "session is offline"}))
                ws.close(1011, "session offline")
            except (OSError, WebSocketError):
                pass
            return
        try:
            channel = agent.open_channel(cols, rows)
        except AgentUnavailable as exc:
            try:
                ws.send_text(json.dumps({"t": "error", "message": str(exc)}))
                ws.close(1011, "agent unavailable")
            except (OSError, WebSocketError):
                pass
            return

        try:
            ws.send_text(
                json.dumps(
                    {
                        "t": "hello",
                        "session": self._public_session(self.app.db.get_session(session_id) or session),
                        "model": session.get("model"),
                    }
                )
            )
        except (OSError, WebSocketError):
            channel.close()
            return

        stop = threading.Event()

        def pump() -> None:
            while not stop.is_set():
                try:
                    kind, value = channel.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if stop.is_set():
                    break
                try:
                    if kind == "data":
                        ws.send_binary(value)
                    elif kind == "error":
                        ws.send_text(json.dumps({"t": "error", "message": value}))
                    elif kind == "exit":
                        ws.send_text(json.dumps({"t": "exit", "code": value}))
                        break
                except (OSError, WSClosed, WebSocketError):
                    break
            stop.set()
            try:
                ws.close()
            except (OSError, WebSocketError):
                pass

        pump_thread = threading.Thread(target=pump, name=f"term-{session_id}", daemon=True)
        pump_thread.start()

        try:
            while not stop.is_set():
                opcode, payload = ws.recv()
                if opcode == OP_BINARY:
                    channel.send_input(payload)
                elif opcode == OP_TEXT:
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    kind = message.get("t")
                    if kind == "resize":
                        channel.resize(message.get("cols"), message.get("rows"))
                    elif kind == "ping":
                        ws.send_text(json.dumps({"t": "pong"}))
        except (WSClosed, OSError, WebSocketError):
            pass
        finally:
            stop.set()
            channel.close()
            try:
                self.connection.close()
            except OSError:
                pass

    # ------------------------------------------------------------- api mode
    def _dispatch_api(self, method: str) -> None:
        path, _ = self._path_query()

        if path == "/healthz":
            return self._send_json(
                200, {"ok": True, "version": VERSION, "mode": "api", "uptime": int(time.time() - self.app.started_at)}
            )

        if path in ("/", ""):
            urls = self.app.public_urls(self.headers)
            return self._send_json(
                200,
                {
                    "ok": True,
                    "service": APP_NAME,
                    "version": VERSION,
                    "gateway": "/v1/messages",
                    "client_api": "/client/me",
                    "install": f"{urls['api']}/install/opencloddie",
                    "web": urls["web"],
                },
            )

        if path == "/install/opencloddie":
            return self._serve_client_script()

        if path == "/install":
            urls = self.app.public_urls(self.headers)
            return self._send_html(200, _install_page(urls["api"], urls["web"]))

        if path == "/agent/ws":
            identity = api_identity(self, self.app)
            return self._agent_ws(identity)

        if path.startswith("/client/"):
            identity = api_identity(self, self.app)
            return self._route_client_api(method, path, identity)

        if path.startswith("/gateway/v1/"):
            path = path[len("/gateway") :]

        if path == "/v1/models" and method in ("GET", "HEAD"):
            identity = api_identity(self, self.app)
            payload = self.app.gateway.models_response()
            payload["ok"] = True
            return self._send_json(200, payload)

        if method == "POST" and (path == "/v1/messages" or path == "/v1/messages/count_tokens"):
            identity = api_identity(self, self.app)
            body = self._read_body(int(self.app.cfg["gateway"].get("max_body_bytes") or MAX_BODY))
            _, query = self._path_query()
            result = self.app.gateway.proxy(identity, path, query, self.headers, body)
            return self._relay_upstream(result)

        raise AuthError(404, "not found", "not_found")

    def _route_client_api(self, method: str, path: str, identity: ApiIdentity) -> None:
        app = self.app
        if path == "/client/me" and method in ("GET", "HEAD"):
            user = app.db.user_by_id(identity.user_id) or {}
            urls = app.public_urls(self.headers)
            model = identity.model
            return self._send_json(
                200,
                {
                    "ok": True,
                    "user": {"username": identity.username, "name": user.get("name") or identity.username},
                    "key": {"id": identity.key_id, "name": identity.key_name, "model": model},
                    "model": model,
                    "context_tokens": context_tokens_for(app.models, model),
                    "models": app.models,
                    "opencode_key_set": bool(identity.opencode_key_enc),
                    "gateway_base_url": urls["api"],
                    "web_url": urls["web"],
                    "install": {
                        "command": (
                            f"curl -fsSL {urls['api']}/install/opencloddie "
                            "-o ~/.local/bin/opencloddie && chmod +x ~/.local/bin/opencloddie"
                        )
                    },
                },
            )

        if path == "/client/sessions" and method in ("GET", "HEAD"):
            sessions = [self._public_session(s) for s in app.db.list_sessions(identity.user_id)]
            return self._send_json(200, {"ok": True, "sessions": sessions})

        match = re.fullmatch(r"/client/sessions/([A-Za-z0-9_-]+)", path)
        if match:
            session_id = match.group(1)
            session = app.db.get_session(session_id)
            if not session or session["user_id"] != identity.user_id:
                raise AuthError(404, "session not found", "not_found")
            if method == "DELETE":
                app.hub.close_session(session_id, kill=True)
                app.db.delete_session(session_id, identity.user_id)
                return self._send_json(200, {"ok": True})
            if method in ("GET", "HEAD"):
                return self._send_json(200, {"ok": True, "session": self._public_session(session)})
            raise AuthError(405, "method not allowed")

        raise AuthError(404, "not found", "not_found")

    def _agent_ws(self, identity: ApiIdentity) -> None:
        ws = self._accept_websocket()
        try:
            self.connection.settimeout(20)
            opcode, payload = ws.recv()
            if opcode != OP_TEXT:
                raise ValueError("expected a JSON hello")
            hello = json.loads(payload.decode("utf-8"))
            if not isinstance(hello, dict):
                raise ValueError("hello must be an object")
            if not hello.get("agent_id"):
                q = self._query()
                hello["agent_id"] = (q.get("agent_id") or [""])[0]
            conn = self.app.hub.attach(ws, identity, hello)
            self.connection.settimeout(None)
            conn.send_json(
                {
                    "t": "welcome",
                    "server_time": iso(),
                    "session_id": conn.session_id,
                    "model": identity.model,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("agent handshake failed: %s", exc)
            try:
                ws.send_text(json.dumps({"t": "error", "message": f"handshake failed: {exc}"}))
                ws.close(1008, "handshake failed")
            except (OSError, WebSocketError):
                pass
            return
        try:
            while conn.alive:
                opcode, payload = ws.recv()
                if opcode != OP_TEXT:
                    continue
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    conn.dispatch(message)
        except (WSClosed, OSError, socket.timeout, TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("agent connection error")
        finally:
            conn.close(reason="agent disconnected")

    # ------------------------------------------------------------ streaming
    def _relay_upstream(self, result: Any) -> None:
        self._response_started = True
        if result.is_stream:
            self.send_response_only(result.status)
            for name, value in result.headers:
                self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self._common_headers()
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in result.iter_bytes():
                    if not chunk:
                        continue
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                result.close()
                self.close_connection = True
            return

        body = result.read_all()
        self.send_response(result.status)
        for name, value in result.headers:
            self.send_header(name, value)
        self._common_headers()
        self.send_header("Content-Length", str(len(body)))
        if result.status >= 400:
            self.send_header("X-OpenCloddie-Error", "upstream")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -------------------------------------------------------------- static
    def _serve_static(self, rel: str, *, head: bool = False) -> None:
        if not SAFE_STATIC.fullmatch("/" + rel.lstrip("/")):
            raise AuthError(404, "not found", "not_found")
        root = self.app.web_root.resolve()
        candidate = (root / unquote(rel)).resolve()
        if root not in candidate.parents and candidate != root:
            raise AuthError(404, "not found", "not_found")
        if not candidate.is_file():
            raise AuthError(404, "not found", "not_found")
        suffix = candidate.suffix.lower()
        content_type = STATIC_TYPES.get(suffix) or mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self._response_started = True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self._common_headers(
            html=suffix == ".html",
            cache="public, max-age=604800, immutable" if rel.startswith("vendor/") else "no-cache",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _serve_client_script(self) -> None:
        path = self.app.client_script
        if not path.is_file():
            raise AuthError(404, "client script not installed on this server", "not_found")
        data = path.read_bytes()
        self._response_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/x-python; charset=utf-8")
        self.send_header("Content-Disposition", 'inline; filename="opencloddie"')
        self._common_headers(cache="no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True


def _int_param(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        value = int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 500))


def _install_page(api_url: str, web_url: str) -> str:
    api = _html_escape(api_url)
    web = _html_escape(web_url)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install opencloddie</title>
<style>
body{{background:#0b0d10;color:#e6edf3;font:15px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;max-width:820px;margin:6vh auto;padding:0 20px}}
h1{{font-size:26px;letter-spacing:-.5px}} code,pre{{background:#141922;border:1px solid #232b36;border-radius:8px}}
code{{padding:2px 6px}} pre{{padding:16px;overflow:auto}} a{{color:#7ee787}}
.muted{{color:#8b949e}} .step{{margin:28px 0}}
</style></head><body>
<h1>opencloddie</h1>
<p class="muted">Ship a local Claude Code session to <a href="{web}">{web}</a>.</p>
<div class="step"><h3>1. Install the client</h3>
<pre>mkdir -p ~/.local/bin
curl -fsSL {api}/install/opencloddie -o ~/.local/bin/opencloddie
chmod +x ~/.local/bin/opencloddie</pre></div>
<div class="step"><h3>2. Generate an API key</h3>
<p>Open <a href="{web}">{web}</a>, sign in, add your OpenCode key, then create an API key.</p></div>
<div class="step"><h3>3. Sign in the client</h3>
<pre>opencloddie login --server {api} --api-key ocd_sk_...</pre></div>
<div class="step"><h3>4. Start a session</h3>
<pre>opencloddie                 # new session in the current directory
opencloddie --name work     # named session
# Ctrl-b d detaches; the session keeps running
opencloddie attach work     # re-attach locally
opencloddie ls              # list sessions from the API</pre></div>
<div class="step"><h3>Web UI</h3>
<p>Open <a href="{web}">{web}</a> to see every connected session in tabs.</p></div>
</body></html>"""


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def _auth_page(message: str, portal: str) -> str:
    portal_link = (
        f'<p><a href="{_html_escape(portal)}">Sign in</a></p>' if portal else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>opencloddie - authentication required</title>
<style>
body{{background:#0b0d10;color:#e6edf3;font:15px/1.6 ui-monospace,monospace;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{max-width:520px;padding:32px;border:1px solid #232b36;border-radius:14px;background:#12161d}}
h1{{font-size:20px;margin:0 0 12px}} p{{color:#9aa7b4}} code{{color:#7ee787}} a{{color:#7ee787}}
</style></head><body><div class="card">
<h1>Authentication required</h1>
<p>{_html_escape(message)}</p>
<p>Route this service through nginx + Authelia. The proxy must forward the
<code>Remote-User</code> header.</p>
{portal_link}
</div></body></html>"""


def make_server(app: App, mode: str) -> OpenCloddieHTTPServer:
    cfg = app.cfg["server"]
    if mode == "web":
        host, port = str(cfg["web_host"]), int(cfg["web_port"])
    elif mode == "api":
        host, port = str(cfg["api_host"]), int(cfg["api_port"])
    else:
        raise ValueError(f"unknown mode: {mode}")
    return OpenCloddieHTTPServer((host, port), app, mode)
