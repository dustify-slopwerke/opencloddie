import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from tests.harness import ROOT, FakeUpstream, TestServer, tmux_has, wait_for

from opencloddie.websocket import OP_BINARY, OP_TEXT, WebSocketClient

TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for the agent end-to-end test")
class AgentEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.server = TestServer(self.upstream.base_url)
        self.token, _ = self.server.create_user_key(username="alice")
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-e2e-")
        self.sid = "s_" + uuid.uuid4().hex[:12]
        self.tmux_name = "ocd-e2e-" + uuid.uuid4().hex[:8]
        subprocess.run(
            [TMUX, "new-session", "-d", "-s", self.tmux_name, "-c", self.tmp, "--",
             "bash", "--noprofile", "--norc"],
            check=True,
        )
        self.config_path = os.path.join(self.tmp, "client.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "server": f"http://127.0.0.1:{self.server.api_port}",
                    "api_key": self.token,
                    "web_url": f"http://127.0.0.1:{self.server.web_port}",
                },
                fh,
            )
        os.chmod(self.config_path, 0o600)
        self.log_path = os.path.join(self.tmp, "agent.log")
        self.log_file = open(self.log_path, "wb")
        self.agent = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "bin" / "opencloddie"),
                "__agent",
                "--config", self.config_path,
                "--session-id", self.sid,
                "--tmux", self.tmux_name,
                "--name", "e2e",
                "--cwd", self.tmp,
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        online = wait_for(lambda: self._online(), timeout=12)
        if not online:
            self.fail("agent did not register:\n" + self._log_tail())

    def tearDown(self):
        try:
            self.server.api_request("DELETE", f"/client/sessions/{self.sid}", self.token)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.agent.poll() is None:
                self.agent.terminate()
                try:
                    self.agent.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.agent.kill()
        except Exception:  # noqa: BLE001
            pass
        self.log_file.close()
        subprocess.run(
            [TMUX, "kill-session", "-t", f"={self.tmux_name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.server.stop()
        self.upstream.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _online(self):
        try:
            status, _, data = self.server.api_json("GET", "/client/sessions", self.token)
        except OSError:
            return False
        if status != 200:
            return False
        return any(s["id"] == self.sid and s["online"] for s in data.get("sessions", []))

    def _log_tail(self):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[-2000:]
        except OSError:
            return ""

    def test_terminal_roundtrip_and_kill(self):
        status, _, data = self.server.api_json("GET", "/client/sessions", self.token)
        self.assertEqual(status, 200)
        session = next(s for s in data["sessions"] if s["id"] == self.sid)
        self.assertEqual(session["name"], "e2e")
        self.assertTrue(session["online"])

        url = (
            f"ws://127.0.0.1:{self.server.web_port}/api/sessions/{self.sid}/ws"
            "?cols=100&rows=30"
        )
        ws = WebSocketClient.connect(url, headers={"Remote-User": "alice"}, timeout=10)
        try:
            opcode, payload = ws.recv()
            self.assertEqual(opcode, OP_TEXT)
            hello = json.loads(payload)
            self.assertEqual(hello["t"], "hello")
            self.assertEqual(hello["session"]["name"], "e2e")

            ws.send_text(json.dumps({"t": "resize", "cols": 100, "rows": 30}))
            marker = "E2E_MARKER_" + uuid.uuid4().hex[:8]
            ws.send_binary(f"echo {marker}\r".encode())
            output = b""
            deadline = time.time() + 12
            while time.time() < deadline and marker.encode() not in output:
                opcode, payload = ws.recv()
                if opcode == OP_BINARY:
                    output += payload
                elif opcode == OP_TEXT:
                    message = json.loads(payload)
                    if message.get("t") == "error":
                        self.fail(f"terminal error: {message}")
            self.assertIn(marker.encode(), output, output[-800:])
        finally:
            ws.close()

        # Killing through the API must stop the local tmux session and agent.
        status, _, _ = self.server.api_json(
            "DELETE", f"/client/sessions/{self.sid}", self.token
        )
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: not tmux_has(self.tmux_name), timeout=8))
        self.assertTrue(wait_for(lambda: self.agent.poll() is not None, timeout=8))


if __name__ == "__main__":
    unittest.main()
