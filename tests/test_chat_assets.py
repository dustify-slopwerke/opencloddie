"""Static asset checks for the structured chat UI."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import FakeUpstream, TestServer  # noqa: E402


class ChatAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = FakeUpstream()
        cls.server = TestServer(cls.upstream.base_url)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.upstream.stop()

    def asset(self, path: str) -> tuple[int, dict[str, str], bytes]:
        return self.server.web_request("GET", path)

    def test_chat_scripts_and_styles_are_served(self) -> None:
        expected = {
            "/chat.js": b"window.ChatView",
            "/chat-render.js": b"window.ChatRender",
            "/chat.css": b".chat-pane",
            "/vendor/marked.min.js": b"marked",
            "/vendor/purify.min.js": b"DOMPurify",
        }
        for path, needle in expected.items():
            status, headers, body = self.asset(path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, body, path)
            if path.endswith(".js"):
                self.assertIn("javascript", headers.get("content-type", ""), path)
            elif path.endswith(".css"):
                self.assertIn("css", headers.get("content-type", ""), path)

    def test_index_wires_chat_assets_and_pane(self) -> None:
        status, headers, body = self.server.web_request("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn('id="chat-pane"', html)
        self.assertIn('href="/chat.css"', html)
        self.assertIn('src="/chat-render.js"', html)
        self.assertIn('src="/chat.js"', html)
        # No CDN scripts: the CSP is script-src 'self'.
        self.assertNotIn("cdn.jsdelivr.net/npm/marked", html)
        self.assertNotIn("cdnjs.cloudflare.com/ajax/libs/marked", html)
        csp = headers.get("content-security-policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("img-src 'self' data:", csp)

if __name__ == "__main__":
    unittest.main()
