"""Phase 2 chat backend: image input, search and file-checkpoint rewind."""

import base64
import json
import unittest

from tests.harness import TestServer

from opencloddie.auth import ApiIdentity
from opencloddie.chat import ChatError
from opencloddie.config import _validate, build_default_config


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send_text(self, text):
        self.sent.append(text)

    def close(self):
        self.closed = True

    def frames(self):
        return [json.loads(text) for text in self.sent]


class Phase2Base(unittest.TestCase):
    def setUp(self):
        self.server = TestServer()
        self.app = self.server.app
        self.db = self.app.db
        self.broker = self.app.chat
        self.hub = self.app.hub
        self.alice = self.db.ensure_user("alice")
        self.bob = self.db.ensure_user("bob")
        self.uid = self.alice["id"]
        self.sid = "s_chat"
        self.db.upsert_session(
            session_id=self.sid,
            user_id=self.uid,
            api_key_id="",
            name="work",
            hostname="laptop",
            cwd="/tmp/project",
            model="deepseek-v4.1-flash",
            agent_id="a_chat",
        )
        self.identity = ApiIdentity(
            user_id=self.uid,
            username="alice",
            key_id="k_alice",
            key_name="test",
            model="deepseek-v4.1-flash",
            opencode_key_enc="",
        )

    def tearDown(self):
        self.server.stop()

    def attach(self):
        ws = FakeWS()
        self.hub.attach(
            ws,
            self.identity,
            {
                "agent_id": "a_chat",
                "hostname": "laptop",
                "version": "0.6.0",
                "platform": "linux",
                "session": {"id": self.sid, "name": "work", "cwd": "/tmp/project"},
                "features": {"chat": True, "chat_version": 1},
            },
        )
        return ws

    @staticmethod
    def image_block(data: bytes, media_type: str = "image/png") -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }


class ConfigValidationTest(unittest.TestCase):
    def test_image_limits_have_defaults_and_clamps(self):
        cfg = build_default_config()
        _validate(cfg)
        chat = cfg["chat"]
        self.assertEqual(chat["max_image_bytes"], 5 * 1024 * 1024)
        self.assertEqual(chat["max_input_images"], 4)
        self.assertEqual(chat["max_inline_event_image_bytes"], 1024 * 1024)

        cfg = build_default_config()
        cfg["chat"]["max_image_bytes"] = 1
        cfg["chat"]["max_input_images"] = 999
        cfg["chat"]["max_inline_event_image_bytes"] = 10**9
        _validate(cfg)
        self.assertEqual(cfg["chat"]["max_image_bytes"], 1024)
        self.assertEqual(cfg["chat"]["max_input_images"], 16)
        self.assertEqual(
            cfg["chat"]["max_inline_event_image_bytes"], 8 * 1024 * 1024
        )

        cfg = build_default_config()
        cfg["chat"]["max_input_images"] = -5
        cfg["chat"]["max_inline_event_image_bytes"] = -1
        _validate(cfg)
        self.assertEqual(cfg["chat"]["max_input_images"], 0)
        self.assertEqual(cfg["chat"]["max_inline_event_image_bytes"], 0)


class ImageInputTest(Phase2Base):
    def test_content_blocks_are_forwarded_and_echoed_unchanged(self):
        ws = self.attach()
        viewer_id, q = self.broker.attach_viewer(self.sid)
        try:
            content = [
                {"type": "text", "text": "what is this?"},
                self.image_block(b"tiny-png"),
            ]
            result = self.broker.send_input(self.sid, self.uid, content, "img-1")
            self.assertEqual(result, {"ok": True, "client_msg_id": "img-1"})
            inputs = [f for f in ws.frames() if f.get("t") == "chat_input"]
            self.assertEqual(len(inputs), 1)
            self.assertEqual(inputs[0]["content"], content)
            echoed = q.get_nowait()
            self.assertEqual(echoed["t"], "input")
            self.assertEqual(echoed["content"], content)
        finally:
            self.broker.detach_viewer(self.sid, viewer_id)

    def test_image_validation_rejects_bad_blocks(self):
        self.attach()
        old_images = self.broker.cfg.get("max_input_images")
        old_bytes = self.broker.cfg.get("max_image_bytes")
        try:
            self.broker.cfg["max_input_images"] = 1
            with self.assertRaises(ChatError) as ctx:
                self.broker.send_input(
                    self.sid,
                    self.uid,
                    [self.image_block(b"a"), self.image_block(b"b")],
                    "too-many",
                )
            self.assertEqual(ctx.exception.status, 413)
            self.assertEqual(ctx.exception.code, "too_many_images")

            self.broker.cfg["max_input_images"] = 4
            self.broker.cfg["max_image_bytes"] = 2
            with self.assertRaises(ChatError) as ctx:
                self.broker.send_input(
                    self.sid, self.uid, [self.image_block(b"abc")], "too-big"
                )
            self.assertEqual(ctx.exception.status, 413)
        finally:
            if old_images is None:
                self.broker.cfg.pop("max_input_images", None)
            else:
                self.broker.cfg["max_input_images"] = old_images
            if old_bytes is None:
                self.broker.cfg.pop("max_image_bytes", None)
            else:
                self.broker.cfg["max_image_bytes"] = old_bytes

        bad_source = {
            "type": "image",
            "source": {"type": "url", "url": "https://example.test/x.png"},
        }
        cases = [
            [self.image_block(b"a", media_type="image/bmp")],
            [bad_source],
            [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "!!!not-base64!!!",
                    },
                }
            ],
            [{"type": "video", "source": {}}],
            [{"type": "text", "text": 5}],
            ["not-a-block"],
        ]
        for content in cases:
            with self.subTest(content=content):
                with self.assertRaises(ChatError) as ctx:
                    self.broker.send_input(self.sid, self.uid, content, "bad")
                self.assertEqual(ctx.exception.status, 400)


