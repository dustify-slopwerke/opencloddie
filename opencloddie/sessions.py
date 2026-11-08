"""Agent hub: live connections from `opencloddie` clients and terminal channels.

A client agent holds one tmux-backed Claude session and opens a channel for
every viewer (the web UI).  Each channel is an independent `tmux attach` PTY on
the client, so viewers get proper redraws and resizing.
"""

from __future__ import annotations

import base64
import itertools
import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .auth import ApiIdentity
from .util import iso, utcnow

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class AgentUnavailable(Exception):
    pass


class Channel:
    def __init__(self, channel_id: int, agent: "AgentConnection"):
        self.id = channel_id
        self.agent = agent
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2048)
        self.opened = threading.Event()
        self._closed = False
        self._lock = threading.Lock()

    def put(self, kind: str, value: Any) -> None:
        try:
            self.queue.put_nowait((kind, value))
        except queue.Full:
            # A viewer that cannot keep up is disconnected rather than growing
            # memory without bound.
            try:
                self.queue.put_nowait(("exit", None))
            except queue.Full:
                pass

    def send_input(self, data: bytes) -> None:
        self.agent.send_json({"t": "in", "ch": self.id, "data": base64.b64encode(data).decode("ascii")})

    def resize(self, cols: int, rows: int) -> None:
        self.agent.send_json({"t": "resize", "ch": self.id, "cols": int(cols), "rows": int(rows)})

    def request_buffer(self) -> None:
        self.agent.send_json({"t": "get_buffer", "ch": self.id})

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.agent.close_channel(self)


@dataclass
class AgentConnection:
    hub: "AgentHub"
    ws: Any
    identity: ApiIdentity
    agent_id: str
    session_id: str = ""
    hostname: str = ""
    last_seen: float = field(default_factory=time.time)
    channels: dict[int, Channel] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    alive: bool = True
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    def send_json(self, obj: dict[str, Any]) -> bool:
        try:
            self.ws.send_text(json.dumps(obj, separators=(",", ":")))
            return True
        except Exception:  # noqa: BLE001 - connection is gone
            self.close()
            return False

    def open_channel(self, cols: int, rows: int) -> Channel:
        with self.lock:
            if not self.alive:
                raise AgentUnavailable("session agent is offline")
            channel = Channel(next(self._counter), self)
            self.channels[channel.id] = channel
        if not self.send_json({"t": "open", "ch": channel.id, "cols": int(cols), "rows": int(rows)}):
            self.close_channel(channel)
            raise AgentUnavailable("failed to reach the session agent")
        return channel

    def close_channel(self, channel: Channel) -> None:
        with self.lock:
            self.channels.pop(channel.id, None)
        self.send_json({"t": "close", "ch": channel.id})

    def dispatch(self, message: dict[str, Any]) -> None:
        self.last_seen = time.time()
        kind = message.get("t")
        if kind == "ping":
            self.send_json({"t": "pong", "ts": message.get("ts")})
            if self.session_id:
                self.hub.app.db.touch_session(self.session_id)
            return
        if kind == "out":
            channel = self.channels.get(_as_int(message.get("ch")))
            if channel:
                try:
                    data = base64.b64decode(message.get("data", ""), validate=False)
                except Exception:  # noqa: BLE001
                    return
                channel.put("data", data)
            return
        if kind == "opened":
            channel = self.channels.get(_as_int(message.get("ch")))
            if channel:
                channel.opened.set()
            return
        if kind == "exit":
            channel = self.channels.get(_as_int(message.get("ch")))
            if channel:
                channel.put("exit", message.get("code"))
            return
        if kind == "error":
            channel = self.channels.get(_as_int(message.get("ch")))
            if channel:
                channel.put("error", str(message.get("message", "terminal error")))
            return
        if kind == "buffer":
            channel = self.channels.get(_as_int(message.get("ch")))
            if channel:
                channel.put(
                    "buffer",
                    {
                        "data": message.get("data", ""),
                        "created": message.get("created", 0),
                        "size": message.get("size", 0),
                        "name": message.get("name", ""),
                        "error": message.get("error", ""),
                    },
                )
            return
        if kind == "meta":
            self._update_meta(message)
            return
        if kind == "bye":
            self.close(reason="agent said goodbye")

    def _update_meta(self, message: dict[str, Any]) -> None:
        if not self.session_id:
            return
        name = _clean_text(message.get("name"), 80)
        cwd = _clean_text(message.get("cwd"), 300)
        hostname = _clean_text(message.get("hostname"), 120)
        if not name and not cwd and not hostname:
            return
        try:
            self.hub.app.db.upsert_session(
                session_id=self.session_id,
                user_id=self.identity.user_id,
                api_key_id=self.identity.key_id,
                name=name or "session",
                hostname=hostname or self.hostname,
                cwd=cwd,
                model=self.identity.model,
                agent_id=self.agent_id,
                metadata={"version": _clean_text(message.get("version"), 40)},
            )
            self.hostname = hostname or self.hostname
        except Exception:  # noqa: BLE001 - metadata is best-effort
            pass

    def close(self, reason: str = "") -> None:
        with self.lock:
            if not self.alive:
                return
            self.alive = False
            channels = list(self.channels.values())
            self.channels.clear()
        for channel in channels:
            channel.put("exit", None)
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        self.hub.detach(self, reason=reason)


