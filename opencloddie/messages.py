"""Session-to-session mailbox helpers.

Messages are durable, addressed by session id, and scoped to one user.  A
recipient pulls them (``check_mailbox``); nothing is pushed into a running
Claude session.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

from .auth import AuthError
from .db import Database

MAX_TARGET_CHARS = 200


class SendLimiter:
    """Per-user sliding window for mailbox sends.

    The gateway limiter is keyed by API key and counts inference traffic; this
    one is deliberately separate so messaging cannot exhaust or inherit that
    budget.
    """

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0):
        self.limit = max(0, int(limit_per_minute))
        self.window = float(window_seconds)
        self.lock = threading.Lock()
        self.buckets: dict[int, collections.deque[float]] = {}

    def check(self, user_id: int) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        with self.lock:
            bucket = self.buckets.setdefault(int(user_id), collections.deque())
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry = int(max(1.0, self.window - (now - bucket[0])))
                raise AuthError(
                    429,
                    f"rate limit exceeded ({self.limit} messages/minute)",
                    "rate_limit_error",
                    headers={"Retry-After": str(retry)},
                )
            bucket.append(now)


def resolve_session(
    db: Database,
    user_id: int,
    target: str,
    *,
    exclude_session_id: str = "",
) -> dict[str, Any]:
    """Resolve a session by id, exact name, or ``name@hostname``.

    Names are not unique (they default to the cwd basename), so an ambiguous
    match returns 409 with candidates rather than guessing.
    """
    text = str(target or "").strip()
    if not text:
        raise AuthError(400, "target session is required", "invalid_request")
    if len(text) > MAX_TARGET_CHARS:
        raise AuthError(400, "target session is too long", "invalid_request")

    sessions = db.list_sessions(user_id)
    if exclude_session_id:
        sessions = [s for s in sessions if s["id"] != exclude_session_id]

    for session in sessions:
        if session["id"] == text:
            return session

    folded = text.casefold()
    by_name = [s for s in sessions if (s.get("name") or "").casefold() == folded]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        # A literal "@" in a name is handled by the exact-name match above;
        # here it is the disambiguator.
        if "@" in text:
            name, host = text.rsplit("@", 1)
            by_host = _match_name_host(sessions, name, host)
            if len(by_host) == 1:
                return by_host[0]
            if len(by_host) > 1:
                raise _ambiguous(by_host, text)
        raise _ambiguous(by_name, text)

    if "@" in text:
        name, host = text.rsplit("@", 1)
        by_host = _match_name_host(sessions, name, host)
        if len(by_host) == 1:
            return by_host[0]
        if len(by_host) > 1:
            raise _ambiguous(by_host, text)

    available = [
        {
            "id": s["id"],
            "name": s.get("name") or "",
            "hostname": s.get("hostname") or "",
            "online": bool(s.get("online")),
        }
        for s in sessions[:50]
    ]
    raise AuthError(
        404,
        f"no session named {text!r}",
        "target_not_found",
        extra={"available": available},
    )


def _match_name_host(
    sessions: list[dict[str, Any]], name: str, host: str
) -> list[dict[str, Any]]:
    name_folded = name.casefold()
    host_folded = host.casefold()
    return [
        s
        for s in sessions
        if (s.get("name") or "").casefold() == name_folded
        and (s.get("hostname") or "").casefold() == host_folded
    ]


def _ambiguous(sessions: list[dict[str, Any]], target: str) -> AuthError:
    candidates = [
        {
            "id": s["id"],
            "name": s.get("name") or "",
            "hostname": s.get("hostname") or "",
            "cwd": s.get("cwd") or "",
            "online": bool(s.get("online")),
        }
        for s in sessions[:20]
    ]
    return AuthError(
        409,
        f"session name {target!r} is ambiguous; use name@hostname or the session id",
        "ambiguous_target",
        extra={"candidates": candidates},
    )


def clean_body(value: Any, max_bytes: int) -> str:
    body = str(value or "").replace("\x00", "").strip()
    if not body:
        raise AuthError(400, "message body is required", "invalid_request")
    size = len(body.encode("utf-8"))
    if size > max_bytes:
        raise AuthError(
            413,
            f"message body is too large ({size} bytes; limit {max_bytes})",
            "message_too_large",
        )
    return body


def public_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "from_session_id": row.get("from_session_id") or "",
        "from_name": row.get("from_name") or "",
        "to_session_id": row.get("to_session_id") or "",
        "to_name": row.get("to_name") or "",
        "body": row.get("body") or "",
        "reply_to": row.get("reply_to"),
        "created_at": row.get("created_at") or "",
        "read_at": row.get("read_at"),
    }
