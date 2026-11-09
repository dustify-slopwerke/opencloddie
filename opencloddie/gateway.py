"""Anthropic-compatible gateway to OpenCode Go.

The public API listener speaks `/v1/messages`.  For every request we:
  * authenticate the opencloddie API key,
  * look up that key's owner and the owner's encrypted OpenCode key,
  * force the model selected in the web UI,
  * preserve Claude Code's session header and mirror it to x-opencode-session,
  * stream the upstream SSE response back without buffering.
"""

from __future__ import annotations

import collections
import gzip
import hashlib
import http.client
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .auth import ApiIdentity
from .crypto import decrypt_secret
from .metrics import SSEUsageParser, usage_from_json
from .models import find_model, model_ids
from .util import json_dumps

log = logging.getLogger("opencloddie.gateway")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_DROP = HOP_BY_HOP | {
    "host",
    "content-length",
    "authorization",
    "x-api-key",
    "x-opencloddie-key",
    "api-key",
    "cookie",
    "accept-encoding",
    "expect",
}
RESPONSE_DROP = HOP_BY_HOP | {"content-length", "content-encoding", "set-cookie"}
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        error_type: str = "invalid_request_error",
        *,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type
        self.headers = headers or {}


def error_body(message: str, error_type: str = "invalid_request_error") -> bytes:
    return json_dumps({"type": "error", "error": {"type": error_type, "message": message}})


@dataclass
class ProxyResult:
    status: int
    headers: list[tuple[str, str]]
    body: bytes | None = None
    stream: Iterator[bytes] | None = None
    model: str = ""
    request_id: str = ""
    metric_token: Any = field(default=None, repr=False)
    metrics: Any = field(default=None, repr=False)
    usage_parser: SSEUsageParser | None = field(default=None, repr=False)
    bytes_out: int = 0
    upstream_ms: float = 0.0
    _metrics_done: bool = field(default=False, repr=False)
    _close: Callable[[], None] = field(default=lambda: None, repr=False)

    @property
    def is_stream(self) -> bool:
        return self.stream is not None

    def finish_metrics(self) -> None:
        """Record this request exactly once, after any body/stream completes."""
        if self._metrics_done or self.metrics is None or self.metric_token is None:
            return
        self._metrics_done = True
        usage: dict[str, int] = {}
        if self.usage_parser is not None:
            usage = self.usage_parser.usage()
        if not usage and self.body is not None:
            usage = usage_from_json(self.body)
        if self.body is not None:
            self.bytes_out = max(self.bytes_out, len(self.body))
        self.metrics.finish(
            self.metric_token,
            self.status,
            bytes_out=self.bytes_out,
            upstream_ms=self.upstream_ms,
            **usage,
        )

    def iter_bytes(self) -> Iterator[bytes]:
        if self.stream is None:
            try:
                if self.body:
                    yield self.body
            finally:
                self.finish_metrics()
            return
        try:
            for chunk in self.stream:
                self.bytes_out += len(chunk)
                if self.usage_parser is not None:
                    self.usage_parser.feed(chunk)
                yield chunk
        finally:
            self.finish_metrics()
            self.close()

    def read_all(self, limit: int = 64 * 1024 * 1024) -> bytes:
        if self.body is not None:
            self.bytes_out = max(self.bytes_out, len(self.body))
            self.finish_metrics()
            return self.body[:limit]
        chunks: list[bytes] = []
        size = 0
        try:
            for chunk in self.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise GatewayError(502, "upstream response too large", "api_error")
                chunks.append(chunk)
        finally:
            self.finish_metrics()
        return b"".join(chunks)

    def close(self) -> None:
        self.finish_metrics()
        try:
            self._close()
        except Exception:  # noqa: BLE001
            pass


def _first_header(headers: Any, name: str) -> str:
    value = headers.get(name)
    if value:
        return str(value)
    return ""


def _forward_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    seen: set[str] = set()
    for name in headers.keys():
        lname = name.lower()
        if lname in seen:
            continue
        seen.add(lname)
        if lname in REQUEST_DROP or lname.startswith("x-opencloddie-"):
            continue
        values = headers.get_all(name, []) or []
        out[name] = ", ".join(str(v) for v in values)
    return out


