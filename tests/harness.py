"""Shared test harness: in-process servers, fake OpenCode upstream, helpers."""

from __future__ import annotations

import email.message
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opencloddie.config import build_default_config  # noqa: E402
from opencloddie.crypto import encrypt_secret  # noqa: E402
from opencloddie.httpd import App, make_server  # noqa: E402
from opencloddie.models import DEFAULT_MODELS  # noqa: E402
from opencloddie.util import iso  # noqa: E402

WEB_HEADERS = {
    "Remote-User": "alice",
    "Remote-Email": "alice@example.com",
    "Remote-Name": "Alice Example",
    "Remote-Groups": "users,god",
}


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FakeUpstream:
    """A tiny OpenCode-Go-shaped upstream that records what it receives."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.mode = "json"
        self.fail_first_status: int | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if outer.fail_first_status is not None:
                    status = outer.fail_first_status
                    outer.fail_first_status = None
                    payload = json.dumps(
                        {"type": "error", "error": {"type": "api_error", "message": "planned failure"}}
                    ).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                try:
                    parsed = json.loads(body.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    parsed = {}
                model = parsed.get("model", "")
                if parsed.get("stream") or outer.mode == "sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    events = [
                        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_test","model":"%s","usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
                        % model,
                        'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n',
                        'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":1}}\n\n',
                        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                    ]
                    for event in events:
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                        time.sleep(0.01)
                    self.close_connection = True
                    return

                payload = json.dumps(
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [{"type": "text", "text": "hello"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    }
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-request-id", "upstream-request-1")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/inference/go/anthropic"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestServer:
    def __init__(self, upstream_url: str | None = None, *, models: list[dict] | None = None):
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-test-")
        cfg = build_default_config(dev=True)
        cfg["server"].update(
            {
                "web_host": "127.0.0.1",
                "web_port": 0,
                "api_host": "127.0.0.1",
                "api_port": 0,
                "behind_proxy": False,
                "public_web_url": "",
                "public_api_url": "",
            }
        )
        if upstream_url:
            cfg["gateway"]["upstream"] = upstream_url
        if models:
            cfg["gateway"]["models"] = models
        cfg["database"]["path"] = os.path.join(self.tmp, "opencloddie.db")
        cfg["_state_dir"] = self.tmp
        cfg["_config_path"] = os.path.join(self.tmp, "config.json")
        self.cfg = cfg
        self.app = App(cfg)
        self.web = make_server(self.app, "web")
        self.api = make_server(self.app, "api")
        self.web_port = self.web.server_address[1]
        self.api_port = self.api.server_address[1]
        cfg["server"]["web_port"] = self.web_port
        cfg["server"]["api_port"] = self.api_port
        cfg["server"]["public_web_url"] = f"http://127.0.0.1:{self.web_port}"
        cfg["server"]["public_api_url"] = f"http://127.0.0.1:{self.api_port}"
        self.threads = [
            threading.Thread(target=self.web.serve_forever, daemon=True),
            threading.Thread(target=self.api.serve_forever, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    # ---------------------------------------------------------------- users
    def create_user_key(
        self,
        username: str = "alice",
        *,
        model: str = "deepseek-v4.1-flash",
        opencode_key: str | None = "oc_sk_test_key",
    ) -> tuple[str, dict]:
        user = self.app.db.ensure_user(username, default_model=model)
        if opencode_key is not None:
            self.app.db.set_opencode_key(
                user["id"],
                encrypt_secret(opencode_key, self.cfg["secrets"]["master"]),
                "…" + opencode_key[-4:],
            )
        row, token = self.app.db.create_api_key(user["id"], "test key", model)
        return token, row

    # ------------------------------------------------------------- requests
    def request(
        self,
        port: int,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
        payload = None
        hdrs = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        data = response.read()
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        status = response.status
        conn.close()
        return status, response_headers, data

    def web_request(self, method: str, path: str, *, web: bool = True, **kwargs):
        headers = dict(WEB_HEADERS if web else {})
        headers.update(kwargs.pop("headers", {}) or {})
        return self.request(self.web_port, method, path, headers=headers, **kwargs)

    def api_request(self, method: str, path: str, token: str, **kwargs):
        headers = {"Authorization": f"Bearer {token}"}
        headers.update(kwargs.pop("headers", {}) or {})
        return self.request(self.api_port, method, path, headers=headers, **kwargs)

    def web_json(self, method: str, path: str, **kwargs):
        status, headers, raw = self.web_request(method, path, **kwargs)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return status, headers, data

    def api_json(self, method: str, path: str, token: str, **kwargs):
        status, headers, raw = self.api_request(method, path, token, **kwargs)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return status, headers, data

    # --------------------------------------------------------------- cleanup
    def stop(self) -> None:
        for server in (self.web, self.api):
            server.shutdown()
            server.server_close()
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def make_headers(mapping: dict[str, str]) -> email.message.Message:
    message = email.message.Message()
    for key, value in mapping.items():
        message.add_header(key, value)
    return message


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def tmux_has(name: str) -> bool:
    import subprocess

    return (
        subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def now_iso() -> str:
    return iso()
