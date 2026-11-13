"""SQLite persistence for users, API keys and terminal sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .crypto import hash_api_key, key_prefix, new_api_key
from .util import iso, new_short_id, safe_username, utcnow

SCHEMA_VERSION = 3

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
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_session_id TEXT,
    from_name TEXT NOT NULL DEFAULT '',
    to_session_id TEXT NOT NULL,
    to_name TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    reply_to INTEGER,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox
    ON messages(user_id, to_session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_outbox
    ON messages(user_id, from_session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages(user_id, to_session_id, read_at);
CREATE INDEX IF NOT EXISTS idx_messages_age ON messages(created_at);
CREATE TABLE IF NOT EXISTS usage_minutes (
    bucket INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    api_key_id TEXT NOT NULL,
    api_key_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    client_errors INTEGER NOT NULL DEFAULT 0,
    server_errors INTEGER NOT NULL DEFAULT 0,
    rate_limited INTEGER NOT NULL DEFAULT 0,
    bytes_in INTEGER NOT NULL DEFAULT 0,
    bytes_out INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, api_key_id, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_user_bucket ON usage_minutes(user_id, bucket);
CREATE INDEX IF NOT EXISTS idx_usage_bucket ON usage_minutes(bucket);
CREATE TABLE IF NOT EXISTS instance_minutes (
    bucket INTEGER PRIMARY KEY,
    sessions_online INTEGER NOT NULL DEFAULT 0,
    sessions_total INTEGER NOT NULL DEFAULT 0,
    agents INTEGER NOT NULL DEFAULT 0,
    viewers INTEGER NOT NULL DEFAULT 0,
    rss_bytes INTEGER,
    cpu_seconds REAL,
    threads INTEGER,
    db_bytes INTEGER
);
"""


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name, 0)
    return getattr(row, name, 0)


