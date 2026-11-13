import os
import sqlite3
import tempfile
import unittest

from tests.harness import ROOT  # noqa: F401

from opencloddie.crypto import new_master_secret
from opencloddie.db import Database, MailboxFull
from opencloddie.metrics import UsageDelta


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-db-")
        self.db = Database(os.path.join(self.tmp, "test.db"), new_master_secret())

    def tearDown(self):
        self.db.close()

    def test_user_upsert_and_secret(self):
        user = self.db.ensure_user("alice", email="a@example.com", groups=["users"])
        again = self.db.ensure_user("alice", email="b@example.com", groups=["users", "god"])
        self.assertEqual(user["id"], again["id"])
        self.assertEqual(again["email"], "b@example.com")
        self.assertEqual(again["groups"], '["users", "god"]')
        self.db.set_opencode_key(user["id"], "encrypted", "…1234")
        self.assertTrue(self.db.user_secret(user["id"]))
        self.db.clear_opencode_key(user["id"])
        self.assertEqual(self.db.user_secret(user["id"]), "")

    def test_invalid_username(self):
        with self.assertRaises(ValueError):
            self.db.ensure_user("bad name!")

    def test_api_keys(self):
        user = self.db.ensure_user("alice")
        row, token = self.db.create_api_key(user["id"], "laptop", "deepseek-v4.1-flash")
        self.assertTrue(token.startswith("ocd_sk_"))
        found = self.db.find_api_key(token)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], row["id"])
        self.assertEqual(found["username"], "alice")
        self.assertEqual(found["opencode_key_enc"], "")

        updated = self.db.update_api_key(user["id"], row["id"], model="qwen3.8-flash")
        self.assertEqual(updated["model"], "qwen3.8-flash")
        self.assertEqual(len(self.db.list_api_keys(user["id"])), 1)

        self.db.update_api_key(user["id"], row["id"], revoked=True)
        self.assertIsNone(self.db.find_api_key(token))
        self.assertTrue(self.db.list_api_keys(user["id"])[0]["revoked_at"])
        self.assertTrue(self.db.delete_api_key(user["id"], row["id"]))
        self.assertEqual(self.db.list_api_keys(user["id"]), [])

    def test_session_ownership_and_offline(self):
        alice = self.db.ensure_user("alice")
        bob = self.db.ensure_user("bob")
        row, _ = self.db.create_api_key(alice["id"], "k", "m")
        session = self.db.upsert_session(
            session_id="s_one",
            user_id=alice["id"],
            api_key_id=row["id"],
            name="one",
            hostname="host",
            cwd="/tmp",
            model="deepseek-v4.1-flash",
            agent_id="a-one",
        )
        self.assertEqual(session["online"], 1)
        self.assertEqual(len(self.db.list_sessions(alice["id"])), 1)
        self.assertEqual(len(self.db.list_sessions(bob["id"])), 0)

        with self.assertRaises(PermissionError):
            self.db.upsert_session(
                session_id="s_one",
                user_id=bob["id"],
                api_key_id=row["id"],
                name="hijack",
                hostname="",
                cwd="",
                model="",
                agent_id="a-two",
            )

        self.assertEqual(self.db.mark_agent_offline("a-one"), ["s_one"])
        self.assertEqual(self.db.get_session("s_one")["online"], 0)
        self.assertTrue(self.db.delete_session("s_one", alice["id"]))
        self.assertIsNone(self.db.get_session("s_one"))

    def test_list_sessions_order_is_stable(self):
        user = self.db.ensure_user("alice")
        key, _ = self.db.create_api_key(user["id"], "k", "m")
        for name in ("one", "two", "three"):
            self.db.upsert_session(
                session_id=f"s_{name}",
                user_id=user["id"],
                api_key_id=key["id"],
                name=name,
                hostname="host",
                cwd="/tmp",
                model="m",
                agent_id=f"a_{name}",
            )
        first = [s["id"] for s in self.db.list_sessions(user["id"])]
        self.assertEqual(len(first), 3)
        # Simulate a heartbeat/last_seen update on the session that is last in
        # the list.  Sorting by last_seen_at (the old behavior) would move it
        # to the front and make web tabs jump every poll.
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "UPDATE sessions SET last_seen_at='2099-01-01T00:00:00Z' WHERE id=?",
                (first[-1],),
            )
        second = [s["id"] for s in self.db.list_sessions(user["id"])]
        self.assertEqual(first, second)

    def test_mark_all_offline_and_stats(self):
        user = self.db.ensure_user("alice")
        key, _ = self.db.create_api_key(user["id"], "k", "m")
        self.db.upsert_session(
            session_id="s_one",
            user_id=user["id"],
            api_key_id=key["id"],
            name="one",
            hostname="",
            cwd="",
            model="",
            agent_id="a",
        )
        self.assertEqual(self.db.mark_all_sessions_offline(), 1)
        stats = self.db.stats(user["id"])
        self.assertEqual(stats["api_keys"], 1)
        self.assertEqual(stats["sessions_total"], 1)
        self.assertEqual(stats["sessions_online"], 0)


    def test_messages_scope_ack_cap_and_purge(self):
        alice = self.db.ensure_user("alice")
        bob = self.db.ensure_user("bob")
        alice_key, _ = self.db.create_api_key(alice["id"], "k", "m")
        bob_key, _ = self.db.create_api_key(bob["id"], "k", "m")
        for user, key, session_id, name in (
            (alice, alice_key, "s_a", "a"),
            (alice, alice_key, "s_b", "b"),
            (bob, bob_key, "s_c", "c"),
        ):
            self.db.upsert_session(
                session_id=session_id,
                user_id=user["id"],
                api_key_id=key["id"],
                name=name,
                hostname="host",
                cwd="/tmp",
                model="m",
                agent_id="a_" + session_id,
            )

        row = self.db.add_message(
            user_id=alice["id"],
            to_session_id="s_b",
            body="hello",
            from_session_id="s_a",
            max_unread=2,
        )
        self.assertEqual(row["from_name"], "a")
        self.assertEqual(row["to_name"], "b")
        self.assertEqual(self.db.count_unread(alice["id"], "s_b"), 1)
        self.assertEqual(self.db.unread_counts(alice["id"]), {"s_b": 1})
        self.assertEqual(self.db.list_messages(alice["id"], "s_b")[0]["body"], "hello")
        self.assertEqual(self.db.list_messages(bob["id"], "s_b"), [])
        self.assertEqual(self.db.get_message(alice["id"], row["id"])["body"], "hello")
        self.assertIsNone(self.db.get_message(bob["id"], row["id"]))

        # A user cannot mark another user's messages read.
        self.assertEqual(self.db.mark_messages_read(bob["id"], "s_b", [row["id"]]), 0)
        self.assertEqual(self.db.mark_messages_read(alice["id"], "s_b", [row["id"]]), 1)
        self.assertEqual(self.db.count_unread(alice["id"], "s_b"), 0)

        # The cap counts unread messages only.
        self.db.add_message(
            user_id=alice["id"],
            to_session_id="s_b",
            body="one",
            from_session_id="s_a",
            max_unread=1,
        )
        with self.assertRaises(MailboxFull):
            self.db.add_message(
                user_id=alice["id"],
                to_session_id="s_b",
                body="two",
                from_session_id="s_a",
                max_unread=1,
            )

        # Replies show up in both directions.
        self.db.add_message(
            user_id=alice["id"],
            to_session_id="s_a",
            body="hi",
            from_session_id="s_b",
        )
        conversation = self.db.list_conversation(alice["id"], "s_a", "s_b")
        self.assertEqual([m["body"] for m in conversation], ["hello", "one", "hi"])

        # The recipient must belong to the sender's user.
        with self.assertRaises(LookupError):
            self.db.add_message(user_id=bob["id"], to_session_id="s_b", body="x")

        with self.db.lock, self.db.conn:
            self.db.conn.execute("UPDATE messages SET created_at='2000-01-01T00:00:00Z'")
        self.assertEqual(self.db.purge_messages(1), 3)

    def test_metrics_schema_migrates_from_v1(self):
        path = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES('schema_version', '1');"
        )
        conn.commit()
        conn.close()

        db = Database(path, new_master_secret())
        try:
            tables = {
                row[0]
                for row in db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("usage_minutes", tables)
            self.assertIn("instance_minutes", tables)
            self.assertIn("messages", tables)
            version = db.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, "3")
        finally:
            db.close()

    def test_usage_history_aggregates_and_scopes(self):
        alice = self.db.ensure_user("alice")
        bob = self.db.ensure_user("bob")
        alice_key, _ = self.db.create_api_key(alice["id"], "laptop", "m")
        bob_key, _ = self.db.create_api_key(bob["id"], "desktop", "m")

        self.db.add_usage_rows(120, [UsageDelta(
            user_id=alice["id"], key_id=alice_key["id"], key_name="laptop",
            username="alice", model="m", requests=1, ok=1,
            bytes_in=10, bytes_out=20, tokens_in=3, tokens_out=4,
        )])
        # Same bucket/key/model is an upsert, not a second row.
        self.db.add_usage_rows(120, [UsageDelta(
            user_id=alice["id"], key_id=alice_key["id"], key_name="laptop",
            username="alice", model="m", requests=1, ok=1,
            bytes_in=5, bytes_out=6, tokens_in=1, tokens_out=2,
        )])
        self.db.add_usage_rows(180, [UsageDelta(
            user_id=alice["id"], key_id=alice_key["id"], key_name="laptop",
            username="alice", model="m", requests=1, client_errors=1,
            bytes_in=1, bytes_out=2,
        )])
        self.db.add_usage_rows(120, [UsageDelta(
            user_id=bob["id"], key_id=bob_key["id"], key_name="desktop",
            username="bob", model="m", requests=1, ok=1,
        )])

        mine = self.db.usage_series(0, 60, alice["id"])
        self.assertEqual(len(mine), 2)
        self.assertEqual(mine[0]["t"], 120)
        self.assertEqual(mine[0]["requests"], 2)
        self.assertEqual(mine[0]["bytes_in"], 15)
        self.assertEqual(mine[0]["bytes_out"], 26)
        self.assertEqual(mine[0]["tokens_in"], 4)
        self.assertEqual(mine[1]["errors"], 1)

        everyone = self.db.usage_series(0, 60, None)
        self.assertEqual(sum(row["requests"] for row in everyone), 4)

        grouped = self.db.usage_series(0, 120, alice["id"])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["t"], 120)
        self.assertEqual(grouped[0]["requests"], 3)

    def test_instance_history_and_purge(self):
        self.db.add_instance_sample(120, {
            "sessions_online": 1, "sessions_total": 2, "agents": 1,
            "viewers": 0, "rss_bytes": 100, "cpu_seconds": 1.5,
            "threads": 9, "db_bytes": 5,
        })
        # A forced flush in the same minute overwrites the sample.
        self.db.add_instance_sample(120, {
            "sessions_online": 3, "sessions_total": 4, "agents": 2,
            "viewers": 1, "rss_bytes": 200, "cpu_seconds": 2.5,
            "threads": 10, "db_bytes": 6,
        })
        self.db.add_instance_sample(180, {
            "sessions_online": 1, "sessions_total": 4, "agents": 1,
            "viewers": 0, "rss_bytes": 300, "cpu_seconds": 3.5,
            "threads": 10, "db_bytes": 7,
        })
        series = self.db.instance_series(0, 60)
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0]["sessions_online"], 3)
        self.assertEqual(series[0]["rss_bytes"], 200)

        grouped = self.db.instance_series(0, 120)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["sessions_online"], 2)
        self.assertEqual(grouped[0]["cpu_seconds"], 3.5)

        self.db.purge_metrics(150)
        self.assertEqual(len(self.db.instance_series(0, 60)), 1)
        self.assertEqual(len(self.db.usage_series(0, 60)), 0)

    def test_scoped_lists_and_instance_counts(self):
        alice = self.db.ensure_user("alice")
        bob = self.db.ensure_user("bob")
        alice_key, _ = self.db.create_api_key(alice["id"], "a", "m")
        bob_key, _ = self.db.create_api_key(bob["id"], "b", "m")
        for user, key, name in ((alice, alice_key, "one"), (bob, bob_key, "two")):
            self.db.upsert_session(
                session_id=f"s_{name}", user_id=user["id"], api_key_id=key["id"],
                name=name, hostname="host", cwd="/tmp", model="m",
                agent_id=f"a_{name}",
            )

        self.assertEqual(len(self.db.list_sessions_scoped(alice["id"])), 1)
        everyone = self.db.list_sessions_scoped(None)
        self.assertEqual(len(everyone), 2)
        self.assertTrue(all(row["username"] for row in everyone))

        self.assertEqual(len(self.db.list_api_keys_scoped(alice["id"])), 1)
        self.assertEqual(len(self.db.list_api_keys_scoped(None)), 2)

        counts = self.db.instance_counts()
        self.assertEqual(counts["users"], 2)
        self.assertEqual(counts["api_keys_active"], 2)
        self.assertEqual(counts["sessions_online"], 2)
        self.assertEqual(counts["sessions_total"], 2)


if __name__ == "__main__":
    unittest.main()
