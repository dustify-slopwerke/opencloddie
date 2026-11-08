"""Minimal RFC 6455 WebSocket server and client helpers (stdlib only).

Only what opencloddie needs: text/binary messages, fragmentation, ping/pong and
close.  No extensions (permessage-deflate is never negotiated), no subprotocols.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from typing import Any
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 16 * 1024 * 1024


class WebSocketError(Exception):
    pass


class WSClosed(WebSocketError):
    def __init__(self, code: int = 1000, reason: str = ""):
        super().__init__(f"websocket closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _read_exact(rfile: Any, n: int) -> bytes:
    data = rfile.read(n)
    if data is None or len(data) < n:
        raise WSClosed(1006, "unexpected EOF")
    return data


def encode_frame(opcode: int, payload: bytes, *, mask: bool, fin: bool = True) -> bytes:
    length = len(payload)
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    header = bytearray([first])
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    header += key
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


class _FrameReader:
    def __init__(self, rfile: Any, *, require_mask: bool, max_message: int = MAX_MESSAGE):
        self.rfile = rfile
        self.require_mask = require_mask
        self.max_message = max_message

    def read_frame(self) -> tuple[bool, int, bytes]:
        head = _read_exact(self.rfile, 2)
        b1, b2 = head[0], head[1]
        fin = bool(b1 & 0x80)
        if b1 & 0x70:
            raise WebSocketError("reserved bits set (extensions are not supported)")
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        if self.require_mask and not masked:
            raise WebSocketError("client frame must be masked")
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", _read_exact(self.rfile, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(self.rfile, 8))[0]
        if length > self.max_message:
            raise WebSocketError("frame too large")
        mask_key = _read_exact(self.rfile, 4) if masked else b""
        payload = _read_exact(self.rfile, length) if length else b""
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # read_frame() is the only primitive callers need; recv() drives it.



def _read_data_message(conn: Any) -> tuple[int, bytes]:
    """Read one complete data message, handling interleaved control frames."""
    chunks: list[bytes] = []
    message_opcode: int | None = None
    total = 0
    while True:
        fin, opcode, payload = conn.reader.read_frame()
        if opcode == OP_CLOSE:
            code = 1000
            reason = ""
            if len(payload) >= 2:
                code = struct.unpack("!H", payload[:2])[0]
                reason = payload[2:].decode("utf-8", "replace")
            try:
                conn.close(code, "")
            except (OSError, WebSocketError):
                pass
            raise WSClosed(code, reason)
        if opcode == OP_PING:
            conn.send(OP_PONG, payload)
            continue
        if opcode == OP_PONG:
            continue
        if opcode == OP_CONT:
            if message_opcode is None:
                raise WebSocketError("unexpected continuation frame")
        else:
            if message_opcode is not None:
                raise WebSocketError("new data frame during fragmented message")
            if opcode not in (OP_TEXT, OP_BINARY):
                raise WebSocketError(f"unsupported opcode {opcode}")
            message_opcode = opcode
        chunks.append(payload)
        total += len(payload)
        if total > conn.reader.max_message:
            raise WebSocketError("message too large")
        if fin:
            return message_opcode or OP_TEXT, b"".join(chunks)


class WebSocket:
    """Server-side connection (unmasked outgoing frames)."""
    def __init__(self, rfile: Any, wfile: Any, sock: socket.socket | None = None):
        self.rfile = rfile
        self.wfile = wfile
        self.sock = sock
        self.reader = _FrameReader(rfile, require_mask=True)
        self._send_lock = threading.Lock()
        self._closed = False

    def send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._closed:
                raise WSClosed()
            self.wfile.write(encode_frame(opcode, payload, mask=False))
            self.wfile.flush()

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self.send(OP_BINARY, data)

    def recv(self) -> tuple[int, bytes]:
        return _read_data_message(self)

    def close(self, code: int = 1000, reason: str = "") -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
            try:
                self.wfile.write(encode_frame(OP_CLOSE, payload, mask=False))
                self.wfile.flush()
            except OSError:
                pass
            try:
                if self.sock:
                    self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class WebSocketClient:
    """Blocking client used by the opencloddie agent."""

    def __init__(self, sock: socket.socket, rfile: Any, wfile: Any):
        self.sock = sock
        self.rfile = rfile
        self.wfile = wfile
        self.reader = _FrameReader(rfile, require_mask=False)
        self._send_lock = threading.Lock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = 30.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> "WebSocketClient":
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("ws", "wss", "http", "https"):
            raise WebSocketError(f"unsupported websocket URL scheme: {scheme}")
        secure = scheme in ("wss", "https")
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl_context or ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: opencloddie-agent/0.1",
        ]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        raw.settimeout(timeout)
        raw.sendall(request.encode("latin-1"))
        rfile = raw.makefile("rb")
        status_line = rfile.readline()
        if not status_line:
            raise WebSocketError("empty handshake response")
        status_parts = status_line.decode("latin-1").strip().split(None, 2)
        if len(status_parts) < 2 or status_parts[1] != "101":
            body = rfile.read(512).decode("utf-8", "replace")
            raw.close()
            raise WebSocketError(f"websocket handshake failed: {status_line.decode('latin-1').strip()} {body}")
        response_headers: dict[str, str] = {}
        while True:
            line = rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            response_headers[name.strip().lower()] = value.strip()
        if response_headers.get("sec-websocket-accept") != accept_key(key):
            raw.close()
            raise WebSocketError("invalid Sec-WebSocket-Accept")
        raw.settimeout(None if timeout is None else timeout)
        wfile = raw.makefile("wb")
        return cls(raw, rfile, wfile)

    def send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._closed:
                raise WSClosed()
            self.wfile.write(encode_frame(opcode, payload, mask=True))
            self.wfile.flush()

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self.send(OP_BINARY, data)

    def recv(self) -> tuple[int, bytes]:
        return _read_data_message(self)

    def close(self, code: int = 1000, reason: str = "") -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
            try:
                self.wfile.write(encode_frame(OP_CLOSE, payload, mask=True))
                self.wfile.flush()
            except OSError:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
