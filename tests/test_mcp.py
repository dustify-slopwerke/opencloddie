import json
import os
import select
import subprocess
import sys
import tempfile
import unittest

from tests.harness import ROOT, FakeUpstream, TestServer, wait_for

CLI = str(ROOT / "bin" / "opencloddie")


class MCPShimTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.server = TestServer(self.upstream.base_url)
        self.token, self.key = self.server.create_user_key("alice")
        self.alice = self.server.app.db.user_by_username("alice")
        for session_id, name in (("s_a", "consumer"), ("s_b", "eve-o-tool")):
            self.server.app.db.upsert_session(
                session_id=session_id,
                user_id=self.alice["id"],
                api_key_id=self.key["id"],
                name=name,
                hostname="host-" + name,
                cwd="/tmp/" + name,
                model="m",
                agent_id="a_" + session_id,
            )
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-mcp-")
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "server": f"http://127.0.0.1:{self.server.api_port}",
                    "api_key": self.token,
                },
                fh,
            )
        os.chmod(self.config, 0o600)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                CLI,
                "__mcp",
                "--config",
                self.config,
                "--session-id",
                "s_a",
                "--name",
                "consumer",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def tearDown(self):
        try:
            if self.proc.poll() is None:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        try:
            self.proc.stdout.close()
            self.proc.stderr.close()
        except Exception:  # noqa: BLE001
            pass
        self.server.stop()
        self.upstream.stop()

    def send(self, message) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout: float = 5) -> dict:
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        self.assertTrue(ready, "timed out waiting for the MCP shim")
        line = self.proc.stdout.readline()
        self.assertTrue(line, "the MCP shim closed stdout")
        return json.loads(line)

    def call(self, name: str, arguments: dict) -> dict:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self.read()

    def test_initialize_list_and_tools(self):
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        init = self.read()
        self.assertEqual(init["id"], 1)
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertEqual(init["result"]["serverInfo"]["name"], "opencloddie")

        # Notifications never produce a response; the next response is for ping.
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(self.read()["id"], 2)

        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = self.read()["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {"list_sessions", "send_session_message", "check_mailbox"},
        )
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")

        result = self.call("list_sessions", {})
        self.assertFalse(result["result"]["isError"])
        text = result["result"]["content"][0]["text"]
        self.assertIn("eve-o-tool", text)
        self.assertIn("(this session)", text)

    def test_send_and_check_mailbox_round_trip(self):
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.read()

        result = self.call(
            "send_session_message",
            {"to": "eve-o-tool", "message": "please work up X"},
        )
        self.assertFalse(result["result"]["isError"])
        self.assertIn("eve-o-tool", result["result"]["content"][0]["text"])
        row = self.server.app.db.list_messages(self.alice["id"], "s_b")[0]
        self.assertEqual(row["body"], "please work up X")
        self.assertEqual(row["from_session_id"], "s_a")

        # A message addressed to this session is returned, then acked after the
        # JSON-RPC response is written.
        self.server.app.db.add_message(
            user_id=self.alice["id"],
            to_session_id="s_a",
            body="here is the contract",
            from_session_id="s_b",
        )
        result = self.call("check_mailbox", {})
        self.assertFalse(result["result"]["isError"])
        self.assertIn("here is the contract", result["result"]["content"][0]["text"])
        self.assertTrue(
            wait_for(
                lambda: self.server.app.db.count_unread(self.alice["id"], "s_a") == 0,
                timeout=3,
            )
        )
        result = self.call("check_mailbox", {})
        self.assertIn("Mailbox empty", result["result"]["content"][0]["text"])

    def test_tool_errors_and_protocol_errors(self):
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.read()

        result = self.call(
            "send_session_message", {"to": "does-not-exist", "message": "x"}
        )
        self.assertTrue(result["result"]["isError"])
        self.assertNotIn("error", result)

        self.proc.stdin.write("not json\n")
        self.proc.stdin.flush()
        self.assertEqual(self.read()["error"]["code"], -32700)

        self.send([{"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual(self.read()["error"]["code"], -32600)

        self.send({"jsonrpc": "2.0", "id": 3, "method": "no/such"})
        self.assertEqual(self.read()["error"]["code"], -32601)

        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
