"""End-to-end structured chat: real client agent/runner + fake claude + hub + web WS.

The fake `claude` echoes stream-json turns, so this exercises the whole path
without inference: CLI launch -> __chat runner -> unix socket -> __agent ->
hub broker -> SQLite -> chat WebSocket.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import TestServer, FakeUpstream, tmux_has, wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "bin" / "opencloddie"
FAKE_CLAUDE = ROOT / "tests" / "fake_chat_claude.py"
TMUX = shutil.which("tmux")

from opencloddie.websocket import OP_TEXT, WebSocketClient  # noqa: E402


@unittest.skipUnless(TMUX, "tmux is required for the chat client end-to-end test")
class ChatClientEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeUpstream()
        self.server = TestServer(self.upstream.base_url)
        self.token, _ = self.server.create_user_key(username="alice")
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-chat-e2e-")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.name = "chat-e2e-" + uuid.uuid4().hex[:6]
        self.xdg_config = os.path.join(self.tmp, "config")
        self.xdg_state = os.path.join(self.tmp, "state")
        self.xdg_cache = os.path.join(self.tmp, "cache")
        for path in (self.xdg_config, self.xdg_state, self.xdg_cache):
            os.makedirs(path, mode=0o700)
        fake = os.path.join(self.tmp, "fake-claude")
        shutil.copyfile(FAKE_CLAUDE, fake)
        os.chmod(fake, 0o755)
        cfg_dir = os.path.join(self.xdg_config, "opencloddie")
        os.makedirs(cfg_dir, mode=0o700)
        with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "server": f"http://127.0.0.1:{self.server.api_port}",
                    "api_key": self.token,
                    "web_url": f"http://127.0.0.1:{self.server.web_port}",
                    "claude_bin": fake,
                },
                fh,
            )
        self.env = dict(os.environ)
        for key in (
            "OPENCLODDIE_CONFIG",
            "OPENCLODDIE_SESSION_ID",
            "OPENCLODDIE_SERVER",
            "OPENCLODDIE_API_KEY",
        ):
            self.env.pop(key, None)
        self.env.update(
            {
                "HOME": self.tmp,
                "XDG_CONFIG_HOME": self.xdg_config,
                "XDG_STATE_HOME": self.xdg_state,
                "XDG_CACHE_HOME": self.xdg_cache,
            }
        )
        self.session_id: str | None = None
        self.tmux_name: str | None = None

    def tearDown(self) -> None:
        try:
            if self.tmux_name:
                subprocess.run(
                    [TMUX, "kill-session", "-t", f"={self.tmux_name}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            self.server.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.upstream.stop()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    def client(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLIENT), *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _session(self) -> dict | None:
        status, _, data = self.server.api_json("GET", "/client/sessions", self.token)
        if status != 200:
            return None
        for session in data.get("sessions", []):
            if session.get("name") == self.name:
                return session
        return None

    def _wait_session(self) -> dict:
        session = wait_for(self._session, timeout=20)
        self.assertIsNotNone(session, "chat session did not register")
        assert session is not None
        self.session_id = session["id"]
        with open(
            os.path.join(self.xdg_config, "opencloddie", "sessions.json"),
            encoding="utf-8",
        ) as fh:
            local = json.load(fh)["sessions"]
        item = next(iter(local.values()))
        self.tmux_name = item.get("tmux")
        return session

    def _chat_state(self) -> dict | None:
        status, _, data = self.server.web_json(
            "GET", f"/api/sessions/{self.session_id}/chat"
        )
        if status != 200:
            return None
        return data

    def _chat_mode(self) -> str | None:
        session = self._session() or {}
        return (session.get("chat") or {}).get("mode")

    def _chat_status(self) -> str | None:
        state = self._chat_state() or {}
        return (state.get("chat") or {}).get("status")

    def _logs(self) -> str:
        chunks = []
        for root, _dirs, files in os.walk(self.xdg_state):
            for name in files:
                if not name.endswith(".log"):
                    continue
                try:
                    with open(os.path.join(root, name), encoding="utf-8", errors="replace") as fh:
                        chunks.append(f"=== {name} ===\n" + fh.read()[-5000:])
                except OSError:
                    pass
        return "\n".join(chunks)

    def _post_chat(self, action: str, body: dict) -> int:
        status, _, _ = self.server.web_json(
            "POST",
            f"/api/sessions/{self.session_id}/chat/{action}",
            body=body,
            headers={"X-OpenCloddie-Client": "web"},
        )
        return status

    def _connect_chat(self) -> WebSocketClient:
        url = f"ws://127.0.0.1:{self.server.web_port}/api/sessions/{self.session_id}/chat/ws"
        ws = WebSocketClient.connect(
            url,
            headers={
                "Remote-User": "alice",
                "Origin": f"http://127.0.0.1:{self.server.web_port}",
            },
            timeout=30,
        )
        opcode, payload = ws.recv()
        self.assertEqual(opcode, OP_TEXT)
        hello = json.loads(payload)
        self.assertEqual(hello["t"], "hello")
        return ws

    def _read_event_of_kind(self, ws: WebSocketClient, kind: str, timeout: float = 30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            op, raw = ws.recv()
            if op != OP_TEXT:
                continue
            frame = json.loads(raw)
            if frame.get("t") == "event" and frame.get("kind") == kind:
                return frame
        return None

    def _send_and_read(self, ws: WebSocketClient, text: str, client_id: str) -> None:
        ws.send_text(
            json.dumps({"t": "input", "client_msg_id": client_id, "content": text})
        )
        deadline = time.time() + 20
        assistant = None
        result = None
        while time.time() < deadline and result is None:
            op, raw = ws.recv()
            if op != OP_TEXT:
                continue
            frame = json.loads(raw)
            if frame.get("t") != "event":
                continue
            if frame.get("kind") == "assistant_block":
                block = frame["payload"].get("block") or {}
                block_text = block.get("text", "") if isinstance(block, dict) else ""
                if text in (block_text or json.dumps(frame["payload"])):
                    assistant = frame
            elif frame.get("kind") == "result":
                result = frame
        self.assertIsNotNone(assistant, f"no assistant_block for {text!r}")
        self.assertIsNotNone(result, f"no result for {text!r}")
        self.assertEqual(result["payload"].get("subtype"), "success")

    # --------------------------------------------------------------------- test
    def test_chat_turn_round_trip(self) -> None:
        started = self.client("run", "--chat", "-n", self.name, "-C", self.work, "--no-attach")
        self.assertEqual(started.returncode, 0, started.stderr)
        session = self._wait_session()
        self.assertTrue(session["online"])
        self.assertEqual(session["chat"]["mode"], "chat", session["chat"])
        self.assertTrue(wait_for(lambda: self._chat_state(), timeout=20))

        ws = self._connect_chat()
        try:
            self._send_and_read(ws, "hello chat", "m1")
            state = self._chat_state()
            assert state is not None
            self.assertTrue(any(e["kind"] == "assistant_block" for e in state["events"]))
            self.assertGreaterEqual(state["last_seq"], 1)
            ws.send_text(json.dumps({"t": "read", "last_seq": state["last_seq"]}))
        finally:
            ws.close()

        # Handoff chat -> TUI -> chat keeps the same Claude session and the
        # runner comes back on the same tmux session.
        self.assertIn(self._post_chat("mode", {"mode": "tui", "force": True}), (200, 202))
        self.assertTrue(
            wait_for(lambda: self._chat_mode() == "tui", timeout=20),
            f"chat did not hand off to tui (mode={self._chat_mode()})",
        )
        self.assertIn(self._post_chat("mode", {"mode": "chat", "force": True}), (200, 202))
        self.assertTrue(
            wait_for(lambda: self._chat_mode() == "chat", timeout=20),
            f"tui did not hand back to chat (mode={self._chat_mode()})",
        )
        self.assertTrue(
            wait_for(
                lambda: self._chat_status() in ("idle", "thinking"),
                timeout=30,
            ),
            f"chat runner did not become ready after handoff (status={self._chat_status()})",
        )
        ws2 = self._connect_chat()
        try:
            self._send_and_read(ws2, "second turn", "m2")
        finally:
            ws2.close()

        stopped = self.client("stop", self.name)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertTrue(wait_for(lambda: not tmux_has(self.tmux_name or ""), timeout=10))


    def test_rewind_round_trip(self) -> None:
        started = self.client("run", "--chat", "-n", self.name, "-C", self.work, "--no-attach")
        self.assertEqual(started.returncode, 0, started.stderr)
        session = self._wait_session()
        self.assertTrue(session["online"])
        self.assertTrue(wait_for(lambda: self._chat_status() in ("idle", "thinking"), timeout=20))
        ws = self._connect_chat()
        try:
            self._send_and_read(ws, "checkpoint me", "m-rewind")
            self.assertTrue(wait_for(lambda: self._chat_status() == "idle", timeout=10))
            state = self._chat_state() or {}
            checkpoints = [
                event for event in state.get("events", []) if event.get("kind") == "checkpoint"
            ]
            self.assertTrue(checkpoints, "no checkpoint event was persisted")
            checkpoint = str(checkpoints[-1]["payload"].get("uuid") or "")
            self.assertTrue(checkpoint, "checkpoint has no uuid")
            self.assertIn(
                self._post_chat("rewind", {"checkpoint_uuid": checkpoint}),
                (200, 202),
            )
            frame = self._read_event_of_kind(ws, "rewind")
            self.assertIsNotNone(frame, "no rewind event reached the browser")
            self.assertTrue(frame["payload"].get("ok"), frame)
            self.assertTrue(
                wait_for(lambda: self._chat_status() == "idle", timeout=30),
                "chat runner did not come back after rewind",
            )
        finally:
            ws.close()
        stopped = self.client("stop", self.name)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_permission_round_trip(self) -> None:
        """The full approval path: fake CLI -> permission MCP shim -> runner ->
        agent -> hub -> browser WS -> decision -> back to the CLI."""
        # The tmux pane does not inherit this test's environment; use a marker
        # file in the session cwd instead.
        with open(os.path.join(self.work, ".fake-permission"), "w", encoding="utf-8") as fh:
            fh.write("1\n")
        started = self.client("run", "--chat", "-n", self.name, "-C", self.work, "--no-attach")
        self.assertEqual(started.returncode, 0, started.stderr)
        session = self._wait_session()
        self.assertTrue(session["online"])
        self.assertTrue(wait_for(lambda: self._chat_status() in ("idle", "thinking"), timeout=20))

        ws = self._connect_chat()
        try:
            ws.send_text(
                json.dumps(
                    {
                        "t": "input",
                        "client_msg_id": "perm1",
                        "content": "run a command",
                    }
                )
            )
            permission = None
            seen = []
            deadline = time.time() + 20
            try:
                while time.time() < deadline:
                    op, raw = ws.recv()
                    if op != OP_TEXT:
                        continue
                    frame = json.loads(raw)
                    seen.append(frame.get("t") or frame.get("kind"))
                    if frame.get("t") == "permission":
                        permission = frame["permission"]
                        break
            except TimeoutError:
                pass
            if permission is None:
                state = self._chat_state() or {}
                kinds = [
                    (event.get("kind"), event.get("payload"))
                    for event in state.get("events", [])
                ]
                self.fail(
                    "no permission frame reached the browser; "
                    f"frames={seen}; events={kinds}\n{self._logs()}"
                )
            self.assertEqual(permission["tool_name"], "Bash")
            self.assertEqual(permission["status"], "pending")
            ws.send_text(
                json.dumps(
                    {
                        "t": "permission",
                        "permission_id": permission["id"],
                        "behavior": "allow",
                    }
                )
            )

            got_tool_result = False
            got_result = False
            deadline = time.time() + 20
            while time.time() < deadline and not got_result:
                op, raw = ws.recv()
                if op != OP_TEXT:
                    continue
                frame = json.loads(raw)
                if frame.get("t") != "event":
                    continue
                if frame.get("kind") == "tool_result":
                    got_tool_result = True
                elif frame.get("kind") == "result":
                    got_result = True
            self.assertTrue(got_tool_result, "the approved tool result never arrived")
            self.assertTrue(got_result, "the turn never completed after approval")
            # The decision is durable and visible in history.
            state = self._chat_state()
            assert state is not None
            resolved = [
                e for e in state["events"] if e["kind"] == "permission_resolved"
            ]
            self.assertTrue(resolved)
        finally:
            ws.close()
        stopped = self.client("stop", self.name)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
