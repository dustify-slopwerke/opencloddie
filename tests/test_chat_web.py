"""HTTP and WebSocket tests for the browser chat API."""

import json
import time
import unittest

from tests.harness import TestServer, wait_for

from opencloddie.auth import ApiIdentity
from opencloddie.websocket import OP_TEXT, WebSocketClient

CSRF = {"X-OpenCloddie-Client": "web"}


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


class ChatWebTest(unittest.TestCase):
    def setUp(self):
        self.server = TestServer()
        self.app = self.server.app
        self.db = self.app.db
        self.chat = self.app.chat
        self.alice = self.db.ensure_user(
            "alice",
            email="alice@example.com",
            name="Alice Example",
            groups=["users", "god"],
        )
        self.bob = self.db.ensure_user("bob")
        self.uid = self.alice["id"]
        self.sid = "s_chat"
        self.identity = ApiIdentity(
            user_id=self.uid,
            username="alice",
            key_id="k_alice",
            key_name="test",
            model="deepseek-v4.1-flash",
            opencode_key_enc="",
        )
        self.agent_ws = FakeWS()
        self.conn = self.app.hub.attach(
            self.agent_ws,
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

    def tearDown(self):
        self.server.stop()

    # --------------------------------------------------------------- helpers
    def web_json(self, method, path, body=None, *, csrf=None, user="alice", headers=None):
        merged = dict(headers or {})
        if user != "alice":
            merged["Remote-User"] = user
        if csrf is None:
            csrf = method not in ("GET", "HEAD")
        if csrf:
            merged["X-OpenCloddie-Client"] = "web"
        return self.server.web_json(method, path, body=body, headers=merged)

    def seed_event(self, event_id="e1", kind="assistant_block", payload=None):
        self.db.chat_state_upsert(self.sid)
        seq, _ = self.db.append_chat_event(
            self.sid, self.uid, event_id, kind, payload or {}
        )
        return seq

    def warm_event_log(self):
        """Advance the event AUTOINCREMENT past seq 1.

        httpd._int_param clamps an absent ``after_seq`` to 1, so the GET/WS
        replay path currently hides the first event of a session (reported as a
        production bug).  Tests that exercise the rest of the replay shape use
        this so their first real event has seq > 1; the bug itself is covered
        by test_chat_get_replays_first_event_without_after_seq.
        """
        self.db.chat_state_upsert(self.sid)
        seq, _ = self.db.append_chat_event(
            self.sid, self.uid, "__warm__", "status", {}
        )
        with self.db.conn:
            self.db.conn.execute(
                "DELETE FROM chat_events WHERE session_id=? AND event_id=?",
                (self.sid, "__warm__"),
            )
        return seq

    def seed_permission(self, permission_id="p1", tool_name="Bash"):
        return self.db.chat_permission_create(
            {
                "id": permission_id,
                "session_id": self.sid,
                "turn_id": "t1",
                "tool_name": tool_name,
                "tool_input": json.dumps({"command": "ls"}),
                "suggestions": json.dumps([{"type": "allow_once"}]),
                "status": "pending",
            }
        )

    def agent_frames(self, kind=None):
        frames = self.agent_ws.frames()
        return [frame for frame in frames if kind is None or frame.get("t") == kind]

    def ws_connect(self, *, user="alice"):
        url = (
            f"ws://127.0.0.1:{self.server.web_port}"
            f"/api/sessions/{self.sid}/chat/ws"
        )
        return WebSocketClient.connect(
            url, headers={"Remote-User": user}, timeout=5
        )

    @staticmethod
    def recv_until(ws, kind, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            opcode, payload = ws.recv()
            if opcode != OP_TEXT:
                continue
            message = json.loads(payload)
            if message.get("t") == kind:
                return message
        return None

    # ------------------------------------------------------------------- GET
    def test_get_chat_returns_state_events_and_pending_for_owner(self):
        self.warm_event_log()
        self.db.chat_state_upsert(
            self.sid,
            mode="chat",
            status="idle",
            model="deepseek-v4.1-flash",
            capabilities={"interrupt_receipt_v1": True},
        )
        seq1, _ = self.db.append_chat_event(
            self.sid, self.uid, "e1", "assistant_block", {"text": "first"}
        )
        seq2, _ = self.db.append_chat_event(
            self.sid, self.uid, "e2", "result", {"subtype": "success"}
        )
        self.seed_permission("p1")

        status, _, data = self.web_json("GET", f"/api/sessions/{self.sid}/chat")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["chat"]["mode"], "chat")
        self.assertEqual(data["chat"]["status"], "idle")
        self.assertEqual(data["chat"]["model"], "deepseek-v4.1-flash")
        self.assertEqual(data["chat"]["capabilities"], {"interrupt_receipt_v1": True})
        self.assertEqual(data["chat"]["pending_count"], 1)
        self.assertEqual(data["last_seq"], seq2)
        self.assertFalse(data["has_more"])

        self.assertEqual([event["event_id"] for event in data["events"]], ["e1", "e2"])
        self.assertEqual(data["events"][0]["seq"], seq1)
        self.assertEqual(data["events"][0]["payload"], {"text": "first"})
        self.assertEqual([item["id"] for item in data["pending"]], ["p1"])
        self.assertEqual(data["pending"][0]["input"], {"command": "ls"})
        self.assertEqual(data["pending"][0]["suggestions"], [{"type": "allow_once"}])

        # after_seq pages forward from a known sequence.
        status, _, page = self.web_json(
            "GET", f"/api/sessions/{self.sid}/chat?after_seq={seq1}"
        )
        self.assertEqual(status, 200)
        self.assertEqual([event["event_id"] for event in page["events"]], ["e2"])

        # Another user cannot read the session at all.
        status, _, _ = self.web_json(
            "GET", f"/api/sessions/{self.sid}/chat", user="bob"
        )
        self.assertEqual(status, 404)

    def test_unknown_session_is_404(self):
        status, _, data = self.web_json("GET", "/api/sessions/s_nope/chat")
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])

    def test_chat_get_replays_first_event_without_after_seq(self):
        # docs/CHAT-PROTOCOL.md says GET /chat returns events ascending and
        # defaults to the latest page when after_seq is absent.
        seq, _ = self.db.append_chat_event(
            self.sid, self.uid, "e1", "assistant_block", {}
        )
        status, _, data = self.web_json("GET", f"/api/sessions/{self.sid}/chat")
        self.assertEqual(status, 200)
        self.assertEqual([event["event_id"] for event in data["events"]], ["e1"])
        self.assertFalse(data["has_more"])
        self.assertEqual(data["events"][0]["seq"], seq)

    # ------------------------------------------------------------------ CSRF
    def test_mutations_require_csrf_header_and_same_origin(self):
        status, _, data = self.web_json(
            "POST",
            f"/api/sessions/{self.sid}/chat/input",
            body={"content": "hi", "client_msg_id": "m1"},
            csrf=False,
        )
        self.assertEqual(status, 403)
        self.assertIn("X-OpenCloddie-Client", data["error"]["message"])

        status, _, data = self.web_json(
            "POST",
            f"/api/sessions/{self.sid}/chat/input",
            body={"content": "hi", "client_msg_id": "m2"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", data["error"]["message"])

        same_origin = f"http://127.0.0.1:{self.server.web_port}"
        status, _, data = self.web_json(
            "POST",
            f"/api/sessions/{self.sid}/chat/input",
            body={"content": "hi", "client_msg_id": "m3"},
            headers={"Origin": same_origin},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["client_msg_id"], "m3")

    # ------------------------------------------------------------- mutations
    def test_input_interrupt_mode_read_permission_and_history(self):
        path = f"/api/sessions/{self.sid}/chat"

        status, _, data = self.web_json(
            "POST", f"{path}/input", body={"content": "hello", "client_msg_id": "m1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "client_msg_id": "m1"})
        inputs = self.agent_frames("chat_input")
        self.assertEqual(inputs[-1]["client_msg_id"], "m1")
        self.assertEqual(inputs[-1]["content"], "hello")

        status, _, data = self.web_json(
            "POST", f"{path}/input", body={"content": "hello", "client_msg_id": "m1"}
        )
        self.assertEqual(status, 200)
        self.assertIs(data["duplicate"], True)
        self.assertEqual(len(self.agent_frames("chat_input")), 1)

        status, _, data = self.web_json("POST", f"{path}/interrupt")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        self.assertTrue(self.agent_frames("chat_interrupt")[-1]["request_id"])

        status, _, data = self.web_json(
            "POST", f"{path}/mode", body={"mode": "chat", "force": True}
        )
        self.assertEqual(status, 202)
        self.assertEqual(data, {"ok": True})
        handoff = self.agent_frames("chat_handoff")[-1]
        self.assertEqual(handoff["to"], "chat")
        self.assertIs(handoff["force"], True)
        status, _, chat = self.web_json("GET", path)
        self.assertEqual(chat["chat"]["mode"], "handoff")
        self.assertEqual(chat["chat"]["status"], "handoff")

        # An invalid mode is a client error, not a silent handoff.
        status, _, data = self.web_json(
            "POST", f"{path}/mode", body={"mode": "bogus"}
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        status, _, data = self.web_json(
            "POST", f"{path}/config", body={"permission_mode": "plan"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(self.agent_frames("chat_config")[-1]["permission_mode"], "plan")
        status, _, data = self.web_json(
            "POST", f"{path}/config", body={"permission_mode": "bogus"}
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        self.chat.on_agent_frame(
            self.conn,
            {
                "t": "chat_event",
                "event_id": "e1",
                "kind": "assistant_block",
                "payload": {},
            },
        )
        seq = self.db.chat_events_after(self.sid)[-1]["seq"]
        status, _, data = self.web_json(
            "POST", f"{path}/read", body={"last_seq": seq}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(self.db.chat_read_get(self.uid, self.sid), seq)

        self.chat.on_agent_frame(
            self.conn,
            {
                "t": "chat_permission_request",
                "permission_id": "p1",
                "tool_name": "Bash",
                "input": {"command": "ls"},
            },
        )
        status, _, data = self.web_json(
            "POST", f"{path}/permissions/p1", body={"behavior": "allow"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "status": "allowed"})
        result = self.agent_frames("chat_permission_result")[-1]
        self.assertEqual(result["permission_id"], "p1")
        self.assertEqual(result["result"]["behavior"], "allow")

        # Clicking twice is an idempotent duplicate.
        status, _, data = self.web_json(
            "POST", f"{path}/permissions/p1", body={"behavior": "deny"}
        )
        self.assertEqual(status, 200)
        self.assertIs(data["duplicate"], True)
        self.assertEqual(data["status"], "allowed")

        # Unknown permissions are 404 for this session.
        status, _, _ = self.web_json(
            "POST", f"{path}/permissions/p_missing", body={"behavior": "allow"}
        )
        self.assertEqual(status, 404)

        status, _, data = self.web_json("DELETE", f"{path}/history")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted"], 3)  # block + permission + resolved
        status, _, chat = self.web_json("GET", path)
        self.assertEqual(chat["events"], [])
        self.assertEqual(chat["pending"], [])
        self.assertEqual(chat["last_seq"], 0)
        self.assertEqual(chat["chat"]["pending_count"], 0)

    def test_mutations_on_another_users_session_are_404(self):
        path = f"/api/sessions/{self.sid}/chat"
        for method, suffix, body in (
            ("POST", "/input", {"content": "hi"}),
            ("POST", "/interrupt", None),
            ("POST", "/mode", {"mode": "chat"}),
            ("POST", "/read", {"last_seq": 1}),
            ("DELETE", "/history", None),
        ):
            status, _, _ = self.web_json(
                method, f"{path}{suffix}", body=body, user="bob"
            )
            self.assertEqual(status, 404, f"{method} {suffix}")

    # ------------------------------------------------------- session summary
    def test_session_list_includes_chat_summary(self):
        self.db.chat_state_upsert(
            self.sid, mode="chat", status="thinking", model="deepseek-v4.1-flash"
        )
        seq = self.seed_event("e1", "assistant_block", {"text": "hi"})
        self.seed_permission("p1")

        status, _, data = self.web_json("GET", "/api/sessions")
        self.assertEqual(status, 200)
        summary = next(
            row for row in data["sessions"] if row["id"] == self.sid
        )
        self.assertTrue(summary["online"])
        self.assertEqual(
            summary["chat"],
            {
                "mode": "chat",
                "status": "thinking",
                "pending": 1,
                "unread": 1,
                "last_seq": seq,
            },
        )

        # Reading the event clears the chat unread badge.
        self.web_json(
            "POST", f"/api/sessions/{self.sid}/chat/read", body={"last_seq": seq}
        )
        status, _, data = self.web_json("GET", "/api/sessions")
        summary = next(
            row for row in data["sessions"] if row["id"] == self.sid
        )
        self.assertEqual(summary["chat"]["unread"], 0)

    # -------------------------------------------------------------- websocket
    def test_websocket_hello_replay_ping_input_and_permission(self):
        self.warm_event_log()
        self.db.chat_state_upsert(self.sid, mode="chat", status="idle")
        seq = self.seed_event("e1", "assistant_block", {"text": "backlog"})
        self.seed_permission("p1")

        ws = self.ws_connect()
        try:
            opcode, payload = ws.recv()
            self.assertEqual(opcode, OP_TEXT)
            hello = json.loads(payload)
            self.assertEqual(hello["t"], "hello")
            self.assertEqual(hello["session"]["id"], self.sid)
            self.assertEqual(hello["chat"]["mode"], "chat")
            self.assertEqual(hello["chat"]["status"], "idle")
            self.assertEqual(hello["chat"]["pending_count"], 1)
            self.assertEqual(hello["last_seq"], seq)
            self.assertEqual([event["event_id"] for event in hello["events"]], ["e1"])
            self.assertEqual([item["id"] for item in hello["pending"]], ["p1"])

            ws.send_text(json.dumps({"t": "ping"}))
            self.assertIsNotNone(self.recv_until(ws, "pong"))

            ws.send_text(
                json.dumps({"t": "input", "client_msg_id": "w1", "content": "hi"})
            )
            echo = self.recv_until(ws, "input")
            self.assertIsNotNone(echo)
            self.assertEqual(echo["client_msg_id"], "w1")
            self.assertEqual(self.agent_frames("chat_input")[-1]["content"], "hi")

            ws.send_text(json.dumps({"t": "read", "last_seq": seq}))
            self.assertTrue(
                wait_for(
                    lambda: self.db.chat_read_get(self.uid, self.sid) == seq,
                    timeout=3,
                )
            )

            ws.send_text(
                json.dumps(
                    {"t": "permission", "permission_id": "p1", "behavior": "deny"}
                )
            )
            self.assertTrue(
                wait_for(
                    lambda: (self.db.chat_permission_get("p1") or {}).get("status")
                    == "denied",
                    timeout=3,
                )
            )
            resolved = self.recv_until(ws, "permission_resolved")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["id"], "p1")
        finally:
            ws.close()

    def test_websocket_rejects_cross_origin_and_other_users(self):
        path = f"/api/sessions/{self.sid}/chat/ws"
        upgrade = {
            "Remote-User": "alice",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Version": "13",
        }

        status, _, data = self.server.request(
            self.server.web_port,
            "GET",
            path,
            headers={**upgrade, "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", json.loads(data)["error"]["message"])

        status, _, data = self.server.request(
            self.server.web_port,
            "GET",
            path,
            headers={**upgrade, "Remote-User": "bob"},
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
