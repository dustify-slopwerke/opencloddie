import base64
import time
import unittest

from tests.harness import TestServer

from opencloddie.auth import ApiIdentity
from opencloddie.metrics import Metrics, SSEUsageParser, percentile, process_snapshot, usage_from_json


class MetricsTest(unittest.TestCase):
    def test_counters_are_idempotent_and_pending_flushes_once(self):
        metrics = Metrics(started_at=1000.0)
        token = metrics.begin(
            user_id=1, key_id="k1", key_name="laptop", username="alice", bytes_in=10
        )
        metrics.set_model(token, "model-a")
        metrics.finish(
            token,
            200,
            bytes_out=20,
            tokens_in=3,
            tokens_out=4,
            cache_read_tokens=1,
            cache_creation_tokens=2,
            upstream_ms=12.5,
        )
        # A second finish (for example close after read_all) must not double-count.
        metrics.finish(token, 200, bytes_out=999)

        snapshot = metrics.snapshot(1)
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["in_flight"], 0)
        self.assertEqual(snapshot["ok"], 1)
        self.assertEqual(snapshot["bytes_in"], 10)
        self.assertEqual(snapshot["bytes_out"], 20)
        self.assertEqual(snapshot["tokens"], {
            "input": 3,
            "output": 4,
            "cache_read": 1,
            "cache_creation": 2,
        })
        self.assertEqual(snapshot["upstream_ms"]["p50"], 12.5)
        self.assertEqual(snapshot["by_status"], {"200": 1})
        self.assertEqual(len(snapshot["by_key"]), 1)
        self.assertEqual(snapshot["by_key"][0]["id"], "k1")
        self.assertEqual(snapshot["by_model"][0]["model"], "model-a")

        pending = metrics.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].requests, 1)
        self.assertEqual(pending[0].tokens_out, 4)
        self.assertEqual(metrics.pending(), [])

    def test_scope_and_error_classification(self):
        metrics = Metrics(started_at=1000.0)
        alice = metrics.begin(user_id=1, key_id="k1", key_name="a", username="alice", bytes_in=1)
        metrics.finish(alice, 400)
        bob = metrics.begin(user_id=2, key_id="k2", key_name="b", username="bob", bytes_in=1)
        metrics.finish(bob, 500)
        limited = metrics.begin(user_id=1, key_id="k1", key_name="a", username="alice", bytes_in=1)
        metrics.finish(limited, 429, rate_limited=True)

        mine = metrics.snapshot(1)
        self.assertEqual(mine["requests"], 2)
        self.assertEqual(mine["client_errors"], 1)
        self.assertEqual(mine["server_errors"], 0)
        self.assertEqual(mine["rate_limited"], 1)
        self.assertEqual([row["id"] for row in mine["by_key"]], ["k1"])

        everyone = metrics.snapshot(None)
        self.assertEqual(everyone["requests"], 3)
        self.assertEqual(everyone["server_errors"], 1)
        self.assertEqual(len(everyone["by_key"]), 2)

    def test_sse_usage_parser_handles_split_chunks(self):
        parser = SSEUsageParser()
        first = (
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"usage":{"input_tokens":7,"cache_read_input_tokens":2}}}\n\n'
        ).encode()
        second = (
            'event: message_delta\ndata: {"type":'
            '"message_delta","usage":{"output_tokens":5}}\n\n'
        ).encode()
        parser.feed(first[:60])
        parser.feed(first[60:] + second)
        usage = parser.usage()
        self.assertEqual(usage["tokens_in"], 7)
        self.assertEqual(usage["tokens_out"], 5)
        self.assertEqual(usage["cache_read_tokens"], 2)

    def test_usage_from_json(self):
        usage = usage_from_json(
            b'{"usage":{"input_tokens":11,"output_tokens":2,'
            b'"cache_creation_input_tokens":4}}'
        )
        self.assertEqual(usage["tokens_in"], 11)
        self.assertEqual(usage["tokens_out"], 2)
        self.assertEqual(usage["cache_creation_tokens"], 4)
        self.assertEqual(usage_from_json(b"not json"), {})

    def test_percentile(self):
        self.assertEqual(percentile([], 50), 0.0)
        self.assertEqual(percentile([5], 95), 5.0)
        self.assertEqual(percentile([0, 10], 50), 5.0)
        self.assertEqual(percentile([0, 10, 20], 100), 20.0)

    def test_process_snapshot_degrades_gracefully(self):
        snapshot = process_snapshot(started_at=time.time() - 5, db_path="/tmp/opencloddie-test.db")
        for key in ("uptime_seconds", "pid", "python", "platform", "threads", "memory", "cpu"):
            self.assertIn(key, snapshot)
        self.assertGreaterEqual(snapshot["uptime_seconds"], 4)
        self.assertIsInstance(snapshot["memory"]["rss_bytes"], (int, type(None)))


class HubMetricsTest(unittest.TestCase):
    class FakeWS:
        def __init__(self):
            self.sent = []
            self.closed = False

        def send_text(self, text):
            self.sent.append(text)

        def close(self):
            self.closed = True

    def setUp(self):
        self.server = TestServer()
        token, row = self.server.create_user_key("alice")
        user = self.server.app.db.user_by_id(row["user_id"])
        self.identity = ApiIdentity(
            user_id=user["id"],
            username=user["username"],
            key_id=row["id"],
            key_name=row["name"],
            model=row["model"],
            opencode_key_enc="",
            key_row=row,
        )
        self.ws = self.FakeWS()

    def tearDown(self):
        self.server.stop()

    def test_hub_counters_snapshot_and_scope(self):
        hub = self.server.app.hub
        conn = hub.attach(self.ws, self.identity, {
            "agent_id": "a-test",
            "hostname": "laptop",
            "version": "0.3.0",
            "platform": "linux",
            "session": {"id": "s_test", "name": "work", "cwd": "/tmp/project"},
        })
        channel = conn.open_channel(80, 24)
        channel.send_input(b"abc")
        conn.dispatch({
            "t": "out",
            "ch": channel.id,
            "data": base64.b64encode(b"hello").decode("ascii"),
        })

        snapshot = hub.snapshot(self.identity.user_id)
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["name"], "work")
        self.assertEqual(snapshot[0]["viewers"], 1)
        self.assertEqual(snapshot[0]["terminal_bytes_in"], 3)
        self.assertEqual(snapshot[0]["terminal_bytes_out"], 5)
        self.assertIsNotNone(snapshot[0]["last_seen_at"])
        self.assertEqual(hub.snapshot(self.identity.user_id + 1), [])

        stats = hub.stats()
        self.assertEqual(stats["agents"], 1)
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["viewers"], 1)
        self.assertEqual(stats["channels_opened_total"], 1)
        self.assertEqual(stats["terminal_bytes_in_total"], 3)
        self.assertEqual(stats["terminal_bytes_out_total"], 5)
        self.assertEqual(stats["agents_connected_total"], 1)
        self.assertEqual(stats["agents_disconnected_total"], 0)

        conn.close(reason="test")
        stats = hub.stats()
        self.assertEqual(stats["agents"], 0)
        self.assertEqual(stats["agents_disconnected_total"], 1)
        self.assertTrue(self.ws.closed)


if __name__ == "__main__":
    unittest.main()
