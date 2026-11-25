"""Database-level tests for the structured chat tables (schema v4)."""

import json
import os
import tempfile
import unittest
from datetime import timedelta

from tests.harness import ROOT  # noqa: F401

from opencloddie.crypto import new_master_secret
from opencloddie.db import SCHEMA_VERSION, Database
from opencloddie.util import iso, utcnow

CHAT_TABLES = (
    "chat_state",
    "chat_events",
    "chat_permissions",
    "chat_inputs",
    "chat_reads",
    "chat_checkpoints",
)


class ChatDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-chat-db-")
        self.db = Database(os.path.join(self.tmp, "test.db"), new_master_secret())
        self.alice = self.db.ensure_user("alice")
        self.bob = self.db.ensure_user("bob")
        self.uid = self.alice["id"]
        self.sid = "s_chat"
        self.db.upsert_session(
            session_id=self.sid,
            user_id=self.uid,
            api_key_id="",
            name="work",
            hostname="host",
            cwd="/tmp/project",
            model="deepseek-v4.1-flash",
            agent_id="a_chat",
        )

    def tearDown(self):
        self.db.close()

    # --------------------------------------------------------------- helpers
    def _append(self, event_id, kind, payload=None, *, sid=None, turn_id=""):
        return self.db.append_chat_event(
            sid or self.sid,
            self.uid,
            event_id,
            kind,
            payload or {},
            turn_id=turn_id,
        )

    def _create_permission(self, permission_id, *, sid=None, status="pending"):
        return self.db.chat_permission_create(
            {
                "id": permission_id,
                "session_id": sid or self.sid,
                "turn_id": "t1",
                "request_id": "r1",
                "tool_name": "Bash",
                "tool_input": json.dumps({"command": "ls"}),
                "suggestions": json.dumps([{"type": "allow_once"}]),
                "status": status,
                "created_at": iso(),
                "expires_at": iso(utcnow() + timedelta(minutes=5)),
            }
        )

    def _age_row(self, table, key_name, key_value, *, days=90):
        old = iso(utcnow() - timedelta(days=days))
        with self.db.conn:
            self.db.conn.execute(
                f"UPDATE {table} SET created_at=? WHERE {key_name}=?",
                (old, key_value),
            )

    # ---------------------------------------------------------------- schema
    def test_schema_v4_creates_chat_tables(self):
        tables = {
            row[0]
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in CHAT_TABLES:
            self.assertIn(table, tables)

        version = self.db.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(int(version), 4)
        self.assertEqual(SCHEMA_VERSION, 4)

    # ----------------------------------------------------------------- state
    def test_state_upsert_defaults_and_merge(self):
        self.assertIsNone(self.db.chat_state_get(self.sid))

        row = self.db.chat_state_upsert(self.sid)
        self.assertEqual(row["mode"], "tui")
        self.assertEqual(row["status"], "offline")
        self.assertEqual(row["permission_mode"], "default")
        self.assertEqual(row["last_seq"], 0)
        self.assertEqual(row["pending_count"], 0)
        self.assertEqual(json.loads(row["capabilities"]), {})
        self.assertIsNone(row["context_tokens"])
        self.assertTrue(row["updated_at"])

        merged = self.db.chat_state_upsert(
            self.sid,
            status="idle",
            model="deepseek-v4.1-flash",
            capabilities={"interrupt_receipt_v1": True},
            cost_usd=0.25,
            context_tokens=1234,
        )
        self.assertEqual(merged["mode"], "tui")  # untouched default survives
        self.assertEqual(merged["status"], "idle")
        self.assertEqual(merged["model"], "deepseek-v4.1-flash")
        self.assertEqual(merged["cost_usd"], 0.25)
        self.assertEqual(merged["context_tokens"], 1234)
        self.assertEqual(
            json.loads(merged["capabilities"]), {"interrupt_receipt_v1": True}
        )
        self.assertEqual(self.db.chat_state_get(self.sid), merged)

    # ---------------------------------------------------------------- events
    def test_events_are_monotonic_deduped_and_ordered(self):
        # last_seq lives on chat_state, so the row must exist before events.
        self.db.chat_state_upsert(self.sid)
        seq1, inserted1 = self._append(
            "e1", "assistant_block", {"text": "one"}, turn_id="t1"
        )
        self.assertTrue(inserted1)
        seq2, inserted2 = self._append(
            "e2", "message_start", {"message_id": "m1"}, turn_id="t1"
        )
        self.assertTrue(inserted2)
        self.assertGreater(seq2, seq1)
        self.assertEqual(self.db.chat_state_get(self.sid)["last_seq"], seq2)

        # The duplicate keeps the original seq and does not advance the log.
        dup_seq, dup_inserted = self._append("e1", "result", {"ignored": True})
        self.assertFalse(dup_inserted)
        self.assertEqual(dup_seq, seq1)
        self.assertEqual(self.db.chat_state_get(self.sid)["last_seq"], seq2)
        self.assertEqual(self.db.chat_event_count(self.sid), 2)

        rows = self.db.chat_events_after(self.sid)
        self.assertEqual([r["event_id"] for r in rows], ["e1", "e2"])
        self.assertEqual([r["seq"] for r in rows], [seq1, seq2])
        self.assertEqual(json.loads(rows[0]["payload"]), {"text": "one"})
        self.assertEqual(rows[0]["turn_id"], "t1")

        after = self.db.chat_events_after(self.sid, seq1)
        self.assertEqual([r["event_id"] for r in after], ["e2"])
        self.assertEqual(self.db.chat_events_after(self.sid, seq2), [])

        self.assertEqual(
            [r["event_id"] for r in self.db.latest_chat_events(self.sid, 1)],
            ["e2"],
        )
        self.assertEqual(
            [r["event_id"] for r in self.db.latest_chat_events(self.sid, 10)],
            ["e1", "e2"],
        )

        # Event ids are scoped per session.
        self.db.upsert_session(
            session_id="s_other",
            user_id=self.uid,
            api_key_id="",
            name="other",
            hostname="",
            cwd="",
            model="",
            agent_id="a_other",
        )
        other_seq, other_inserted = self._append("e1", "result", {}, sid="s_other")
        self.assertTrue(other_inserted)
        self.assertNotEqual(other_seq, seq1)
        self.assertEqual(self.db.chat_event_count("s_other"), 1)

    def test_unread_counts_and_read_watermark(self):
        seq1, _ = self._append("e1", "assistant_block", {})
        seq2, _ = self._append("e2", "message_start", {})
        seq3, _ = self._append("e3", "result", {})
        seq4, _ = self._append("e4", "permission", {})

        self.assertEqual(self.db.chat_unread(self.uid, self.sid), 3)
        self.assertEqual(self.db.chat_read_get(self.uid, self.sid), 0)

        self.db.chat_read_set(self.uid, self.sid, seq1)
        self.assertEqual(self.db.chat_read_get(self.uid, self.sid), seq1)
        self.assertEqual(self.db.chat_unread(self.uid, self.sid), 2)

        # message_start does not count as unread activity.
        self.db.chat_read_set(self.uid, self.sid, seq2)
        self.assertEqual(self.db.chat_unread(self.uid, self.sid), 2)

        self.db.chat_read_set(self.uid, self.sid, seq4)
        self.assertEqual(self.db.chat_unread(self.uid, self.sid), 0)

        # The watermark never moves backwards.
        self.db.chat_read_set(self.uid, self.sid, 0)
        self.assertEqual(self.db.chat_read_get(self.uid, self.sid), seq4)

        # Read watermarks are per user.
        self.assertEqual(self.db.chat_read_get(self.bob["id"], self.sid), 0)
        self.db.chat_read_set(self.bob["id"], self.sid, seq1)
        self.assertEqual(self.db.chat_unread(self.bob["id"], self.sid), 2)
        self.assertEqual(self.db.chat_unread(self.uid, self.sid), 0)

    # ----------------------------------------------------------- permissions
    def test_permission_lifecycle_pending_count_and_expiry(self):
        self.db.chat_state_upsert(self.sid)

        row = self._create_permission("p1")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(json.loads(row["tool_input"]), {"command": "ls"})
        self.assertEqual(json.loads(row["suggestions"]), [{"type": "allow_once"}])
        self.assertIsNone(row["resolved_at"])

        pending = self.db.chat_permissions_pending(self.sid)
        self.assertEqual([p["id"] for p in pending], ["p1"])
        self.assertEqual(self.db.chat_state_get(self.sid)["pending_count"], 1)

        resolved = self.db.chat_permission_resolve(
            "p1",
            status="allowed",
            decision=json.dumps({"behavior": "allow"}),
            resolved_by="alice",
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["status"], "allowed")
        self.assertEqual(resolved["resolved_by"], "alice")
        self.assertTrue(resolved["resolved_at"])
        self.assertEqual(self.db.chat_permissions_pending(self.sid), [])
        self.assertEqual(self.db.chat_state_get(self.sid)["pending_count"], 0)

        # Atomic resolve: the second caller loses.
        self.assertIsNone(
            self.db.chat_permission_resolve(
                "p1",
                status="denied",
                decision=json.dumps({"behavior": "deny"}),
                resolved_by="bob",
            )
        )
        self.assertEqual(self.db.chat_permission_get("p1")["status"], "allowed")

        # Expiry goes through the same atomic path and denies by default.
        self._create_permission("p2")
        self._create_permission("p3")
        expired = self.db.chat_permission_expire("p2")
        self.assertIsNotNone(expired)
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(json.loads(expired["decision"])["behavior"], "deny")
        self.assertEqual(expired["resolved_by"], "timeout")
        self.assertIsNone(self.db.chat_permission_expire("p2"))

        expired_session = self.db.chat_permissions_expire_session(self.sid)
        self.assertEqual([row["id"] for row in expired_session], ["p3"])
        self.assertEqual(self.db.chat_permissions_pending(self.sid), [])
        self.assertEqual(self.db.chat_state_get(self.sid)["pending_count"], 0)

        self.db.chat_permission_mark_delivered("p1")
        self.assertTrue(self.db.chat_permission_get("p1")["delivered_at"])

    # ----------------------------------------------------- delete / retention
    def test_checkpoints_inputs_and_delete_history(self):
        self.db.chat_state_upsert(self.sid)
        seq, _ = self._append("e1", "assistant_block", {})
        self._create_permission("p1")
        self.db.chat_checkpoint_add(self.sid, "cp1", seq)
        self.db.chat_checkpoint_add(self.sid, "cp1", seq)  # idempotent
        self.assertEqual(
            [row["checkpoint_uuid"] for row in self.db.chat_checkpoints(self.sid)],
            ["cp1"],
        )
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO chat_inputs(session_id,client_msg_id,seq,created_at) "
                "VALUES(?,?,?,?)",
                (self.sid, "m1", seq, iso()),
            )

        deleted = self.db.delete_chat_history(self.sid)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.db.chat_events_after(self.sid), [])
        self.assertEqual(self.db.chat_permissions_pending(self.sid), [])
        self.assertEqual(self.db.chat_checkpoints(self.sid), [])
        inputs = self.db.conn.execute(
            "SELECT COUNT(*) FROM chat_inputs WHERE session_id=?", (self.sid,)
        ).fetchone()[0]
        self.assertEqual(inputs, 0)
        state = self.db.chat_state_get(self.sid)
        self.assertEqual(state["last_seq"], 0)
        self.assertEqual(state["pending_count"], 0)
        self.assertIsNone(state["last_event_at"])

    def test_purge_chat_respects_retention_and_pending_permissions(self):
        self.db.chat_state_upsert(self.sid)
        old_seq, _ = self._append("old", "result", {})
        new_seq, _ = self._append("new", "result", {})
        self._age_row("chat_events", "event_id", "old")

        self._create_permission("p_old_resolved")
        self.db.chat_permission_resolve(
            "p_old_resolved",
            status="allowed",
            decision=json.dumps({"behavior": "allow"}),
            resolved_by="alice",
        )
        self._age_row("chat_permissions", "id", "p_old_resolved")

        self._create_permission("p_old_pending")
        self._age_row("chat_permissions", "id", "p_old_pending")

        self._create_permission("p_new_pending")

        removed = self.db.purge_chat(30)
        self.assertEqual(removed, 1)
        self.assertEqual(
            [row["event_id"] for row in self.db.chat_events_after(self.sid)],
            ["new"],
        )
        # Pending permissions are kept regardless of age so the UI can answer.
        self.assertIsNone(self.db.chat_permission_get("p_old_resolved"))
        self.assertIsNotNone(self.db.chat_permission_get("p_old_pending"))
        self.assertIsNotNone(self.db.chat_permission_get("p_new_pending"))
        self.assertEqual(
            {row["id"] for row in self.db.chat_permissions_pending(self.sid)},
            {"p_old_pending", "p_new_pending"},
        )


if __name__ == "__main__":
    unittest.main()