class MailboxFull(Exception):
    """Raised when a recipient's unread mailbox is at its configured cap."""


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
            else:
                try:
                    stored = int(row["value"])
                except (TypeError, ValueError):
                    stored = 0
                if stored < SCHEMA_VERSION:
                    self.conn.execute(
                        "UPDATE meta SET value=? WHERE key='schema_version'",
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

    # ------------------------------------------------------------ messages
    def add_message(
        self,
        *,
        user_id: int,
        to_session_id: str,
        body: str,
        from_session_id: str | None = None,
        from_name: str = "",
        to_name: str = "",
        reply_to: int | None = None,
        max_unread: int = 0,
    ) -> dict[str, Any]:
        """Insert a mailbox message.

        The recipient must exist and belong to ``user_id``.  The sender is
        optional: a session row may not exist yet during the brief window
        between tmux start and agent registration, and a human CLI/web send has
        no session at all.  When the sender row exists, its name is used.
        """
        now = iso()
        with self.lock, self.conn:
            recipient = self.conn.execute(
                "SELECT id, name FROM sessions WHERE id=? AND user_id=?",
                (to_session_id, user_id),
            ).fetchone()
            if recipient is None:
                raise LookupError("target session not found")
            if from_session_id:
                sender = self.conn.execute(
                    "SELECT name FROM sessions WHERE id=? AND user_id=?",
                    (from_session_id, user_id),
                ).fetchone()
                if sender is not None:
                    from_name = sender["name"] or from_name
                elif from_name == "":
                    # The id is known but its row is not registered yet; keep
                    # the message rather than dropping it in a startup race.
                    from_name = from_session_id
            if max_unread > 0:
                unread = self.conn.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE user_id=? AND to_session_id=? AND read_at IS NULL",
                    (user_id, to_session_id),
                ).fetchone()[0]
                if int(unread) >= max_unread:
                    raise MailboxFull(to_session_id)
            cur = self.conn.execute(
                """INSERT INTO messages(user_id, from_session_id, from_name,
                       to_session_id, to_name, body, reply_to, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    from_session_id,
                    from_name,
                    to_session_id,
                    to_name or recipient["name"],
                    body,
                    reply_to,
                    now,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM messages WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_messages(
        self,
        user_id: int,
        session_id: str,
        *,
        unread_only: bool = False,
        limit: int = 50,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM messages WHERE user_id=? AND to_session_id=?"
        params: list[Any] = [user_id, session_id]
        if unread_only:
            query += " AND read_at IS NULL"
        if after_id is not None:
            query += " AND id > ?"
            params.append(int(after_id))
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_conversation(
        self,
        user_id: int,
        session_a: str,
        session_b: str = "",
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if session_b:
            query = (
                "SELECT * FROM messages WHERE user_id=? AND ("
                "(from_session_id=? AND to_session_id=?) OR "
                "(from_session_id=? AND to_session_id=?)) "
                "ORDER BY id ASC LIMIT ?"
            )
            params: list[Any] = [user_id, session_a, session_b, session_b, session_a, limit]
        else:
            query = (
                "SELECT * FROM messages WHERE user_id=? AND "
                "(from_session_id=? OR to_session_id=?) ORDER BY id ASC LIMIT ?"
            )
            params = [user_id, session_a, session_a, limit]
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_message(self, user_id: int, message_id: int) -> dict[str, Any] | None:
        with self.lock:
            return _row(
                self.conn.execute(
                    "SELECT * FROM messages WHERE id=? AND user_id=?",
                    (int(message_id), user_id),
                ).fetchone()
            )

    def mark_messages_read(self, user_id: int, session_id: str, ids: list[int]) -> int:
        clean = sorted({int(value) for value in ids})[:100]
        if not clean:
            return 0
        placeholders = ",".join("?" for _ in clean)
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE messages SET read_at=? WHERE user_id=? AND to_session_id=? "
                f"AND read_at IS NULL AND id IN ({placeholders})",
                [iso(), user_id, session_id, *clean],
            )
        return cur.rowcount

    def unread_counts(self, user_id: int) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT to_session_id, COUNT(*) AS n FROM messages "
                "WHERE user_id=? AND read_at IS NULL GROUP BY to_session_id",
                (user_id,),
            ).fetchall()
        return {row["to_session_id"]: int(row["n"]) for row in rows}

    def count_unread(self, user_id: int, session_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE user_id=? AND to_session_id=? AND read_at IS NULL",
                (user_id, session_id),
            ).fetchone()
        return int(row[0])

    def purge_messages(self, older_than_days: int = 30) -> int:
        from datetime import timedelta

        cutoff = iso(utcnow() - timedelta(days=max(1, int(older_than_days))))
        with self.lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff,)
            )
        return cur.rowcount

    # ------------------------------------------------------------- metrics
    def add_usage_rows(self, bucket: int, rows: list[Any]) -> None:
        if not rows:
            return
        values = [
            (
                int(bucket),
                int(_row_value(row, "user_id")),
                str(_row_value(row, "key_id")),
                str(_row_value(row, "key_name")),
                str(_row_value(row, "username")),
                str(_row_value(row, "model")),
                int(_row_value(row, "requests")),
                int(_row_value(row, "ok")),
                int(_row_value(row, "client_errors")),
                int(_row_value(row, "server_errors")),
                int(_row_value(row, "rate_limited")),
                int(_row_value(row, "bytes_in")),
                int(_row_value(row, "bytes_out")),
                int(_row_value(row, "tokens_in")),
                int(_row_value(row, "tokens_out")),
                int(_row_value(row, "cache_read_tokens")),
                int(_row_value(row, "cache_creation_tokens")),
            )
            for row in rows
        ]
        with self.lock, self.conn:
            self.conn.executemany(
                """
                INSERT INTO usage_minutes(
                    bucket, user_id, api_key_id, api_key_name, username, model,
                    requests, ok, client_errors, server_errors, rate_limited,
                    bytes_in, bytes_out, tokens_in, tokens_out,
                    cache_read_tokens, cache_creation_tokens)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bucket, api_key_id, model) DO UPDATE SET
                    user_id=excluded.user_id,
                    api_key_name=excluded.api_key_name,
                    username=excluded.username,
                    requests=usage_minutes.requests + excluded.requests,
                    ok=usage_minutes.ok + excluded.ok,
                    client_errors=usage_minutes.client_errors + excluded.client_errors,
                    server_errors=usage_minutes.server_errors + excluded.server_errors,
                    rate_limited=usage_minutes.rate_limited + excluded.rate_limited,
                    bytes_in=usage_minutes.bytes_in + excluded.bytes_in,
                    bytes_out=usage_minutes.bytes_out + excluded.bytes_out,
                    tokens_in=usage_minutes.tokens_in + excluded.tokens_in,
                    tokens_out=usage_minutes.tokens_out + excluded.tokens_out,
                    cache_read_tokens=usage_minutes.cache_read_tokens + excluded.cache_read_tokens,
                    cache_creation_tokens=usage_minutes.cache_creation_tokens + excluded.cache_creation_tokens
                """,
                values,
            )

    def usage_series(
        self, since: int, step: int, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        step = max(1, int(step))
        sql = """
            SELECT (bucket / ?) * ? AS t,
                   SUM(requests) AS requests,
                   SUM(ok) AS ok,
                   SUM(client_errors) AS client_errors,
                   SUM(server_errors) AS server_errors,
                   SUM(rate_limited) AS rate_limited,
                   SUM(bytes_in) AS bytes_in,
                   SUM(bytes_out) AS bytes_out,
                   SUM(tokens_in) AS tokens_in,
                   SUM(tokens_out) AS tokens_out,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_creation_tokens) AS cache_creation_tokens
            FROM usage_minutes WHERE bucket >= ?
        """
        args: list[Any] = [step, step, int(since)]
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        sql += " GROUP BY t ORDER BY t"
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        series: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["errors"] = int(
                item["client_errors"] + item["server_errors"] + item["rate_limited"]
            )
            series.append(item)
        return series

    def add_instance_sample(self, bucket: int, sample: dict[str, Any]) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO instance_minutes(
                    bucket, sessions_online, sessions_total, agents, viewers,
                    rss_bytes, cpu_seconds, threads, db_bytes)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bucket) DO UPDATE SET
                    sessions_online=excluded.sessions_online,
                    sessions_total=excluded.sessions_total,
                    agents=excluded.agents,
                    viewers=excluded.viewers,
                    rss_bytes=excluded.rss_bytes,
                    cpu_seconds=excluded.cpu_seconds,
                    threads=excluded.threads,
                    db_bytes=excluded.db_bytes
                """,
                (
                    int(bucket),
                    int(sample.get("sessions_online") or 0),
                    int(sample.get("sessions_total") or 0),
                    int(sample.get("agents") or 0),
                    int(sample.get("viewers") or 0),
                    sample.get("rss_bytes"),
                    sample.get("cpu_seconds"),
                    sample.get("threads"),
                    sample.get("db_bytes"),
                ),
            )

    def instance_series(self, since: int, step: int) -> list[dict[str, Any]]:
        step = max(1, int(step))
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT (bucket / ?) * ? AS t,
                       AVG(sessions_online) AS sessions_online,
                       AVG(sessions_total) AS sessions_total,
                       AVG(agents) AS agents,
                       AVG(viewers) AS viewers,
                       AVG(rss_bytes) AS rss_bytes,
                       MAX(cpu_seconds) AS cpu_seconds,
                       AVG(threads) AS threads,
                       AVG(db_bytes) AS db_bytes
                FROM instance_minutes WHERE bucket >= ?
                GROUP BY t ORDER BY t
                """,
                (step, step, int(since)),
            ).fetchall()
        series: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for name in (
                "sessions_online",
                "sessions_total",
                "agents",
                "viewers",
                "rss_bytes",
                "threads",
                "db_bytes",
            ):
                value = item.get(name)
                item[name] = int(round(value)) if value is not None else None
            series.append(item)
        return series

    def purge_metrics(self, cutoff_bucket: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM usage_minutes WHERE bucket < ?", (int(cutoff_bucket),))
            self.conn.execute(
                "DELETE FROM instance_minutes WHERE bucket < ?", (int(cutoff_bucket),)
            )

    def instance_counts(self) -> dict[str, int]:
        with self.lock:
            sessions_online = self.conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE online=1"
            ).fetchone()["n"]
            sessions_total = self.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            users = self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            api_keys = self.conn.execute(
                "SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at IS NULL"
            ).fetchone()["n"]
        return {
            "sessions_online": sessions_online,
            "sessions_total": sessions_total,
            "users": users,
            "api_keys_active": api_keys,
        }

    def list_sessions_scoped(self, user_id: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT s.*, u.username AS username FROM sessions s "
            "JOIN users u ON u.id = s.user_id"
        )
        args: list[Any] = []
        if user_id is not None:
            query += " WHERE s.user_id=?"
            args.append(user_id)
        query += " ORDER BY s.created_at ASC, s.id ASC"
        with self.lock:
            rows = self.conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def list_api_keys_scoped(self, user_id: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT k.*, u.username AS username FROM api_keys k "
            "JOIN users u ON u.id = k.user_id"
        )
        args: list[Any] = []
        if user_id is not None:
            query += " WHERE k.user_id=?"
            args.append(user_id)
        query += " ORDER BY k.created_at DESC"
        with self.lock:
            rows = self.conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def db_size_bytes(self) -> int | None:
        from .metrics import db_size_bytes

        return db_size_bytes(self.path)

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
