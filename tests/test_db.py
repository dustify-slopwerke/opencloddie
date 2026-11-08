import os
import tempfile
import unittest

from tests.harness import ROOT  # noqa: F401

from opencloddie.crypto import new_master_secret
from opencloddie.db import Database


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


if __name__ == "__main__":
    unittest.main()
