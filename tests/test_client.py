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
        tmux_name = next(
            item["tmux"] for item in local["sessions"].values() if item["name"] == self.name
        )
        self.assertTrue(tmux_has(tmux_name))

        stopped = self.client("stop", self.name)
        self.assertIn("stopped local session", stopped.stdout)
        self.assertTrue(wait_for(lambda: not tmux_has(tmux_name), timeout=5))
        after = self.server.api_json("GET", "/client/sessions", self.token)[2]
        self.assertFalse(any(s["name"] == self.name for s in after["sessions"]))


if __name__ == "__main__":
    unittest.main()
