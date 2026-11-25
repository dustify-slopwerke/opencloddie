"""Tests for the in-process ChatBroker (agent relay, viewers, commands)."""

import json
import queue
import unittest

from tests.harness import TestServer, wait_for

from opencloddie.auth import ApiIdentity
from opencloddie.chat import ChatError


class FakeWS:
    """Minimal agent WebSocket: every send_json lands here as JSON text."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send_text(self, text):
        self.sent.append(text)

    def close(self):
        self.closed = True

    def frames(self):
        return [json.loads(text) for text in self.sent]


class ChatBrokerTest(unittest.TestCase):
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

    # --------------------------------------------------------------- helpers
    def attach(self, *, session_id=None, agent_id=None, features=None, identity=None):
        session_id = session_id or self.sid
        agent_id = agent_id or f"a_{session_id}"
        if features is None:
            features = {"chat": True, "chat_version": 1}
        ws = FakeWS()
        conn = self.hub.attach(
            ws,
            identity or self.identity,
            {
                "agent_id": agent_id,
                "hostname": "laptop",
                "version": "0.6.0",
                "platform": "linux",
                "session": {"id": session_id, "name": "work", "cwd": "/tmp/project"},
                "features": features,
            },
        )
        return conn

    def viewer(self, session_id=None):
        return self.broker.attach_viewer(session_id or self.sid)

    @staticmethod
    def drain(q):
        frames = []
        while True:
            try:
                frames.append(q.get_nowait())
            except queue.Empty:
                return frames

    @staticmethod
    def by_type(frames, kind):
        return [frame for frame in frames if frame.get("t") == kind]

    def wait_frames(self, q, kind, timeout=3.0):
        collected = []

        def found():
            collected.extend(self.drain(q))
            return any(frame.get("t") == kind for frame in collected)

        self.assertTrue(wait_for(found, timeout=timeout), f"no {kind}: {collected}")
        return collected

    # ---------------------------------------------------------- public state
    def test_public_state_defaults_and_permission_event_shapes(self):
        state = self.broker.public_state("s_missing", self.uid)
        self.assertEqual(state["mode"], "tui")
        self.assertEqual(state["status"], "offline")
        self.assertEqual(state["permission_mode"], "default")
        self.assertEqual(state["last_seq"], 0)
        self.assertEqual(state["pending_count"], 0)
        self.assertEqual(state["unread"], 0)
        self.assertEqual(state["capabilities"], {})
        self.assertEqual(state["cost_usd"], 0.0)
        self.assertIsNone(state["context_tokens"])

        self.db.chat_state_upsert(
            self.sid,
            mode="chat",
            status="idle",
            model="m",
            capabilities={"interrupt_receipt_v1": True},
            cost_usd=0.5,
            tokens_in=3,
            tokens_out=4,
            context_tokens=99,
        )
        self.db.append_chat_event(self.sid, self.uid, "e1", "assistant_block", {})
        self.db.chat_permission_create(
            {
                "id": "p1",
                "session_id": self.sid,
                "tool_name": "Bash",
                "tool_input": json.dumps({"command": "ls"}),
                "suggestions": json.dumps([{"type": "allow_once"}]),
            }
        )
        state = self.broker.public_state(self.sid, self.uid)
        self.assertEqual(state["mode"], "chat")
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["capabilities"], {"interrupt_receipt_v1": True})
        self.assertEqual(state["tokens_in"], 3)
        self.assertEqual(state["tokens_out"], 4)
        self.assertEqual(state["context_tokens"], 99)
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(state["unread"], 1)  # the assistant_block

        permission = self.broker.public_permission(
            self.db.chat_permission_get("p1")
        )
        self.assertEqual(permission["id"], "p1")
        self.assertEqual(permission["input"], {"command": "ls"})
        self.assertEqual(permission["suggestions"], [{"type": "allow_once"}])
        self.assertEqual(permission["status"], "pending")
        self.assertIsNone(permission["decision"])

        row = self.db.chat_events_after(self.sid)[0]
        event = self.broker.public_event(row)
        self.assertEqual(event["seq"], row["seq"])
        self.assertEqual(event["session_id"], self.sid)
        self.assertEqual(event["kind"], "assistant_block")
        self.assertEqual(event["payload"], {})
        self.assertIsInstance(event["seq"], int)

    # -------------------------------------------------------------- relay in
    def test_event_is_stored_broadcast_and_deduped(self):
        conn = self.attach()
        _, q = self.viewer()

        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_event",
                "event_id": "e1",
                "kind": "assistant_block",
                "turn_id": "t1",
                "payload": {"message_id": "m1", "block_index": 0},
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        self.assertEqual(self.db.chat_event_count(self.sid), 1)
        self.assertEqual(
            self.db.chat_state_get(self.sid)["last_seq"],
            self.db.chat_events_after(self.sid)[0]["seq"],
        )

        frames = self.drain(q)
        events = self.by_type(frames, "event")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "e1")
        self.assertEqual(events[0]["kind"], "assistant_block")
        self.assertEqual(events[0]["payload"]["message_id"], "m1")
        self.assertEqual(events[0]["turn_id"], "t1")

        # The same event_id (a runner replay) must not be stored or broadcast.
        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_event",
                "event_id": "e1",
                "kind": "result",
                "payload": {"duplicate": True},
            },
        )
        self.assertEqual(self.db.chat_event_count(self.sid), 1)
        self.assertEqual(self.drain(q), [])

    def test_delta_is_transient_and_state_updates_public_state(self):
        conn = self.attach()
        _, q = self.viewer()

        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_delta",
                "turn_id": "t1",
                "parent_tool_use_id": None,
                "message_id": "m1",
                "block_index": 0,
                "delta": {"type": "text_delta", "text": "hel"},
            },
        )
        frames = self.drain(q)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["t"], "delta")
        self.assertEqual(frames[0]["delta"]["text"], "hel")
        # Deltas are never persisted.
        self.assertEqual(self.db.chat_event_count(self.sid), 0)

        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_state",
                "status": "thinking",
                "turn_id": "t2",
                "model": "deepseek-v4.1-flash",
                "permission_mode": "acceptEdits",
                "claude_session_id": "c1",
                "cost_usd": 0.75,
                "usage": {"input_tokens": 11, "output_tokens": 22},
                "context_tokens": 333,
                "capabilities": {"interrupt_receipt_v1": True},
            },
        )
        state = self.broker.public_state(self.sid, self.uid)
        self.assertEqual(state["status"], "thinking")
        self.assertEqual(state["turn_id"], "t2")
        self.assertEqual(state["permission_mode"], "acceptEdits")
        self.assertEqual(state["claude_session_id"], "c1")
        self.assertEqual(state["cost_usd"], 0.75)
        self.assertEqual(state["tokens_in"], 11)
        self.assertEqual(state["tokens_out"], 22)
        self.assertEqual(state["context_tokens"], 333)
        self.assertEqual(state["capabilities"], {"interrupt_receipt_v1": True})

        state_frames = self.by_type(self.drain(q), "state")
        self.assertEqual(len(state_frames), 1)
        self.assertEqual(state_frames[0]["chat"]["status"], "thinking")
        self.assertEqual(state_frames[0]["chat"]["tokens_in"], 11)

    def test_chat_hello_restores_state_and_pending_permissions(self):
        conn = self.attach()
        _, q = self.viewer()

        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_hello",
                "run_id": "cr_1",
                "claude_session_id": "c1",
                "model": "deepseek-v4.1-flash",
                "permission_mode": "default",
                "status": "idle",
                "capabilities": {"interrupt_receipt_v1": True},
                "last_local_seq": 7,
                "pending": [
                    {
                        "id": "p1",
                        "permission_id": "p1",
                        "turn_id": "t1",
                        "tool_name": "Bash",
                        "input": {"command": "ls"},
                        "suggestions": [],
                    }
                ],
            },
        )

        state = self.db.chat_state_get(self.sid)
        self.assertEqual(state["run_id"], "cr_1")
        self.assertEqual(state["claude_session_id"], "c1")
        self.assertEqual(state["status"], "idle")
        self.assertEqual(json.loads(state["capabilities"]), {"interrupt_receipt_v1": True})
        self.assertEqual(state["pending_count"], 1)

        permission = self.db.chat_permission_get("p1")
        self.assertIsNotNone(permission)
        self.assertEqual(permission["session_id"], self.sid)
        self.assertEqual(permission["status"], "pending")

        frames = self.wait_frames(q, "state")
        states = self.by_type(frames, "state")
        self.assertEqual(states[-1]["chat"]["run_id"], "cr_1")
        # The pending permission also produces its durable permission event.
        self.assertTrue(self.by_type(frames, "event"))

    def test_chat_hello_accepts_documented_permission_id_pending(self):
        # docs/CHAT-PROTOCOL.md documents pending entries keyed by
        # permission_id.
        conn = self.attach()
        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_hello",
                "run_id": "cr_1",
                "status": "idle",
                "pending": [
                    {
                        "permission_id": "p_doc",
                        "tool_name": "Bash",
                        "input": {"command": "ls"},
                        "suggestions": [],
                    }
                ],
            },
        )
        self.assertIsNotNone(self.db.chat_permission_get("p_doc"))

    # ----------------------------------------------------------- permissions
    def test_permission_request_creates_row_event_and_broadcast(self):
        conn = self.attach()
        _, q = self.viewer()

        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_permission_request",
                "permission_id": "p1",
                "turn_id": "t1",
                "request_id": "r1",
                "tool_name": "Bash",
                "input": {"command": "rm -rf /tmp/x"},
                "suggestions": [{"type": "allow_once"}],
            },
        )

        row = self.db.chat_permission_get("p1")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["session_id"], self.sid)
        self.assertEqual(json.loads(row["tool_input"]), {"command": "rm -rf /tmp/x"})
        self.assertEqual(json.loads(row["suggestions"]), [{"type": "allow_once"}])
        self.assertEqual(self.db.chat_state_get(self.sid)["pending_count"], 1)

        events = self.db.chat_events_after(self.sid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "permission")
        self.assertEqual(events[0]["event_id"], "perm:p1:request")
        self.assertEqual(json.loads(events[0]["payload"])["permission_id"], "p1")

        frames = self.drain(q)
        permissions = self.by_type(frames, "permission")
        self.assertEqual(len(permissions), 1)
        self.assertEqual(permissions[0]["permission"]["id"], "p1")
        self.assertEqual(permissions[0]["permission"]["tool_name"], "Bash")
        events = self.by_type(frames, "event")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "permission")

    def test_permission_timeout_expires_denies_and_notifies_agent(self):
        old_timeout = self.broker.permission_timeout
        self.broker.permission_timeout = 0.1
        try:
            conn = self.attach()
            _, q = self.viewer()
            self.broker.on_agent_frame(
                conn,
                {
                    "t": "chat_permission_request",
                    "permission_id": "p_timeout",
                    "tool_name": "Bash",
                    "input": {"command": "ls"},
                },
            )
            self.assertEqual(
                self.db.chat_permission_get("p_timeout")["status"], "pending"
            )

            self.assertTrue(
                wait_for(
                    lambda: (
                        self.db.chat_permission_get("p_timeout") or {}
                    ).get("status")
                    == "expired",
                    timeout=3,
                ),
                "permission did not expire",
            )
            self.assertTrue(
                wait_for(
                    lambda: any(
                        frame.get("t") == "chat_permission_result"
                        for frame in conn.ws.frames()
                    ),
                    timeout=3,
                ),
                "agent was not told about the timeout",
            )
            results = [
                frame
                for frame in conn.ws.frames()
                if frame.get("t") == "chat_permission_result"
            ]
            self.assertEqual(results[-1]["permission_id"], "p_timeout")
            self.assertEqual(results[-1]["result"]["behavior"], "deny")

            resolved = self.by_type(
                self.wait_frames(q, "permission_resolved"), "permission_resolved"
            )
            self.assertEqual(resolved[-1]["id"], "p_timeout")
            self.assertEqual(resolved[-1]["status"], "expired")

            self.assertTrue(
                wait_for(
                    lambda: any(
                        event["kind"] == "permission_resolved"
                        for event in self.db.chat_events_after(self.sid)
                    ),
                    timeout=3,
                ),
                "permission_resolved event was not stored",
            )
            events = self.db.chat_events_after(self.sid)
            self.assertEqual(
                [event["kind"] for event in events],
                ["permission", "permission_resolved"],
            )
            self.assertEqual(self.db.chat_state_get(self.sid)["pending_count"], 0)
        finally:
            self.broker.permission_timeout = old_timeout

    def test_decide_permission_allow_deny_idempotent_and_owner_checks(self):
        conn = self.attach()
        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_permission_request",
                "permission_id": "p1",
                "tool_name": "Bash",
                "input": {"command": "ls"},
            },
        )

        denied = self.broker.decide_permission(
            self.sid, self.uid, "p1", {"behavior": "deny", "message": "not now"}
        )
        self.assertEqual(denied, {"ok": True, "status": "denied"})
        row = self.db.chat_permission_get("p1")
        self.assertEqual(row["status"], "denied")
        self.assertEqual(row["resolved_by"], "work")
        self.assertTrue(row["delivered_at"])
        results = [
            frame
            for frame in conn.ws.frames()
            if frame.get("t") == "chat_permission_result"
        ]
        self.assertEqual(results[-1]["result"]["behavior"], "deny")
        self.assertEqual(results[-1]["result"]["message"], "not now")

        # The decision is durable; a second click is a no-op duplicate.
        again = self.broker.decide_permission(
            self.sid, self.uid, "p1", {"behavior": "allow"}
        )
        self.assertEqual(again, {"ok": True, "status": "denied", "duplicate": True})
        self.assertEqual(
            len(
                [
                    frame
                    for frame in conn.ws.frames()
                    if frame.get("t") == "chat_permission_result"
                ]
            ),
            1,
        )

        # A non-owner cannot even see the session.
        with self.assertRaises(ChatError) as ctx:
            self.broker.decide_permission(
                self.sid, self.bob["id"], "p1", {"behavior": "allow"}
            )
        self.assertEqual(ctx.exception.status, 404)

        with self.assertRaises(ChatError) as ctx:
            self.broker.decide_permission(
                self.sid, self.uid, "p_missing", {"behavior": "allow"}
            )
        self.assertEqual(ctx.exception.status, 404)

        # A permission id only belongs to the session that created it.
        self.attach(session_id="s_other", agent_id="a_other")
        with self.assertRaises(ChatError) as ctx:
            self.broker.decide_permission(
                "s_other", self.uid, "p1", {"behavior": "allow"}
            )
        self.assertEqual(ctx.exception.status, 404)

        # Allow passes the edited input and the optional permission update back.
        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_permission_request",
                "permission_id": "p2",
                "tool_name": "Bash",
                "input": {"command": "ls"},
            },
        )
        allowed = self.broker.decide_permission(
            self.sid,
            self.uid,
            "p2",
            {
                "behavior": "allow",
                "updated_input": {"command": "pwd"},
                "updated_permissions": [{"type": "allow", "rule": "Bash(pwd)"}],
            },
        )
        self.assertEqual(allowed, {"ok": True, "status": "allowed"})
        result = [
            frame
            for frame in conn.ws.frames()
            if frame.get("t") == "chat_permission_result"
            and frame.get("permission_id") == "p2"
        ][-1]["result"]
        self.assertEqual(result["behavior"], "allow")
        self.assertEqual(result["updatedInput"], {"command": "pwd"})
        self.assertEqual(result["updatedPermissions"], [{"type": "allow", "rule": "Bash(pwd)"}])

        # An unknown behavior is rejected without resolving the row.
        self.broker.on_agent_frame(
            conn,
            {
                "t": "chat_permission_request",
                "permission_id": "p3",
                "tool_name": "Bash",
            },
        )
        with self.assertRaises(ChatError) as ctx:
            self.broker.decide_permission(
                self.sid, self.uid, "p3", {"behavior": "maybe"}
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.db.chat_permission_get("p3")["status"], "pending")

    # -------------------------------------------------------------- commands
    def test_send_input_echoes_dedupes_and_rejects_bad_input(self):
        conn = self.attach()
        _, q = self.viewer()

        sent = self.broker.send_input(self.sid, self.uid, "hello", "m1")
        self.assertEqual(sent, {"ok": True, "client_msg_id": "m1"})
        inputs = [
            frame for frame in conn.ws.frames() if frame.get("t") == "chat_input"
        ]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["client_msg_id"], "m1")
        self.assertEqual(inputs[0]["content"], "hello")
        self.assertTrue(inputs[0]["request_id"])

        frames = self.drain(q)
        echoed = self.by_type(frames, "input")
        self.assertEqual(len(echoed), 1)
        self.assertEqual(echoed[0]["client_msg_id"], "m1")
        self.assertEqual(echoed[0]["content"], "hello")
        self.assertEqual(self.broker.public_state(self.sid, self.uid)["status"], "thinking")

        duplicate = self.broker.send_input(self.sid, self.uid, "hello", "m1")
        self.assertEqual(
            duplicate,
            {"ok": True, "client_msg_id": "m1", "duplicate": True},
        )
        self.assertEqual(
            len([f for f in conn.ws.frames() if f.get("t") == "chat_input"]), 1
        )

        old_limit = self.broker.cfg.get("max_input_text_bytes")
        self.broker.cfg["max_input_text_bytes"] = 4
        try:
            with self.assertRaises(ChatError) as ctx:
                self.broker.send_input(self.sid, self.uid, "12345", "m2")
            self.assertEqual(ctx.exception.status, 413)
        finally:
            self.broker.cfg["max_input_text_bytes"] = old_limit

        with self.assertRaises(ChatError) as ctx:
            self.broker.send_input(self.sid, self.uid, 123, "m3")
        self.assertEqual(ctx.exception.status, 400)

        # Offline agents reject the frame.
        self.hub.detach(conn, reason="test")
        with self.assertRaises(ChatError) as ctx:
            self.broker.send_input(self.sid, self.uid, "again", "m4")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "offline")

        # Older clients that did not advertise chat are told to restart.
        self.attach(session_id="s_old", agent_id="a_old", features={})
        with self.assertRaises(ChatError) as ctx:
            self.broker.send_input("s_old", self.uid, "hi", "m5")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "unsupported")

    def test_offline_send_does_not_consume_the_client_msg_id(self):
        # A failed offline send must not be remembered as delivered.
        conn = self.attach()
        self.hub.detach(conn, reason="test")
        with self.assertRaises(ChatError):
            self.broker.send_input(self.sid, self.uid, "hello", "retry-me")

        retry_conn = self.attach()
        result = self.broker.send_input(self.sid, self.uid, "hello", "retry-me")
        self.assertNotIn("duplicate", result)
        self.assertTrue(
            any(
                frame.get("t") == "chat_input"
                for frame in retry_conn.ws.frames()
            )
        )

    def test_request_mode_interrupt_read_and_delete_history(self):
        conn = self.attach()
        _, q = self.viewer()

        self.broker.request_mode(self.sid, self.uid, "chat", force=True)
        handoff = [
            frame
            for frame in conn.ws.frames()
            if frame.get("t") == "chat_handoff"
        ][-1]
        self.assertEqual(handoff["to"], "chat")
        self.assertIs(handoff["force"], True)
        state = self.db.chat_state_get(self.sid)
        self.assertEqual(state["mode"], "handoff")
        self.assertEqual(state["status"], "handoff")
        mode_frames = self.by_type(self.drain(q), "mode")
        self.assertEqual(mode_frames[-1]["message"], "switching to chat")

        # The legacy "terminal" spelling is normalised to tui.
        self.broker.request_mode(self.sid, self.uid, "terminal")
        handoff = [
            frame
            for frame in conn.ws.frames()
            if frame.get("t") == "chat_handoff"
        ][-1]
        self.assertEqual(handoff["to"], "tui")

        with self.assertRaises(ChatError) as ctx:
            self.broker.request_mode(self.sid, self.uid, "bogus")
        self.assertEqual(ctx.exception.status, 400)

        self.broker.interrupt(self.sid, self.uid)
        interrupt = [
            frame
            for frame in conn.ws.frames()
            if frame.get("t") == "chat_interrupt"
        ][-1]
        self.assertTrue(interrupt["request_id"])
        self.assertEqual(
            self.broker.public_state(self.sid, self.uid)["status"], "interrupting"
        )
        self.assertTrue(self.by_type(self.drain(q), "state"))

        seq, _ = self.db.append_chat_event(
            self.sid, self.uid, "e1", "assistant_block", {}
        )
        self.db.chat_read_set(self.uid, self.sid, 0)
        self.assertEqual(self.broker.public_state(self.sid, self.uid)["unread"], 1)
        self.broker.mark_read(self.sid, self.uid, seq)
        self.assertEqual(self.db.chat_read_get(self.uid, self.sid), seq)
        self.assertEqual(self.broker.public_state(self.sid, self.uid)["unread"], 0)

        with self.assertRaises(ChatError) as ctx:
            self.broker.mark_read(self.sid, self.bob["id"], seq)
        self.assertEqual(ctx.exception.status, 404)

        self.db.chat_permission_create(
            {
                "id": "p_del",
                "session_id": self.sid,
                "tool_name": "Bash",
            }
        )
        deleted = self.broker.delete_history(self.sid, self.uid)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.db.chat_events_after(self.sid), [])
        self.assertEqual(self.db.chat_permissions_pending(self.sid), [])
        state = self.db.chat_state_get(self.sid)
        self.assertEqual(state["last_seq"], 0)
        self.assertEqual(state["pending_count"], 0)

        with self.assertRaises(ChatError) as ctx:
            self.broker.delete_history(self.sid, self.bob["id"])
        self.assertEqual(ctx.exception.status, 404)

    def test_request_config_restarts_and_updates_state(self):
        conn = self.attach()
        _, q = self.viewer()

        self.broker.request_config(self.sid, self.uid, permission_mode="acceptEdits")
        frame = [
            item
            for item in conn.ws.frames()
            if item.get("t") == "chat_config"
        ][-1]
        self.assertEqual(frame["permission_mode"], "acceptEdits")
        self.assertIs(frame["force"], False)
        state = self.db.chat_state_get(self.sid)
        self.assertEqual(state["permission_mode"], "acceptEdits")
        self.assertEqual(state["status"], "restarting")
        states = self.by_type(self.drain(q), "state")
        self.assertEqual(states[-1]["chat"]["permission_mode"], "acceptEdits")

        with self.assertRaises(ChatError) as ctx:
            self.broker.request_config(self.sid, self.uid, permission_mode="bogus")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ChatError) as ctx:
            self.broker.request_config(self.sid, self.bob["id"], permission_mode="default")
        self.assertEqual(ctx.exception.status, 404)

    # --------------------------------------------------------------- viewers
    def test_viewer_snapshot_detach_and_slow_viewer(self):
        self.db.chat_state_upsert(self.sid, mode="chat", status="idle")
        seq, _ = self.db.append_chat_event(
            self.sid, self.uid, "e1", "assistant_block", {"text": "backlog"}
        )

        viewer_id, q, snap = self.broker.attach_with_snapshot(self.sid, after_seq=0)
        self.assertEqual(
            [event["event_id"] for event in snap["events"]], ["e1"]
        )
        self.assertEqual(snap["events"][0]["payload"]["text"], "backlog")
        self.assertEqual(snap["state"]["mode"], "chat")
        self.assertEqual(snap["state"]["last_seq"], seq)

        # A subscriber attached after the event still sees live broadcasts.
        self.broker._broadcast(self.sid, {"t": "event", "seq": seq + 1})
        self.assertEqual(self.drain(q)[0]["seq"], seq + 1)

        self.broker.detach_viewer(self.sid, viewer_id)
        self.assertNotIn(self.sid, self.broker._viewers)
        self.broker._broadcast(self.sid, {"t": "event", "seq": seq + 2})
        self.assertEqual(self.drain(q), [])

        # A viewer that stops draining has frames dropped rather than blocking
        # the agent reader.
        viewer_id, slow, _ = self.broker.attach_with_snapshot(self.sid)
        for _ in range(1024):
            slow.put_nowait({"t": "filler"})
        self.broker._broadcast(self.sid, {"t": "overflow"})
        self.assertEqual(slow.qsize(), 1024)
        self.broker.detach_viewer(self.sid, viewer_id)


if __name__ == "__main__":
    unittest.main()