class EventNormalizationTest(Phase2Base):
    def _event(self, event_id, kind, payload):
        self.broker._on_event(
            self.sid,
            {
                "event_id": event_id,
                "kind": kind,
                "turn_id": "t1",
                "payload": payload,
            },
        )
        rows = self.db.chat_events_after(self.sid)
        return next(row for row in rows if row["event_id"] == event_id)

    def test_small_image_stays_inline_and_large_becomes_placeholder(self):
        old_limit = self.broker.cfg.get("max_inline_event_image_bytes")
        old_event = self.broker.max_event_bytes
        try:
            self.broker.cfg["max_inline_event_image_bytes"] = 4
            # Keep the per-event cap out of the way so this tests only the
            # inline/placeholder threshold.
            self.broker.max_event_bytes = 1024 * 1024

            small = self.image_block(b"abc")
            row = self._event(
                "ev-small", "user_message", {"content": [small], "uuid": "u1"}
            )
            payload = json.loads(row["payload"])
            self.assertEqual(payload["content"], [small])

            big = self.image_block(b"abcdefgh")
            row = self._event(
                "ev-big", "user_message", {"content": [big], "uuid": "u2"}
            )
            payload = json.loads(row["payload"])
            self.assertEqual(
                payload["content"],
                [
                    {
                        "type": "image",
                        "placeholder": True,
                        "media_type": "image/png",
                        "bytes": 8,
                    }
                ],
            )

            # tool_result images follow the same rule.
            row = self._event(
                "ev-tool",
                "tool_result",
                {"tool_use_id": "x", "content": [big], "is_error": False},
            )
            payload = json.loads(row["payload"])
            self.assertTrue(payload["content"][0]["placeholder"])
        finally:
            if old_limit is None:
                self.broker.cfg.pop("max_inline_event_image_bytes", None)
            else:
                self.broker.cfg["max_inline_event_image_bytes"] = old_limit
            self.broker.max_event_bytes = old_event

    def test_event_byte_cap_drops_images_that_do_not_fit(self):
        old_limit = self.broker.cfg.get("max_inline_event_image_bytes")
        old_event = self.broker.max_event_bytes
        try:
            self.broker.cfg["max_inline_event_image_bytes"] = 1024 * 1024
            self.broker.max_event_bytes = 120
            block = self.image_block(b"x" * 200)
            row = self._event(
                "ev-fit", "user_message", {"content": [block], "uuid": "u3"}
            )
            payload = json.loads(row["payload"])
            self.assertTrue(payload["content"][0]["placeholder"])
            self.assertEqual(payload["content"][0]["bytes"], 200)
        finally:
            if old_limit is None:
                self.broker.cfg.pop("max_inline_event_image_bytes", None)
            else:
                self.broker.cfg["max_inline_event_image_bytes"] = old_limit
            self.broker.max_event_bytes = old_event

    def test_checkpoint_event_is_recorded_and_deduped(self):
        row = self._event(
            "cp-event",
            "checkpoint",
            {"uuid": "cp-1", "label": "hello"},
        )
        rows = self.db.chat_checkpoints(self.sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["checkpoint_uuid"], "cp-1")
        self.assertEqual(rows[0]["event_seq"], row["seq"])

        # The same event id is a duplicate and must not add another row.
        self._event("cp-event", "checkpoint", {"uuid": "cp-1", "label": "hello"})
        self.assertEqual(len(self.db.chat_checkpoints(self.sid)), 1)


