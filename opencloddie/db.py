"""SQLite persistence for users, API keys and terminal sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .crypto import hash_api_key, key_prefix, new_api_key
from .util import iso, new_short_id, safe_username, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    groups TEXT NOT NULL DEFAULT '[]',
    default_model TEXT NOT NULL DEFAULT '',
    opencode_key_enc TEXT NOT NULL DEFAULT '',
    opencode_key_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    api_key_id TEXT,
    name TEXT NOT NULL,
    hostname TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    online INTEGER NOT NULL DEFAULT 0,
    agent_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
"""


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: str, master_secret: str):
        self.path = path
        self.master = master_secret
        self.lock = threading.RLock()
        self._touch_cache: dict[str, float] = {}
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    # ---------------------------------------------------------------- users
    def ensure_user(
        self,
        username: str,
        *,
        email: str = "",
        name: str = "",
        groups: list[str] | None = None,
        default_model: str = "",
    ) -> dict[str, Any]:
        username = safe_username(username)
        groups = groups or []
        now = iso()
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO users(username, email, name, groups, default_model,
                                  created_at, updated_at, last_seen_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                    email=excluded.email,
                    name=excluded.name,
                    groups=excluded.groups,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at
                """,
                (username, email, name, json.dumps(groups), default_model, now, now, now),
            )
            row = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row)

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.lock:
            return _row(
                self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            )

    def list_users(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [dict(r) for r in rows]

    def set_opencode_key(self, user_id: int, encrypted: str, hint: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE users SET opencode_key_enc=?, opencode_key_hint=?, updated_at=? WHERE id=?",
                (encrypted, hint, iso(), user_id),
            )

    def clear_opencode_key(self, user_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE users SET opencode_key_enc='', opencode_key_hint='', updated_at=? WHERE id=?",
                (iso(), user_id),
            )

    def set_default_model(self, user_id: int, model: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE users SET default_model=?, updated_at=? WHERE id=?",
                (model, iso(), user_id),
            )

    def user_secret(self, user_id: int) -> str:
        with self.lock:
            row = self.conn.execute(
                "SELECT opencode_key_enc FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return row["opencode_key_enc"] if row else ""

    # ------------------------------------------------------------- api keys
    def create_api_key(self, user_id: int, name: str, model: str) -> tuple[dict[str, Any], str]:
        token = new_api_key()
        key_id = new_short_id("k")
        now = iso()
        with self.lock, self.conn:
            self.conn.execute(
                """INSERT INTO api_keys(id, user_id, name, prefix, token_hash, model, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (key_id, user_id, name, key_prefix(token), hash_api_key(token, self.master), model, now),
            )
            row = self.conn.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        return dict(row), token

    def list_api_keys(self, user_id: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM api_keys WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def find_api_key(self, token: str) -> dict[str, Any] | None:
        token_hash = hash_api_key(token, self.master)
        with self.lock:
            row = self.conn.execute(
                """
                SELECT k.*, u.username, u.email, u.name AS user_name, u.groups AS user_groups,
                       u.default_model, u.opencode_key_enc
                FROM api_keys k JOIN users u ON u.id = k.user_id
                WHERE k.token_hash=? AND k.revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        self.touch_api_key(data["id"])
        return data

    def touch_api_key(self, key_id: str) -> None:
        now = time.time()
        if now - self._touch_cache.get(key_id, 0) < 30:
            return
        self._touch_cache[key_id] = now
        with self.lock, self.conn:
            self.conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (iso(), key_id))

    def update_api_key(
        self,
        user_id: int,
        key_id: str,
        *,
        name: str | None = None,
        model: str | None = None,
        revoked: bool | None = None,
    ) -> dict[str, Any] | None:
        sets: list[str] = []
        args: list[Any] = []
        if name is not None:
            sets.append("name=?")
            args.append(name)
        if model is not None:
            sets.append("model=?")
            args.append(model)
        if revoked is not None:
            sets.append("revoked_at=?")
            args.append(iso() if revoked else None)
        if not sets:
            with self.lock:
                return _row(
                    self.conn.execute(
                        "SELECT * FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id)
                    ).fetchone()
                )
        args.extend([key_id, user_id])
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE api_keys SET {', '.join(sets)} WHERE id=? AND user_id=?", args
            )
            row = self.conn.execute(
                "SELECT * FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id)
            ).fetchone()
        return _row(row)

    def delete_api_key(self, user_id: int, key_id: str) -> bool:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id)
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------- sessions
    def upsert_session(
        self,
        *,
        session_id: str,
        user_id: int,
        api_key_id: str,
        name: str,
        hostname: str,
        cwd: str,
        model: str,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = iso()
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        with self.lock, self.conn:
            existing = self.conn.execute(
                "SELECT user_id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if existing and existing["user_id"] != user_id:
                raise PermissionError("session id belongs to another user")
            if existing:
                self.conn.execute(
                    """UPDATE sessions SET api_key_id=?, name=?, hostname=?, cwd=?, model=?,
                       online=1, agent_id=?, last_seen_at=?, metadata=? WHERE id=?""",
                    (api_key_id, name, hostname, cwd, model, agent_id, now, metadata_json, session_id),
                )
            else:
                self.conn.execute(
                    """INSERT INTO sessions(id, user_id, api_key_id, name, hostname, cwd, model,
                       online, agent_id, created_at, last_seen_at, metadata)
                       VALUES(?,?,?,?,?,?,?,1,?,?,?,?)""",
                    (session_id, user_id, api_key_id, name, hostname, cwd, model,
                     agent_id, now, now, metadata_json),
                )
            row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row)

    def touch_session(self, session_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET last_seen_at=?, online=1 WHERE id=?", (iso(), session_id)
            )

    def mark_agent_offline(self, agent_id: str) -> list[str]:
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT id FROM sessions WHERE agent_id=? AND online=1", (agent_id,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.conn.execute(
                    "UPDATE sessions SET online=0, last_seen_at=? WHERE agent_id=?",
                    (iso(), agent_id),
                )
        return ids

    def set_session_online(self, session_id: str, online: bool) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET online=?, last_seen_at=? WHERE id=?",
                (1 if online else 0, iso(), session_id),
            )

    def mark_all_sessions_offline(self) -> int:
        """Called at startup: no agent can have survived a server restart."""
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE sessions SET online=0 WHERE online=1")
        return cur.rowcount

    def list_sessions(self, user_id: int, include_offline: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions WHERE user_id=?"
        if not include_offline:
            query += " AND online=1"
        # created_at/id gives the web UI a stable tab order.  Sorting by
        # last_seen_at (or online) made tabs jump every heartbeat.
        query += " ORDER BY created_at ASC, id ASC"
        with self.lock:
            rows = self.conn.execute(query, (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            return _row(
                self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            )

    def delete_session(self, session_id: str, user_id: int | None = None) -> bool:
        with self.lock, self.conn:
            if user_id is None:
                cur = self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            else:
                cur = self.conn.execute(
                    "DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)
                )
        return cur.rowcount > 0

    def purge_offline_sessions(self, older_than_days: int = 30) -> int:
        from datetime import timedelta

        cutoff = iso(utcnow() - timedelta(days=older_than_days))
        with self.lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM sessions WHERE online=0 AND last_seen_at < ?", (cutoff,)
            )
        return cur.rowcount

    def stats(self, user_id: int) -> dict[str, Any]:
        with self.lock:
            keys = self.conn.execute(
                "SELECT COUNT(*) AS n FROM api_keys WHERE user_id=? AND revoked_at IS NULL",
                (user_id,),
            ).fetchone()["n"]
            online = self.conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE user_id=? AND online=1", (user_id,)
            ).fetchone()["n"]
            total = self.conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (user_id,)
            ).fetchone()["n"]
        return {"api_keys": keys, "sessions_online": online, "sessions_total": total}
