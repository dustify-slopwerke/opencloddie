import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from tests.harness import ROOT, FakeUpstream, TestServer, tmux_has, wait_for

CLI = str(ROOT / "bin" / "opencloddie")
CLIENT_SCRIPT = str(ROOT / "opencloddie" / "client" / "opencloddie")


def load_client_module():
    """Import the extensionless client script for direct unit testing."""
    loader = importlib.machinery.SourceFileLoader("opencloddie_client_script", CLIENT_SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SessionMemoryLimitTest(unittest.TestCase):
    def setUp(self):
        self.client = load_client_module()
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-mem-")
        self._saved = {}
        overrides = {
            "HOME": self.tmp,
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
            "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
            "OPENCLODDIE_CONFIG": os.path.join(
                self.tmp, "config", "opencloddie", "config.json"
            ),
        }
        for key, value in overrides.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        self._saved["OPENCLODDIE_MEMORY_LIMIT_MB"] = os.environ.pop(
            "OPENCLODDIE_MEMORY_LIMIT_MB", None
        )
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            os.makedirs(os.environ[key], exist_ok=True)
        os.makedirs(os.path.dirname(os.environ["OPENCLODDIE_CONFIG"]), exist_ok=True)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_and_overrides(self):
        default = self.client.DEFAULT_SESSION_MEMORY_LIMIT_MB
        self.assertEqual(self.client.session_memory_limit_mb({}), default)
        self.assertEqual(
            self.client.session_memory_limit_line({}),
            f"ulimit -v {default * 1024} 2>/dev/null || true",
        )
        self.assertEqual(
            self.client.session_memory_limit_mb({"memory_limit_mb": 4096}), 4096
        )
        os.environ["OPENCLODDIE_MEMORY_LIMIT_MB"] = "2048"
        self.assertEqual(
            self.client.session_memory_limit_mb({"memory_limit_mb": 4096}), 2048
        )
        os.environ.pop("OPENCLODDIE_MEMORY_LIMIT_MB", None)
        self.assertEqual(self.client.session_memory_limit_mb({"memory_limit_mb": 0}), 0)
        self.assertEqual(self.client.session_memory_limit_line({"memory_limit_mb": 0}), "")
        self.assertEqual(
            self.client.session_memory_limit_mb({"memory_limit_mb": "bogus"}), default
        )

    def test_runner_script_applies_cap(self):
        cfg = {
            "server": "http://127.0.0.1:1",
            "api_key": "k",
            "claude_bin": "/bin/true",
            "memory_limit_mb": 4096,
        }
        item = {
            "id": "s_" + uuid.uuid4().hex[:12],
            "tmux": "ocd-mem-test",
            "cwd": self.tmp,
            "name": "mem",
            "claude_session_id": str(uuid.uuid4()),
        }
        me = {"model": "test-model", "messages_enabled": False}
        path = self.client.write_runner(item, cfg, me, [], mode="tui")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("opencloddie-runner-v3", text)
        self.assertIn("ulimit -v 4194304 2>/dev/null || true", text)
        self.assertLess(text.index("ulimit -v"), text.index("cd --"))


class PtyCloseTest(unittest.TestCase):
    def test_close_is_idempotent_and_spares_reused_fd(self):
        # The agent closes a Pty from its reader thread, close frames, and
        # Agent.stop(); a second os.close() of the master fd can hit a number
        # that another thread just reused (the tmux_kill subprocess pipe).
        client = load_client_module()
        pty = client.spawn_pty(["sleep", "5"], 80, 24)
        fd = pty.master
        pty.close()
        self.assertEqual(pty.master, -1)
        probe = os.open(os.devnull, os.O_RDONLY)
        try:
            self.assertEqual(probe, fd, "expected the closed pty fd number to be reused")
            pty.close()
            os.fstat(probe)  # raises OSError if the second close hit this fd
        finally:
            os.close(probe)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for the client integration test")
class ClientCliTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.server = TestServer(self.upstream.base_url)
        self.token, _ = self.server.create_user_key(username="alice")
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-cli-")
        self.xdg = os.path.join(self.tmp, "xdg")
        self.state = os.path.join(self.tmp, "state")
        self.cache = os.path.join(self.tmp, "cache")
        for path in (self.xdg, self.state, self.cache):
            os.makedirs(path, exist_ok=True)
        self.config = os.path.join(self.xdg, "config.json")
        self.env = os.environ.copy()
        self.env.update(
            {
                "XDG_CONFIG_HOME": self.xdg,
                "XDG_STATE_HOME": self.state,
                "XDG_CACHE_HOME": self.cache,
                "OPENCLODDIE_CONFIG": self.config,
                "HOME": self.tmp,
            }
        )
        self.env.pop("OPENCLODDIE_SERVER", None)
        self.env.pop("OPENCLODDIE_API_KEY", None)
        self.name = "cli-" + uuid.uuid4().hex[:6]

    def tearDown(self):
        try:
            self.client("stop", self.name, check=False)
        except Exception:  # noqa: BLE001
            pass
        sessions = {}
        try:
            with open(os.path.join(self.xdg, "sessions.json"), "r", encoding="utf-8") as fh:
                sessions = json.load(fh).get("sessions", {})
        except OSError:
            pass
        for item in sessions.values():
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={item.get('tmux', '')}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.server.stop()
        self.upstream.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, *args, check=True):
        result = subprocess.run(
            [sys.executable, CLI, *args],
            capture_output=True,
            text=True,
            timeout=40,
            env=self.env,
            cwd=self.tmp,
        )
        if check and result.returncode != 0:
            self.fail(
                f"opencloddie {' '.join(args)} failed ({result.returncode})\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def test_login_config_env_and_run(self):
        api_url = f"http://127.0.0.1:{self.server.api_port}"
        login = self.client("login", "--server", api_url, "--api-key", self.token)
        self.assertIn("connected to", login.stdout)
        self.assertTrue(os.path.exists(self.config))
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)

        with open(self.config, encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual(config["server"], api_url)
        self.assertEqual(config["api_key"], self.token)
        self.assertTrue(config["web_url"].endswith(str(self.server.web_port)))

        shown = self.client("config", "show")
        self.assertNotIn(self.token, shown.stdout)
        self.assertIn("api_key", shown.stdout)

        exported = self.client("env")
        self.assertIn(f"ANTHROPIC_BASE_URL={api_url}", exported.stdout)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=", exported.stdout)
        self.assertIn("ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4.1-flash", exported.stdout)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000", exported.stdout)

        who = self.client("whoami")
        self.assertIn("alice", who.stdout)
        self.assertIn("deepseek-v4.1-flash", who.stdout)

        # A fake claude binary keeps the tmux session alive without spending tokens.
        fake = os.path.join(self.tmp, "fake-claude")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\nexec bash --noprofile --norc\n")
        os.chmod(fake, 0o755)
        config["claude_bin"] = fake
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(config, fh)

        started = self.client("run", "--name", self.name, "--no-attach")
        self.assertIn(self.name, started.stdout)

        listing = self.client("ls")
        self.assertIn(self.name, listing.stdout)
        self.assertIn("yes", listing.stdout)

        sessions_data = self.server.api_json("GET", "/client/sessions", self.token)[2]
        session = next(s for s in sessions_data["sessions"] if s["name"] == self.name)
        self.assertTrue(session["online"])
        self.assertEqual(session["cwd"], self.tmp)
        with open(os.path.join(self.xdg, "sessions.json"), encoding="utf-8") as fh:
            local = json.load(fh)
        local_item = next(
            item for item in local["sessions"].values() if item["name"] == self.name
        )
        session_id = local_item["id"]
        tmux_name = local_item["tmux"]
        self.assertTrue(tmux_has(tmux_name))

        runner_path = os.path.join(self.cache, "opencloddie", f"runner-{session_id}.sh")
        with open(runner_path, encoding="utf-8") as fh:
            runner = fh.read()
        self.assertIn("OPENCLODDIE_SESSION_ID=", runner)
        self.assertIn("OPENCLODDIE_CONFIG=", runner)
        self.assertIn("--mcp-config", runner)
        mcp_path = os.path.join(self.cache, "opencloddie", f"mcp-{session_id}.json")
        with open(mcp_path, encoding="utf-8") as fh:
            mcp = json.load(fh)
        entry = mcp["mcpServers"]["opencloddie"]
        self.assertEqual(entry["type"], "stdio")
        self.assertIn("__mcp", entry["args"])
        self.assertNotIn(self.token, json.dumps(mcp))

        stopped = self.client("stop", self.name)
        self.assertIn("stopped local session", stopped.stdout)
        self.assertTrue(wait_for(lambda: not tmux_has(tmux_name), timeout=5))
        after = self.server.api_json("GET", "/client/sessions", self.token)[2]
        self.assertFalse(any(s["name"] == self.name for s in after["sessions"]))


class MailboxCliTest(unittest.TestCase):
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
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-mailbox-cli-")
        self.xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(self.xdg, exist_ok=True)
        self.config = os.path.join(self.xdg, "config.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "server": f"http://127.0.0.1:{self.server.api_port}",
                    "api_key": self.token,
                },
                fh,
            )
        os.chmod(self.config, 0o600)
        self.env = os.environ.copy()
        self.env.update(
            {
                "OPENCLODDIE_CONFIG": self.config,
                "XDG_CONFIG_HOME": self.xdg,
                "HOME": self.tmp,
            }
        )
        for key in ("OPENCLODDIE_SESSION_ID", "OPENCLODDIE_SERVER", "OPENCLODDIE_API_KEY"):
            self.env.pop(key, None)

    def tearDown(self):
        self.server.stop()
        self.upstream.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, *args, check=True, env=None):
        result = subprocess.run(
            [sys.executable, CLI, *args],
            capture_output=True,
            text=True,
            timeout=40,
            env=env or self.env,
            cwd=self.tmp,
        )
        if check and result.returncode != 0:
            self.fail(
                f"opencloddie {' '.join(args)} failed ({result.returncode})\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def test_msg_inbox_and_ls(self):
        result = self.client("msg", "eve-o-tool", "hello", "from", "cli", "--session", "s_a")
        self.assertIn("sent #", result.stdout)
        row = self.server.app.db.list_messages(self.alice["id"], "s_b")[0]
        self.assertEqual(row["body"], "hello from cli")
        self.assertEqual(row["from_session_id"], "s_a")

        result = self.client("inbox", "--session", "s_b")
        self.assertIn("hello from cli", result.stdout)
        self.assertEqual(self.server.app.db.count_unread(self.alice["id"], "s_b"), 0)

        self.server.app.db.add_message(
            user_id=self.alice["id"],
            to_session_id="s_b",
            body="second",
            from_session_id="s_a",
        )
        result = self.client("inbox", "--session", "s_b", "--peek")
        self.assertIn("second", result.stdout)
        self.assertEqual(self.server.app.db.count_unread(self.alice["id"], "s_b"), 1)

        result = self.client("inbox", "--session", "s_b", "--all", "--json")
        data = json.loads(result.stdout)
        self.assertEqual(len(data["messages"]), 2)

        self.server.app.db.add_message(
            user_id=self.alice["id"],
            to_session_id="s_b",
            body="third",
            from_session_id="s_a",
        )
        result = self.client("ls")
        self.assertIn("UNREAD", result.stdout)
        line = next(line for line in result.stdout.splitlines() if "eve-o-tool" in line)
        self.assertIn(" 1 ", line)

    def test_sender_from_env_and_ambiguous_target(self):
        env = dict(self.env)
        env["OPENCLODDIE_SESSION_ID"] = "s_a"
        self.client("msg", "eve-o-tool", "via env", env=env)
        row = self.server.app.db.list_messages(self.alice["id"], "s_b")[0]
        self.assertEqual(row["from_session_id"], "s_a")

        # No session: a human message, attributed to the account.
        self.client("msg", "eve-o-tool", "human message")
        rows = self.server.app.db.list_messages(self.alice["id"], "s_b")
        self.assertEqual(rows[-1]["from_session_id"], None)
        self.assertEqual(rows[-1]["from_name"], "alice")

        for session_id, name, host in (
            ("s_c", "dup", "one"),
            ("s_d", "dup", "two"),
        ):
            self.server.app.db.upsert_session(
                session_id=session_id,
                user_id=self.alice["id"],
                api_key_id=self.key["id"],
                name=name,
                hostname=host,
                cwd="/tmp",
                model="m",
                agent_id="a_" + session_id,
            )
        result = self.client("msg", "dup", "x", "--session", "s_a", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous", result.stderr)
        self.assertIn("dup@one", result.stderr)

        result = self.client("inbox", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no session selected", result.stderr)


if __name__ == "__main__":
    unittest.main()