class AgentHub:
    def __init__(self, app: Any, heartbeat_timeout: float = 120.0):
        self.app = app
        self.heartbeat_timeout = heartbeat_timeout
        self.lock = threading.RLock()
        self.agents: dict[str, AgentConnection] = {}
        self.by_session: dict[str, str] = {}

    def attach(self, ws: Any, identity: ApiIdentity, hello: dict[str, Any]) -> AgentConnection:
        agent_id = _clean_text(hello.get("agent_id"), 64) or ""
        if not AGENT_ID_RE.fullmatch(agent_id):
            from .util import new_short_id

            agent_id = new_short_id("a").replace("_", "-")
        session = hello.get("session") or {}
        session_id = _clean_text(session.get("id"), 64) or ""
        if not SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("invalid session id")
        name = _clean_text(session.get("name"), 80) or "session"
        hostname = _clean_text(hello.get("hostname"), 120)
        cwd = _clean_text(session.get("cwd"), 300)
        metadata = {
            "version": _clean_text(hello.get("version"), 40),
            "platform": _clean_text(hello.get("platform"), 80),
            "agent": agent_id,
        }

        # One live connection per agent id.  Close a stale duplicate first.
        with self.lock:
            old = self.agents.get(agent_id)
        if old is not None and old is not ws:
            old.close(reason="replaced by a new agent connection")

        conn = AgentConnection(self, ws, identity, agent_id)
        conn.session_id = session_id
        conn.hostname = hostname

        try:
            self.app.db.upsert_session(
                session_id=session_id,
                user_id=identity.user_id,
                api_key_id=identity.key_id,
                name=name,
                hostname=hostname,
                cwd=cwd,
                model=identity.model,
                agent_id=agent_id,
                metadata=metadata,
            )
        except PermissionError as exc:
            raise ValueError("session id is already registered by another user") from exc

        with self.lock:
            previous = self.by_session.get(session_id)
            self.agents[agent_id] = conn
            self.by_session[session_id] = agent_id
            old_for_session = self.agents.get(previous) if previous and previous != agent_id else None
        if old_for_session is not None and old_for_session is not conn:
            old_for_session.close(reason="session was re-registered by a new agent")
        return conn

    def detach(self, conn: AgentConnection, reason: str = "") -> None:
        with self.lock:
            if self.agents.get(conn.agent_id) is conn:
                self.agents.pop(conn.agent_id, None)
            if conn.session_id and self.by_session.get(conn.session_id) == conn.agent_id:
                self.by_session.pop(conn.session_id, None)
        if conn.session_id:
            self.app.db.mark_agent_offline(conn.agent_id)

    def get_for_session(self, session_id: str) -> AgentConnection | None:
        with self.lock:
            agent_id = self.by_session.get(session_id)
            conn = self.agents.get(agent_id) if agent_id else None
        if conn and conn.alive:
            return conn
        return None

    def is_online(self, session_id: str) -> bool:
        return self.get_for_session(session_id) is not None

    def close_session(self, session_id: str, *, kill: bool = False) -> bool:
        conn = self.get_for_session(session_id)
        if not conn:
            return False
        for channel in list(conn.channels.values()):
            channel.put("exit", None)
        if kill:
            # Let the agent kill its tmux session and close the socket itself;
            # closing immediately can race with delivery of the kill message.
            conn.send_json({"t": "kill"})
            timer = threading.Timer(5.0, conn.close, kwargs={"reason": "kill timeout"})
            timer.daemon = True
            timer.start()
            return True
        conn.close(reason="session closed by the API")
        return True

    def watchdog(self) -> int:
        now = time.time()
        closed = 0
        with self.lock:
            agents = list(self.agents.values())
        for conn in agents:
            if now - conn.last_seen > self.heartbeat_timeout:
                conn.close(reason="heartbeat timeout")
                closed += 1
        return closed

    def stats(self) -> dict[str, int]:
        with self.lock:
            return {"agents": len(self.agents), "sessions": len(self.by_session)}

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            out = []
            for conn in self.agents.values():
                out.append(
                    {
                        "agent_id": conn.agent_id,
                        "session_id": conn.session_id,
                        "user": conn.identity.username,
                        "hostname": conn.hostname,
                        "channels": len(conn.channels),
                        "last_seen": iso(utcnow()),
                    }
                )
            return out


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").strip()
    return text[:limit]
