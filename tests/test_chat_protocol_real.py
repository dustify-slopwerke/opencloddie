"""Protocol smoke test against a real Claude Code binary and a fake upstream.

Skipped by default: set OPENCLODDIE_TEST_REAL_CLAUDE=1 to run it.  It never
reaches the real API (ANTHROPIC_BASE_URL points at tests.harness.FakeUpstream),
so it validates the headless stream-json contract without inference spend.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import FakeUpstream  # noqa: E402

CLAUDE = shutil.which("claude")
REAL = os.environ.get("OPENCLODDIE_TEST_REAL_CLAUDE") == "1" and bool(CLAUDE)


def clean_env(home: str, port: int) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TERM", "TZ"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(
        {
            "HOME": home,
            "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}/inference/go/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "sk-ant-test-not-a-real-key",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4.1-flash",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4.1-flash",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4.1-flash",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    return env


def user_message(text: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
            }
        )
        + "\n"
    )


@unittest.skipUnless(REAL, "set OPENCLODDIE_TEST_REAL_CLAUDE=1 with claude installed")
class RealClaudeProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeUpstream()
        self.home = tempfile.mkdtemp(prefix="opencloddie-claude-chat-")
        self.session_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self.upstream.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, messages: list[str], timeout: float = 20.0) -> tuple[list[dict], list[str], int]:
        cmd = [
            CLAUDE,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", "deepseek-v4.1-flash",
            "--session-id", self.session_id,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=self.home,
            env=clean_env(self.home, self.upstream.port),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        events: list[dict] = []
        errors: list[str] = []

        def read_out() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        def read_err() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                errors.append(line.rstrip())

        threading.Thread(target=read_out, daemon=True).start()
        threading.Thread(target=read_err, daemon=True).start()
        time.sleep(1.0)
        assert proc.stdin is not None
        for message in messages:
            proc.stdin.write(user_message(message))
            proc.stdin.flush()
            time.sleep(2.0)
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        time.sleep(0.2)
        return events, errors, int(proc.returncode or 0)

    def test_multi_turn_stream_json(self) -> None:
        events, errors, code = self._run(["Reply with exactly: one", "Reply with exactly: two"])
        kinds = [event.get("type") for event in events]
        self.assertIn("system", kinds)
        self.assertIn("assistant", kinds)
        results = [event for event in events if event.get("type") == "result"]
        self.assertEqual(len(results), 2, f"expected two turns, got {results}; stderr={errors}")
        self.assertTrue(all(result.get("session_id") == self.session_id for result in results))
        self.assertEqual(code, 0)
        self.assertTrue(self.upstream.requests, "the fake upstream never received a request")
        # The headless CLI must use the gateway base URL and the preassigned id.
        for request in self.upstream.requests:
            self.assertIn("/v1/messages", request["path"])

    def test_sigint_exits_but_resume_works(self) -> None:
        """Phase 0: SIGINT ends the child; a resumed run keeps the conversation."""
        events, _, _ = self._run(["Remember the word pineapple"])
        self.assertTrue([e for e in events if e.get("type") == "result"])
        # The transcript is on disk under HOME; resume with the same id.
        cmd = [
            CLAUDE,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", "deepseek-v4.1-flash",
            "--resume", self.session_id,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=self.home,
            env=clean_env(self.home, self.upstream.port),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        events2: list[dict] = []

        def read_out() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    events2.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        threading.Thread(target=read_out, daemon=True).start()
        time.sleep(1.0)
        assert proc.stdin is not None
        proc.stdin.write(user_message("What word did I mention?"))
        proc.stdin.flush()
        time.sleep(2.0)
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        results = [e for e in events2 if e.get("type") == "result"]
        self.assertTrue(results, f"resume produced no result: {events2}")
        self.assertNotEqual(
            results[0].get("subtype"),
            "error_during_execution",
            f"resume failed: {results[0]}",
        )
        # The resumed request must include more than the single new message.
        self.assertGreaterEqual(
            max(len(json.loads(r["body"]).get("messages", [])) for r in self.upstream.requests),
            3,
        )


if __name__ == "__main__":
    unittest.main()