class SearchTest(Phase2Base):
    def _seed(self):
        events = [
            ("e1", "user_message", {"content": "Hello World"}),
            ("e2", "assistant_block", {"block": {"type": "text", "text": "hello again"}}),
            ("e3", "result", {"result": "nothing here"}),
            ("e4", "assistant_block", {"block": {"type": "text", "text": "HELLO there"}}),
        ]
        for event_id, kind, payload in events:
            self.db.append_chat_event(self.sid, self.uid, event_id, kind, payload)

    def test_db_search_is_case_insensitive_chronological_and_paged(self):
        self._seed()
        rows, has_more = self.db.search_chat_events(self.sid, "hello", 2)
        self.assertEqual([r["event_id"] for r in rows], ["e1", "e2"])
        self.assertTrue(has_more)
        rows, has_more = self.db.search_chat_events(self.sid, "hello", 50)
        self.assertEqual([r["event_id"] for r in rows], ["e1", "e2", "e4"])
        self.assertFalse(has_more)
        rows, _ = self.db.search_chat_events(self.sid, "HELLO", 50)
        self.assertEqual([r["event_id"] for r in rows], ["e1", "e2", "e4"])
        # LIKE wildcards in the query are matched literally.
        rows, has_more = self.db.search_chat_events(self.sid, "%", 50)
        self.assertEqual(rows, [])
        self.assertFalse(has_more)

    def test_search_rest_endpoint_is_owner_only(self):
        self._seed()
        status, _, data = self.server.web_json(
            "GET", f"/api/sessions/{self.sid}/chat/search?q=hello&limit=2"
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["query"], "hello")
        self.assertEqual([e["event_id"] for e in data["events"]], ["e1", "e2"])
        self.assertTrue(data["has_more"])

        status, _, _ = self.server.web_json(
            "GET",
            f"/api/sessions/{self.sid}/chat/search?q=hello",
            headers={"Remote-User": "bob"},
        )
        self.assertEqual(status, 404)

        status, _, data = self.server.web_json(
            "GET", f"/api/sessions/{self.sid}/chat/search?q="
        )
        self.assertEqual(status, 400)

    def test_get_chat_exposes_checkpoints(self):
        self.db.chat_checkpoint_add(self.sid, "cp-1", 7)
        status, _, data = self.server.web_json(
            "GET", f"/api/sessions/{self.sid}/chat"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            data["checkpoints"], [{"uuid": "cp-1", "event_seq": 7}]
        )


class RewindTest(Phase2Base):
    def setUp(self):
        super().setUp()
        self.db.chat_checkpoint_add(self.sid, "cp-1", 3)
        self.db.chat_state_upsert(self.sid, status="idle", mode="chat")

    def test_request_rewind_sends_frame_and_sets_state(self):
        ws = self.attach()
        self.broker.request_rewind(self.sid, self.uid, "cp-1")
        frames = [f for f in ws.frames() if f.get("t") == "chat_rewind"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["checkpoint_uuid"], "cp-1")
        self.assertFalse(frames[0]["force"])
        self.assertEqual(
            self.broker.public_state(self.sid, self.uid)["status"], "restarting"
        )

    def test_rewind_validation_busy_force_and_ownership(self):
        self.attach()
        with self.assertRaises(ChatError) as ctx:
            self.broker.request_rewind(self.sid, self.uid, "")
        self.assertEqual(ctx.exception.status, 400)

        with self.assertRaises(ChatError) as ctx:
            self.broker.request_rewind(self.sid, self.uid, "missing")
        self.assertEqual(ctx.exception.status, 404)

        with self.assertRaises(ChatError) as ctx:
            self.broker.request_rewind(self.sid, self.bob["id"], "cp-1")
        self.assertEqual(ctx.exception.status, 404)

        self.db.chat_state_upsert(self.sid, status="thinking")
        with self.assertRaises(ChatError) as ctx:
            self.broker.request_rewind(self.sid, self.uid, "cp-1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "busy")

        ws = self.attach()  # latest connection wins for the relay
        self.broker.request_rewind(self.sid, self.uid, "cp-1", force=True)
        frames = [f for f in ws.frames() if f.get("t") == "chat_rewind"]
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0]["force"])

    def test_rewind_rest_endpoint(self):
        self.attach()
        headers = {"X-OpenCloddie-Client": "web"}
        status, _, data = self.server.web_json(
            "POST",
            f"/api/sessions/{self.sid}/chat/rewind",
            body={"checkpoint_uuid": "cp-1", "force": True},
            headers=headers,
        )
        self.assertEqual(status, 202)
        self.assertTrue(data["ok"])

        status, _, _ = self.server.web_json(
            "POST",
            f"/api/sessions/{self.sid}/chat/rewind",
            body={},
            headers=headers,
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
