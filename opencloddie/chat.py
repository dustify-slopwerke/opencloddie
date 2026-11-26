"""Structured chat sessions: durable events, approvals, viewers and agent relay.

The client host runs `claude` in headless stream-json mode inside tmux.  Its
`__chat` runner speaks to the existing `__agent` process over a Unix socket;
the agent forwards `chat_*` frames to this broker, which owns the durable
event sequence, permission state and browser fan-out.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import queue
import threading
import uuid
from typing import Any

from .util import iso, utcnow

log = logging.getLogger("opencloddie.chat")

# Image media types accepted on input.  Keep this in sync with the protocol.
IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _event_id(value: Any) -> str:
    return str(value or uuid.uuid4().hex)


class ChatError(Exception):
    def __init__(self, status: int, message: str, code: str = "chat_error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class ChatBroker:
    def __init__(self, app: Any):
        self.app = app
        cfg = app.cfg.get("chat") or {}
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.persist = bool(cfg.get("persist", True))
        self.max_event_bytes = int(cfg.get("max_event_bytes") or 256 * 1024)
        self.max_tool_result_bytes = int(
            cfg.get("max_tool_result_bytes") or 64 * 1024
        )
        self.max_events_per_session = int(
            cfg.get("max_events_per_session") or 5000
        )
        self.max_image_bytes = int(cfg.get("max_image_bytes") or 5 * 1024 * 1024)
        self.max_input_images = int(
            cfg.get("max_input_images", 4)
            if cfg.get("max_input_images") is not None
            else 4
        )
        self.max_inline_event_image_bytes = int(
            cfg.get("max_inline_event_image_bytes") or 1 * 1024 * 1024
        )
        self.permission_timeout = int(cfg.get("permission_timeout_seconds") or 300)
        self._lock = threading.RLock()
        self._viewers: dict[str, dict[int, queue.Queue]] = {}
        self._viewer_ids = itertools.count(1)
        self._timers: dict[str, threading.Timer] = {}

    # ------------------------------------------------------------------ state
    def _state_row(self, session_id: str) -> dict[str, Any]:
        row = self.app.db.chat_state_get(session_id) or {}
        defaults = {
            "mode": "tui",
            "status": "offline",
            "run_id": "",
            "claude_session_id": "",
            "model": "",
            "permission_mode": "default",
            "turn_id": "",
            "last_seq": 0,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "context_tokens": None,
            "pending_count": 0,
            "capabilities": "{}",
        }
        return {**defaults, **row}

    def public_state(self, session_id: str, user_id: int) -> dict[str, Any]:
        row = self._state_row(session_id)
        return {
            "mode": row.get("mode") or "tui",
            "status": row.get("status") or "offline",
            "run_id": row.get("run_id") or "",
            "claude_session_id": row.get("claude_session_id") or "",
            "model": row.get("model") or "",
            "permission_mode": row.get("permission_mode") or "default",
            "turn_id": row.get("turn_id") or "",
            "last_seq": int(row.get("last_seq") or 0),
            "cost_usd": float(row.get("cost_usd") or 0.0),
            "tokens_in": int(row.get("tokens_in") or 0),
            "tokens_out": int(row.get("tokens_out") or 0),
            "context_tokens": row.get("context_tokens"),
            "pending_count": int(row.get("pending_count") or 0),
            "capabilities": _json(row.get("capabilities"), {}),
            "unread": self.app.db.chat_unread(user_id, session_id),
        }

    def public_permission(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "turn_id": row.get("turn_id") or "",
            "tool_name": row.get("tool_name") or "",
            "input": _json(row.get("tool_input"), {}),
            "suggestions": _json(row.get("suggestions"), []),
            "status": row.get("status") or "pending",
            "created_at": row.get("created_at"),
            "resolved_at": row.get("resolved_at"),
            "delivered_at": row.get("delivered_at"),
            "expires_at": row.get("expires_at"),
            "decision": _json(row.get("decision"), None),
        }

    def public_event(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "seq": int(row.get("seq") or 0),
            "session_id": row.get("session_id"),
            "event_id": row.get("event_id"),
            "turn_id": row.get("turn_id") or "",
            "kind": row.get("kind"),
            "payload": _json(row.get("payload"), {}),
            "created_at": row.get("created_at"),
        }

    def snapshot(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self.app.db.chat_events_after(session_id, after_seq, limit)
            pending = self.app.db.chat_permissions_pending(session_id)
            state = self._state_row(session_id)
        return {
            "events": [self.public_event(row) for row in rows],
            "pending": [self.public_permission(row) for row in pending],
            "state": state,
        }

    # ---------------------------------------------------------------- viewers
    def attach_viewer(self, session_id: str) -> tuple[int, queue.Queue]:
        viewer_id = next(self._viewer_ids)
        q: queue.Queue = queue.Queue(maxsize=1024)
        with self._lock:
            self._viewers.setdefault(session_id, {})[viewer_id] = q
        return viewer_id, q

    def attach_with_snapshot(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> tuple[int, queue.Queue, dict[str, Any]]:
        """Subscribe and read the backlog atomically, so no event is missed."""
        with self._lock:
            viewer_id = next(self._viewer_ids)
            q: queue.Queue = queue.Queue(maxsize=1024)
            self._viewers.setdefault(session_id, {})[viewer_id] = q
            rows = self.app.db.chat_events_after(session_id, after_seq, limit)
            pending = self.app.db.chat_permissions_pending(session_id)
            state = self._state_row(session_id)
        return (
            viewer_id,
            q,
            {
                "events": [self.public_event(row) for row in rows],
                "pending": [self.public_permission(row) for row in pending],
                "state": state,
            },
        )

    def detach_viewer(self, session_id: str, viewer_id: int) -> None:
        with self._lock:
            viewers = self._viewers.get(session_id)
            if viewers is not None:
                viewers.pop(viewer_id, None)
                if not viewers:
                    self._viewers.pop(session_id, None)

    def _broadcast(self, session_id: str, frame: dict[str, Any]) -> None:
        with self._lock:
            viewers = list((self._viewers.get(session_id) or {}).values())
        for q in viewers:
            try:
                q.put_nowait(frame)
            except queue.Full:
                # A viewer that cannot keep up can resync from the next hello.
                pass

    # ------------------------------------------------------------- agent relay
    def _agent(self, session_id: str) -> Any:
        agent = self.app.hub.get_for_session(session_id)
        if agent is None:
            raise ChatError(409, "session is offline; start it on its host", "offline")
        if not agent.features.get("chat"):
            raise ChatError(
                409,
                "this session was started by an older opencloddie client; "
                "restart it with the current client to enable chat",
                "unsupported",
            )
        return agent

    def _send_agent(self, session_id: str, frame: dict[str, Any]) -> None:
        agent = self._agent(session_id)
        if not agent.send_json(frame):
            raise ChatError(409, "session agent is unavailable", "offline")

    def on_agent_frame(self, agent: Any, message: dict[str, Any]) -> None:
        kind = message.get("t")
        session_id = agent.session_id
        if not session_id:
            return
        try:
            if kind == "chat_hello":
                self._on_hello(session_id, message)
            elif kind == "chat_event":
                self._on_event(session_id, message)
            elif kind == "chat_delta":
                self._broadcast(
                    session_id,
                    {
                        "t": "delta",
                        "turn_id": message.get("turn_id") or "",
                        "parent_tool_use_id": message.get("parent_tool_use_id"),
                        "message_id": message.get("message_id") or "",
                        "block_index": message.get("block_index"),
                        "delta": message.get("delta"),
                    },
                )
            elif kind == "chat_state":
                self._on_state(session_id, message)
            elif kind == "chat_permission_request":
                self._on_permission_request(session_id, message)
            elif kind == "chat_permission_resolved":
                self._on_permission_resolved(session_id, message)
            elif kind == "chat_mode":
                self._on_mode(session_id, message)
            elif kind in ("chat_exit", "chat_error"):
                self._on_status(session_id, message)
        except Exception:  # noqa: BLE001 - never kill the agent reader thread
            log.exception("chat frame failed (session=%s kind=%s)", session_id, kind)

    def _on_hello(self, session_id: str, message: dict[str, Any]) -> None:
        self.app.db.chat_state_upsert(
            session_id,
            run_id=str(message.get("run_id") or ""),
            claude_session_id=str(message.get("claude_session_id") or ""),
            model=str(message.get("model") or ""),
            permission_mode=str(message.get("permission_mode") or "default"),
            status=str(message.get("status") or "idle"),
            capabilities=message.get("capabilities") or {},
        )
        for pending in message.get("pending") or []:
            if not isinstance(pending, dict) or not (
                pending.get("id") or pending.get("permission_id")
            ):
                continue
            self._create_permission(session_id, pending)
        # A decision made while the agent was offline must reach the runner.
        for row in self.app.db.chat_permissions_undelivered(session_id):
            try:
                self._send_agent(
                    session_id,
                    {
                        "t": "chat_permission_result",
                        "permission_id": row["id"],
                        "result": _json(row.get("decision"), {}),
                    },
                )
                self.app.db.chat_permission_mark_delivered(row["id"])
            except ChatError:
                break
        self._broadcast(
            session_id,
            {"t": "state", "chat": self.public_state(session_id, self._owner(session_id))},
        )

    def _append_event(
        self,
        session_id: str,
        event_id: str,
        kind: str,
        payload: Any,
        turn_id: str = "",
    ) -> tuple[int, bool]:
        if not self.persist:
            return 0, True
        seq, inserted = self.app.db.append_chat_event(
            session_id,
            self._owner(session_id),
            event_id,
            kind,
            payload,
            turn_id=turn_id,
        )
        if inserted and seq and seq % 100 == 0:
            if (
                self.app.db.chat_event_count(session_id)
                > self.max_events_per_session
            ):
                self.app.db.trim_chat_events(
                    session_id, self.max_events_per_session
                )
        return seq, inserted

    def _on_event(self, session_id: str, message: dict[str, Any]) -> None:
        event_id = _event_id(message.get("event_id"))
        kind = str(message.get("kind") or "event")
        payload = message.get("payload")
        if kind in ("user_message", "tool_result"):
            payload = self._normalize_images(payload)
        if kind == "tool_result":
            payload = self._cap_tool_result(payload)
        if kind in ("user_message", "tool_result"):
            payload = self._fit_event_bytes(payload)
        seq, inserted = self._append_event(
            session_id,
            event_id,
            kind,
            payload or {},
            str(message.get("turn_id") or ""),
        )
        if not inserted:
            return
        if kind == "checkpoint" and isinstance(payload, dict):
            checkpoint_uuid = str(payload.get("uuid") or "").strip()
            if checkpoint_uuid:
                self.app.db.chat_checkpoint_add(session_id, checkpoint_uuid, seq)
        row = {
            "seq": seq,
            "session_id": session_id,
            "event_id": event_id,
            "turn_id": message.get("turn_id") or "",
            "kind": kind,
            "payload": payload,
            "created_at": message.get("created_at") or "",
        }
        self._broadcast(session_id, {"t": "event", **self.public_event(row)})

    @staticmethod
    def _decoded_image_bytes(data: Any) -> int | None:
        if not isinstance(data, str) or not data:
            return None
        try:
            return len(base64.b64decode(data, validate=True))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _block_media_type(block: dict[str, Any]) -> str:
        source = block.get("source")
        if isinstance(source, dict) and source.get("media_type"):
            return str(source["media_type"])
        return str(block.get("media_type") or "")

    def _normalize_images(self, payload: Any) -> Any:
        """Replace oversized inline images with placeholders.

        ``user_message`` / ``tool_result`` content may be a list of Anthropic
        content blocks.  An image up to ``max_inline_event_image_bytes`` stays
        inline (the browser renders it as a ``data:`` URL); anything larger is
        reduced to ``{type:image, placeholder:true, media_type, bytes}`` so a
        single large upload cannot blow up the transcript.
        """
        if not isinstance(payload, dict):
            return payload
        content = payload.get("content")
        if not isinstance(content, list):
            return payload
        limit = int(
            self.cfg.get(
                "max_inline_event_image_bytes", self.max_inline_event_image_bytes
            )
        )
        normalized: list[Any] = []
        changed = False
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "image"):
                normalized.append(block)
                continue
            if block.get("placeholder"):
                normalized.append(block)
                continue
            source = block.get("source")
            data = source.get("data") if isinstance(source, dict) else None
            decoded = self._decoded_image_bytes(data)
            if decoded is None:
                # Not a base64 image we understand; leave it untouched.
                normalized.append(block)
                continue
            if decoded <= limit:
                normalized.append(block)
                continue
            normalized.append(
                {
                    "type": "image",
                    "placeholder": True,
                    "media_type": self._block_media_type(block),
                    "bytes": decoded,
                }
            )
            changed = True
        if not changed:
            return payload
        return {**payload, "content": normalized}

    def _fit_event_bytes(self, payload: Any) -> Any:
        """Drop remaining inline images if the event would exceed its cap."""
        if not isinstance(payload, dict):
            return payload
        try:
            size = len(
                json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
            )
        except (TypeError, ValueError):
            return payload
        if size <= self.max_event_bytes:
            return payload
        content = payload.get("content")
        if not isinstance(content, list):
            return payload
        fitted: list[Any] = []
        changed = False
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "image"):
                fitted.append(block)
                continue
            if block.get("placeholder"):
                fitted.append(block)
                continue
            source = block.get("source")
            data = source.get("data") if isinstance(source, dict) else None
            decoded = self._decoded_image_bytes(data)
            if decoded is None:
                fitted.append(block)
                continue
            fitted.append(
                {
                    "type": "image",
                    "placeholder": True,
                    "media_type": self._block_media_type(block),
                    "bytes": decoded,
                }
            )
            changed = True
        if not changed:
            return payload
        return {**payload, "content": fitted}

    def _cap_tool_result(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        content = payload.get("content")
        text = json.dumps(content, separators=(",", ":")) if content is not None else ""
        if len(text.encode("utf-8")) <= self.max_tool_result_bytes:
            return payload
        clipped = text[: self.max_tool_result_bytes]
        return {
            **payload,
            "content": clipped,
            "truncated": True,
            "original_bytes": len(text.encode("utf-8")),
        }

    def _on_state(self, session_id: str, message: dict[str, Any]) -> None:
        usage = message.get("usage") or {}
        fields: dict[str, Any] = {}
        for key in ("status", "turn_id", "model", "permission_mode", "claude_session_id"):
            if message.get(key) is not None:
                fields[key] = message[key]
        if message.get("cost_usd") is not None:
            fields["cost_usd"] = float(message["cost_usd"])
        if isinstance(usage, dict):
            if usage.get("input_tokens") is not None:
                fields["tokens_in"] = int(usage["input_tokens"])
            if usage.get("output_tokens") is not None:
                fields["tokens_out"] = int(usage["output_tokens"])
        if message.get("context_tokens") is not None:
            fields["context_tokens"] = int(message["context_tokens"])
        if message.get("capabilities") is not None:
            fields["capabilities"] = message["capabilities"]
        self.app.db.chat_state_upsert(session_id, **fields)
        self._broadcast(
            session_id,
            {"t": "state", "chat": self.public_state(session_id, self._owner(session_id))},
        )

    def _on_permission_request(self, session_id: str, message: dict[str, Any]) -> None:
        row = self._create_permission(session_id, message)
        if row is None:
            return
        self._broadcast(session_id, {"t": "permission", "permission": row})

    def _create_permission(
        self, session_id: str, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        permission_id = str(message.get("permission_id") or message.get("id") or "")
        if not permission_id:
            return None
        existing = self.app.db.chat_permission_get(permission_id)
        if existing is not None:
            return self.public_permission(existing)
        from datetime import timedelta

        expires_at = message.get("expires_at") or iso(
            utcnow() + timedelta(seconds=self.permission_timeout)
        )
        row = self.app.db.chat_permission_create(
            {
                "id": permission_id,
                "session_id": session_id,
                "turn_id": str(message.get("turn_id") or ""),
                "request_id": str(message.get("request_id") or ""),
                "tool_name": str(message.get("tool_name") or ""),
                "tool_input": json.dumps(message.get("input") or {}),
                "suggestions": json.dumps(message.get("suggestions") or []),
                "status": "pending",
                "created_at": iso(),
                "expires_at": expires_at,
            }
        )
        seq, inserted = self._append_event(
            session_id,
            f"perm:{permission_id}:request",
            "permission",
            {
                "permission_id": permission_id,
                "tool_name": row.get("tool_name"),
                "input": _json(row.get("tool_input"), {}),
                "suggestions": _json(row.get("suggestions"), []),
                "status": "pending",
            },
            row.get("turn_id") or "",
        )
        if inserted:
            self._broadcast(
                session_id,
                {
                    "t": "event",
                    **self.public_event(
                        {
                            "seq": seq,
                            "session_id": session_id,
                            "event_id": f"perm:{permission_id}:request",
                            "turn_id": row.get("turn_id") or "",
                            "kind": "permission",
                            "payload": {
                                "permission_id": permission_id,
                                "tool_name": row.get("tool_name"),
                                "input": _json(row.get("tool_input"), {}),
                                "suggestions": _json(row.get("suggestions"), []),
                                "status": "pending",
                            },
                            "created_at": row.get("created_at") or "",
                        }
                    ),
                },
            )
        timer = threading.Timer(self.permission_timeout, self._expire, args=(permission_id,))
        timer.daemon = True
        with self._lock:
            self._timers[permission_id] = timer
        timer.start()
        return self.public_permission(row)

    def _on_permission_resolved(
        self, session_id: str, message: dict[str, Any]
    ) -> None:
        permission_id = str(message.get("permission_id") or "")
        if not permission_id:
            return
        decision = _json(message.get("decision"), {})
        status = "allowed" if decision.get("behavior") == "allow" else "denied"
        resolved = self.app.db.chat_permission_resolve(
            permission_id,
            status=status,
            decision=json.dumps(decision),
            resolved_by=str(message.get("resolved_by") or "runner"),
        )
        if resolved is None:
            return
        self._finish_permission(resolved)

    def _finish_permission(self, row: dict[str, Any]) -> None:
        with self._lock:
            timer = self._timers.pop(row["id"], None)
        if timer is not None:
            timer.cancel()
        self._broadcast(
            row["session_id"],
            {
                "t": "permission_resolved",
                "id": row["id"],
                "status": row["status"],
                "decision": _json(row.get("decision"), None),
            },
        )
        self._append_event(
            row["session_id"],
            f"perm:{row['id']}:resolved",
            "permission_resolved",
            {
                "permission_id": row["id"],
                "status": row["status"],
                "decision": _json(row.get("decision"), None),
            },
            row.get("turn_id") or "",
        )

    def _expire(self, permission_id: str) -> None:
        row = self.app.db.chat_permission_get(permission_id)
        if row is None or row.get("status") != "pending":
            return
        resolved = self.app.db.chat_permission_expire(permission_id)
        if resolved is None:
            return
        try:
            self._send_agent(
                row["session_id"],
                {
                    "t": "chat_permission_result",
                    "permission_id": permission_id,
                    "result": _json(resolved.get("decision"), {}),
                },
            )
        except ChatError:
            pass
        self._finish_permission(resolved)

    def _on_mode(self, session_id: str, message: dict[str, Any]) -> None:
        mode = str(message.get("mode") or "")
        status = str(message.get("status") or "idle")
        fields: dict[str, Any] = {"status": status}
        if mode:
            fields["mode"] = mode
        if mode == "tui":
            self._expire_pending(session_id)
        self.app.db.chat_state_upsert(session_id, **fields)
        self._broadcast(
            session_id,
            {
                "t": "mode",
                "mode": mode,
                "status": status,
                "message": message.get("message") or "",
            },
        )
        self._broadcast(
            session_id,
            {"t": "state", "chat": self.public_state(session_id, self._owner(session_id))},
        )

    def _expire_pending(self, session_id: str) -> None:
        """A stopped runner or a mode switch cannot answer pending approvals."""
        for row in self.app.db.chat_permissions_pending(session_id):
            resolved = self.app.db.chat_permission_expire(row["id"])
            if resolved is None:
                continue
            try:
                self._send_agent(
                    session_id,
                    {
                        "t": "chat_permission_result",
                        "permission_id": row["id"],
                        "result": _json(resolved.get("decision"), {}),
                    },
                )
            except ChatError:
                pass
            self._finish_permission(resolved)

    def _on_status(self, session_id: str, message: dict[str, Any]) -> None:
        status = "error" if message.get("t") == "chat_error" else "offline"
        if message.get("t") == "chat_exit":
            self._expire_pending(session_id)
        self.app.db.chat_state_upsert(session_id, status=status)
        self._broadcast(
            session_id,
            {
                "t": "state",
                "chat": self.public_state(session_id, self._owner(session_id)),
            },
        )
        if message.get("message"):
            self._broadcast(
                session_id,
                {"t": "error", "message": message["message"], "code": message.get("t")},
            )

    # --------------------------------------------------------------- commands
    def _owner(self, session_id: str) -> int:
        row = self.app.db.get_session(session_id)
        return int(row["user_id"]) if row else 0

    def _check_owner(self, session_id: str, user_id: int) -> dict[str, Any]:
        row = self.app.db.get_session(session_id)
        if not row or int(row["user_id"]) != int(user_id):
            raise ChatError(404, "session not found", "not_found")
        return row

    def _validate_input(self, content: Any) -> None:
        """Validate text/image content and raise ChatError on a bad request.

        Images are checked by decoded size, never by JSON-dumping the base64
        into the text budget.
        """
        max_text = int(
            self.cfg.get("max_input_text_bytes") or 256 * 1024
        )
        max_images = int(
            self.cfg.get(
                "max_input_images",
                self.max_input_images,
            )
        )
        max_image_bytes = int(
            self.cfg.get("max_image_bytes") or self.max_image_bytes
        )
        if isinstance(content, str):
            if len(content.encode("utf-8")) > max_text:
                raise ChatError(413, "message is too large", "too_large")
            return
        if not isinstance(content, list):
            raise ChatError(400, "content must be text or content blocks", "invalid")
        text_bytes = 0
        image_count = 0
        image_bytes = 0
        for block in content:
            if not isinstance(block, dict):
                raise ChatError(400, "content blocks must be objects", "invalid")
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ChatError(
                        400, "text content blocks need a string 'text'", "invalid"
                    )
                text_bytes += len(text.encode("utf-8"))
                continue
            if block_type != "image":
                raise ChatError(
                    400, f"unsupported content block type: {block_type!r}", "invalid"
                )
            image_count += 1
            if image_count > max_images:
                raise ChatError(
                    413,
                    f"at most {max_images} image(s) per message",
                    "too_many_images",
                )
            source = block.get("source")
            if not isinstance(source, dict):
                raise ChatError(400, "image blocks need a source object", "invalid")
            if source.get("type") != "base64":
                raise ChatError(
                    400, "only base64 image sources are supported", "invalid"
                )
            if source.get("media_type") not in IMAGE_MEDIA_TYPES:
                raise ChatError(400, "unsupported image media type", "invalid")
            data = source.get("data")
            if not isinstance(data, str) or not data:
                raise ChatError(400, "image data is required", "invalid")
            # Cheap upper-bound check before allocating a decode buffer.
            if max_image_bytes and len(data) > ((max_image_bytes + 2) // 3) * 4 + 4:
                raise ChatError(413, "image is too large", "too_large")
            try:
                decoded = base64.b64decode(data, validate=True)
            except (TypeError, ValueError) as exc:
                raise ChatError(
                    400, "image data is not valid base64", "invalid"
                ) from exc
            if len(decoded) > max_image_bytes:
                raise ChatError(413, "image is too large", "too_large")
            image_bytes += len(decoded)
        if text_bytes > max_text:
            raise ChatError(413, "message text is too large", "too_large")
        # Total decoded budget: the text cap plus the aggregate image allowance.
        if text_bytes + image_bytes > max_text + max_image_bytes * max_images:
            raise ChatError(413, "message is too large", "too_large")

    def send_input(
        self, session_id: str, user_id: int, content: Any, client_msg_id: str
    ) -> dict[str, Any]:
        self._check_owner(session_id, user_id)
        self._validate_input(content)
        client_msg_id = str(client_msg_id or uuid.uuid4().hex)
        # Fail before consuming the idempotency key if the session cannot chat.
        self._agent(session_id)
        if not self.app.db.claim_chat_input(session_id, client_msg_id):
            return {"ok": True, "client_msg_id": client_msg_id, "duplicate": True}
        try:
            self._send_agent(
                session_id,
                {
                    "t": "chat_input",
                    "request_id": uuid.uuid4().hex,
                    "client_msg_id": client_msg_id,
                    "content": content,
                },
            )
        except ChatError:
            # Do not consume the key when delivery failed; a retry must work.
            self.app.db.delete_chat_input(session_id, client_msg_id)
            raise
        self._broadcast(
            session_id,
            {"t": "input", "client_msg_id": client_msg_id, "content": content},
        )
        self.app.db.chat_state_upsert(session_id, status="thinking")
        return {"ok": True, "client_msg_id": client_msg_id}

    def interrupt(self, session_id: str, user_id: int) -> None:
        self._check_owner(session_id, user_id)
        self._send_agent(session_id, {"t": "chat_interrupt", "request_id": uuid.uuid4().hex})
        self.app.db.chat_state_upsert(session_id, status="interrupting")
        self._broadcast(
            session_id,
            {"t": "state", "chat": self.public_state(session_id, user_id)},
        )

    def decide_permission(
        self,
        session_id: str,
        user_id: int,
        permission_id: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        session = self._check_owner(session_id, user_id)
        row = self.app.db.chat_permission_get(permission_id)
        if row is None or row["session_id"] != session_id:
            raise ChatError(404, "permission not found", "not_found")
        if row.get("status") != "pending":
            return {"ok": True, "status": row.get("status"), "duplicate": True}
        behavior = decision.get("behavior")
        if behavior not in ("allow", "deny"):
            raise ChatError(400, "behavior must be allow or deny", "invalid")
        if behavior == "allow":
            result = {
                "behavior": "allow",
                "updatedInput": decision.get("updated_input")
                if decision.get("updated_input") is not None
                else _json(row.get("tool_input"), {}),
            }
            if decision.get("updated_permissions"):
                result["updatedPermissions"] = decision["updated_permissions"]
            status = "allowed"
        else:
            result = {
                "behavior": "deny",
                "message": decision.get("message") or "User denied this action",
            }
            status = "denied"
        resolved = self.app.db.chat_permission_resolve(
            permission_id,
            status=status,
            decision=json.dumps(result),
            resolved_by=str(session.get("name") or user_id),
        )
        if resolved is None:
            current = self.app.db.chat_permission_get(permission_id) or row
            return {"ok": True, "status": current.get("status"), "duplicate": True}
        try:
            self._send_agent(
                session_id,
                {
                    "t": "chat_permission_result",
                    "permission_id": permission_id,
                    "result": result,
                },
            )
            self.app.db.chat_permission_mark_delivered(permission_id)
        except ChatError:
            # The decision is durable; the runner will receive it on hello.
            pass
        self._finish_permission(resolved)
        return {"ok": True, "status": status}

    def request_mode(
        self, session_id: str, user_id: int, to: str, *, force: bool = False
    ) -> None:
        self._check_owner(session_id, user_id)
        if to not in ("chat", "tui", "terminal"):
            raise ChatError(400, "mode must be chat or tui", "invalid")
        to = "tui" if to == "terminal" else to
        # A pending approval cannot survive a mode switch; deny it durably.
        for row in self.app.db.chat_permissions_pending(session_id):
            resolved = self.app.db.chat_permission_expire(row["id"])
            if resolved is None:
                continue
            try:
                self._send_agent(
                    session_id,
                    {
                        "t": "chat_permission_result",
                        "permission_id": row["id"],
                        "result": _json(resolved.get("decision"), {}),
                    },
                )
            except ChatError:
                pass
            self._finish_permission(resolved)
        self._send_agent(session_id, {"t": "chat_handoff", "to": to, "force": bool(force)})
        self.app.db.chat_state_upsert(session_id, mode="handoff", status="handoff")
        self._broadcast(
            session_id,
            {
                "t": "mode",
                "mode": "handoff",
                "status": "handoff",
                "message": f"switching to {to}",
            },
        )

    def request_config(
        self,
        session_id: str,
        user_id: int,
        *,
        permission_mode: str | None = None,
        model: str | None = None,
        force: bool = False,
    ) -> None:
        self._check_owner(session_id, user_id)
        allowed = {
            "",
            "default",
            "acceptEdits",
            "plan",
            "auto",
            "dontAsk",
            "manual",
            "bypassPermissions",
        }
        if permission_mode is not None and permission_mode not in allowed:
            raise ChatError(400, "unsupported permission mode", "invalid")
        self._send_agent(
            session_id,
            {
                "t": "chat_config",
                "permission_mode": permission_mode,
                "model": model,
                "force": bool(force),
            },
        )
        fields: dict[str, Any] = {"status": "restarting"}
        if permission_mode:
            fields["permission_mode"] = permission_mode
        if model:
            fields["model"] = model
        self.app.db.chat_state_upsert(session_id, **fields)
        self._broadcast(
            session_id,
            {
                "t": "state",
                "chat": self.public_state(session_id, user_id),
            },
        )

    def request_rewind(
        self,
        session_id: str,
        user_id: int,
        checkpoint_uuid: str,
        *,
        force: bool = False,
    ) -> None:
        self._check_owner(session_id, user_id)
        checkpoint_uuid = str(checkpoint_uuid or "").strip()
        if not checkpoint_uuid:
            raise ChatError(400, "checkpoint_uuid is required", "invalid")
        known = {
            str(row.get("checkpoint_uuid"))
            for row in self.app.db.chat_checkpoints(session_id)
        }
        if checkpoint_uuid not in known:
            raise ChatError(404, "checkpoint not found", "not_found")
        state = self._state_row(session_id)
        busy = str(state.get("status") or "") in {
            "thinking",
            "tool",
            "waiting_permission",
            "interrupting",
            "restarting",
        }
        if busy and not force:
            raise ChatError(
                409,
                "session is busy; interrupt or pass force to rewind",
                "busy",
            )
        # The runner refuses to open the same Claude session twice; it stops
        # the live child (after an interrupt when forced) before rewinding.
        self._send_agent(
            session_id,
            {
                "t": "chat_rewind",
                "checkpoint_uuid": checkpoint_uuid,
                "force": bool(force),
            },
        )
        self.app.db.chat_state_upsert(session_id, status="restarting")
        self._broadcast(
            session_id,
            {"t": "state", "chat": self.public_state(session_id, user_id)},
        )

    def mark_read(self, session_id: str, user_id: int, last_seq: int) -> None:
        self._check_owner(session_id, user_id)
        self.app.db.chat_read_set(user_id, session_id, int(last_seq))

    def delete_history(self, session_id: str, user_id: int) -> int:
        self._check_owner(session_id, user_id)
        deleted = self.app.db.delete_chat_history(session_id)
        self._broadcast(
            session_id,
            {
                "t": "state",
                "chat": self.public_state(session_id, user_id),
            },
        )
        return deleted

    # ------------------------------------------------------------------ house
    def purge_tick(self) -> None:
        if not self.enabled:
            return
        self.app.db.purge_chat(int(self.cfg.get("retention_days") or 30))
        # Timers die with the process; expire anything that outlived a restart.
        for row in self.app.db.chat_permissions_stale(iso()):
            self._expire(row["id"])

    def agent_detached(self, session_id: str) -> None:
        if self.app.db.get_session(session_id) is None:
            return
        self.app.db.chat_state_upsert(session_id, status="offline")
        self._broadcast(
            session_id,
            {
                "t": "state",
                "chat": self.public_state(session_id, self._owner(session_id)),
            },
        )

    def session_removed(self, session_id: str) -> None:
        with self._lock:
            self._viewers.pop(session_id, None)
        pending = self.app.db.chat_permissions_pending(session_id)
        with self._lock:
            for row in pending:
                timer = self._timers.pop(row["id"], None)
                if timer is not None:
                    timer.cancel()
        for row in pending:
            self.app.db.chat_permission_expire(row["id"])