class Gateway:
    def __init__(self, cfg: dict[str, Any], db: Any, metrics: Any = None):
        self.cfg = cfg
        self.db = db
        if metrics is None:
            from .metrics import Metrics

            metrics = Metrics(started_at=time.time())
        self.metrics = metrics
        self.gateway_cfg = cfg["gateway"]
        self.models = cfg["gateway"]["models"]
        self.model_ids = set(model_ids(self.models))
        self._rate_lock = threading.Lock()
        self._rate: dict[str, collections.deque[float]] = {}
        parsed = urlsplit(self.gateway_cfg["upstream"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("gateway.upstream must be an http(s) URL")
        self.upstream_scheme = parsed.scheme
        self.upstream_host = parsed.hostname
        self.upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.upstream_base = parsed.path.rstrip("/")

    # ------------------------------------------------------------- routing
    def resolve_model(self, identity: ApiIdentity, requested: str) -> str:
        if self.gateway_cfg.get("allow_client_model_override") and requested in self.model_ids:
            return requested
        if identity.model in self.model_ids:
            return identity.model
        default = self.gateway_cfg.get("default_model") or self.models[0]["id"]
        return default if find_model(self.models, default) else self.models[0]["id"]

    def rate_limit(self, identity: ApiIdentity) -> None:
        limit = int(self.gateway_cfg.get("rate_limit_per_minute") or 0)
        if limit <= 0:
            return
        now = time.time()
        with self._rate_lock:
            bucket = self._rate.setdefault(identity.key_id, collections.deque())
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= limit:
                raise GatewayError(
                    429,
                    f"rate limit exceeded ({limit} requests/minute for this API key)",
                    "rate_limit_error",
                    headers={"Retry-After": "60"},
                )
            bucket.append(now)

    def rate_snapshot(self, key_ids: set[str] | None = None) -> dict[str, dict[str, int]]:
        """Read-only view of the sliding-window rate limit for the given keys."""
        limit = int(self.gateway_cfg.get("rate_limit_per_minute") or 0)
        now = time.time()
        out: dict[str, dict[str, int]] = {}
        with self._rate_lock:
            ids = list(self._rate.keys()) if key_ids is None else list(key_ids)
            for key_id in ids:
                bucket = self._rate.get(key_id)
                if bucket is not None:
                    while bucket and now - bucket[0] > 60:
                        bucket.popleft()
                used = len(bucket) if bucket is not None else 0
                resets = 0
                if bucket:
                    resets = int(max(0.0, 60.0 - (now - bucket[0])))
                out[key_id] = {
                    "used": used,
                    "limit": max(0, limit),
                    "remaining": max(0, limit - used) if limit > 0 else 0,
                    "reset_in_seconds": resets,
                }
        return out

    def opencode_key(self, identity: ApiIdentity) -> str:
        encrypted = identity.opencode_key_enc or self.db.user_secret(identity.user_id)
        if not encrypted:
            raise GatewayError(
                400,
                "no OpenCode API key is configured for this account; add one in the "
                "opencloddie web UI (Settings → OpenCode key)",
                "invalid_request_error",
            )
        try:
            key = decrypt_secret(encrypted, self.cfg["secrets"]["master"])
        except ValueError as exc:
            raise GatewayError(
                500,
                "the stored OpenCode key cannot be decrypted; re-save it in the web UI",
                "api_error",
            ) from exc
        if not key:
            raise GatewayError(400, "OpenCode API key is empty; re-save it in the web UI")
        return key

    # --------------------------------------------------------------- proxy
    def proxy(
        self,
        identity: ApiIdentity,
        suffix: str,
        query: str,
        request_headers: Any,
        body: bytes,
    ) -> ProxyResult:
        token = self.metrics.begin(
            user_id=identity.user_id,
            key_id=identity.key_id,
            key_name=identity.key_name,
            username=identity.username,
            bytes_in=len(body),
        )
        try:
            self.rate_limit(identity)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GatewayError(400, "request body must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise GatewayError(400, "request body must be a JSON object")

            requested = str(payload.get("model") or "")
            model = self.resolve_model(identity, requested)
            self.metrics.set_model(token, model)
            payload["model"] = model
            new_body = json_dumps(payload)

            opencode_key = self.opencode_key(identity)
            headers = _forward_headers(request_headers)
            headers["Accept-Encoding"] = "identity"
            headers.setdefault(
                "User-Agent", str(self.gateway_cfg.get("user_agent") or "opencloddie")
            )

            session_id = (
                _first_header(request_headers, "X-Claude-Code-Session-Id")
                or _first_header(request_headers, "x-opencode-session")
            )
            if not session_id:
                # Stable per API key so prompt caching has an affinity key.
                session_id = "opencloddie-" + hashlib.sha256(
                    f"{identity.user_id}:{identity.key_id}".encode()
                ).hexdigest()[:32]
            headers["x-opencode-session"] = session_id

            if self.gateway_cfg.get("auth_style") == "x-api-key":
                headers.pop("Authorization", None)
                headers["x-api-key"] = opencode_key
            else:
                headers.pop("x-api-key", None)
                headers["Authorization"] = f"Bearer {opencode_key}"

            path = self.upstream_base + suffix
            if query:
                path += "?" + query

            request_id = "req_" + secrets.token_hex(8)
            started = time.perf_counter()
            result = self._request_once(headers, new_body, path, model, request_id)
            upstream_ms = (time.perf_counter() - started) * 1000.0

            fallback = str(self.gateway_cfg.get("fallback_model") or "")
            if (
                result.status in RETRYABLE_STATUS
                and fallback
                and fallback != model
                and find_model(self.models, fallback)
            ):
                log.warning(
                    "upstream %s for model %s; retrying once with fallback %s",
                    result.status,
                    model,
                    fallback,
                )
                result.close()
                retry_payload = dict(payload)
                retry_payload["model"] = fallback
                started = time.perf_counter()
                result = self._request_once(
                    headers, json_dumps(retry_payload), path, fallback, request_id
                )
                upstream_ms = (time.perf_counter() - started) * 1000.0
                model = fallback
                self.metrics.set_model(token, model)

            result.model = model
            result.request_id = request_id
            result.metrics = self.metrics
            result.metric_token = token
            result.upstream_ms = upstream_ms
            log.info(
                "gateway user=%s key=%s model=%s status=%s stream=%s request_id=%s",
                identity.username,
                identity.key_name,
                model,
                result.status,
                result.is_stream,
                request_id,
            )
            return result
        except GatewayError as exc:
            self.metrics.finish(token, exc.status, rate_limited=exc.status == 429)
            raise
        except Exception:
            self.metrics.finish(token, 500)
            raise

    def _request_once(
        self,
        headers: dict[str, str],
        body: bytes,
        path: str,
        model: str,
        request_id: str,
    ) -> ProxyResult:
        cls = http.client.HTTPSConnection if self.upstream_scheme == "https" else http.client.HTTPConnection
        conn = cls(
            self.upstream_host,
            self.upstream_port,
            timeout=float(self.gateway_cfg.get("connect_timeout") or 30),
        )
        try:
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(float(self.gateway_cfg.get("read_timeout") or 900))
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001 - normalize network failures
            conn.close()
            raise GatewayError(
                502,
                f"could not reach OpenCode upstream ({type(exc).__name__}: {exc})",
                "api_error",
            ) from exc

        response_headers: list[tuple[str, str]] = []
        for name, value in resp.getheaders():
            if name.lower() in RESPONSE_DROP or name.lower().startswith("x-opencloddie-"):
                continue
            response_headers.append((name, value))
        response_headers.append(("x-opencloddie-model", model))
        response_headers.append(("x-opencloddie-request-id", request_id))

        content_type = (resp.getheader("content-type") or "").lower()
        content_encoding = (resp.getheader("content-encoding") or "").lower()
        stream = "text/event-stream" in content_type

        def close() -> None:
            try:
                resp.close()
            finally:
                conn.close()

        if not stream:
            try:
                data = resp.read()
            except Exception as exc:  # noqa: BLE001
                close()
                raise GatewayError(502, f"upstream read failed: {exc}", "api_error") from exc
            finally:
                close()
            if content_encoding == "gzip":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    pass
            return ProxyResult(
                status=resp.status,
                headers=response_headers,
                body=data,
                model=model,
                request_id=request_id,
                _close=lambda: None,
            )

        def iterator() -> Iterator[bytes]:
            decompressor = None
            if content_encoding == "gzip":
                import zlib

                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    if decompressor is not None:
                        chunk = decompressor.decompress(chunk)
                        if not chunk:
                            continue
                    yield chunk
                if decompressor is not None:
                    tail = decompressor.flush()
                    if tail:
                        yield tail
            except (OSError, http.client.HTTPException) as exc:
                log.info("upstream stream ended: %s", exc)
            finally:
                close()

        return ProxyResult(
            status=resp.status,
            headers=response_headers,
            stream=iterator(),
            model=model,
            request_id=request_id,
            usage_parser=SSEUsageParser(),
            _close=close,
        )

    # --------------------------------------------------------------- models
    def models_response(self) -> dict[str, Any]:
        data = []
        for m in self.models:
            data.append(
                {
                    "type": "model",
                    "id": m["id"],
                    "display_name": m["label"],
                    "created_at": "2026-01-01T00:00:00Z",
                }
            )
        return {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None}

    # ------------------------------------------------------------ key test
    def test_opencode_key(self, key: str, model: str) -> dict[str, Any]:
        """Validate a key with a minimal upstream request.  Returns status info."""
        body = json_dumps(
            {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        )
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "Accept-Encoding": "identity",
            "User-Agent": str(self.gateway_cfg.get("user_agent") or "opencloddie"),
            "x-opencode-session": "opencloddie-keytest-" + secrets.token_hex(8),
        }
        if self.gateway_cfg.get("auth_style") == "x-api-key":
            headers["x-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
        path = self.upstream_base + "/v1/messages"
        conn = None
        try:
            conn = (
                http.client.HTTPSConnection
                if self.upstream_scheme == "https"
                else http.client.HTTPConnection
            )(self.upstream_host, self.upstream_port, timeout=60)
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(60)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(2048)
            text = raw.decode("utf-8", "replace")
            ok = resp.status == 200
            # 429/5xx mean the key authenticated but upstream is busy.
            authenticated = resp.status not in (401, 403)
            detail = text
            try:
                parsed = json.loads(text)
                detail = parsed.get("error", {}).get("message") or text
            except json.JSONDecodeError:
                pass
            return {
                "ok": ok or (authenticated and resp.status in (429, 500, 502, 503, 504, 529)),
                "status": resp.status,
                "message": detail[:500],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": 0, "message": f"{type(exc).__name__}: {exc}"}
        finally:
            if conn is not None:
                conn.close()
