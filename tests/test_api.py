import json
import unittest

import opencloddie

from tests.harness import FakeUpstream, TestServer, WEB_HEADERS

CSRF = {"X-OpenCloddie-Client": "web"}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.server = TestServer(self.upstream.base_url)
        # Turn off the loopback dev user so authentication is actually tested.
        self.server.cfg["web"]["dev_user"] = ""

    def tearDown(self):
        self.server.stop()
        self.upstream.stop()

    def web_json(self, method, path, body=None, extra=None):
        headers = dict(CSRF if method not in ("GET", "HEAD") else {})
        headers.update(extra or {})
        status, response_headers, raw = self.server.request(
            self.server.web_port, method, path, body=body, headers={**WEB_HEADERS, **headers}
        )
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return status, response_headers, data

    def api_json(self, method, path, token, body=None, extra=None):
        headers = {"Authorization": f"Bearer {token}"}
        headers.update(extra or {})
        status, response_headers, raw = self.server.request(
            self.server.api_port, method, path, body=body, headers=headers
        )
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return status, response_headers, data

    # ------------------------------------------------------------- web auth
    def test_web_requires_authelia_header(self):
        status, _, raw = self.server.request(self.server.web_port, "GET", "/api/me")
        self.assertEqual(status, 401)
        data = json.loads(raw)
        self.assertIn("authenticated", data["error"]["message"].lower())

        status, _, body = self.server.request(self.server.web_port, "GET", "/")
        self.assertEqual(status, 401)
        self.assertIn("Authentication required", body.decode())

    def test_web_identity_and_me(self):
        status, _, data = self.web_json("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["username"], "alice")
        self.assertEqual(data["user"]["email"], "alice@example.com")
        self.assertEqual(data["default_model"], "deepseek-v4.1-flash")
        self.assertFalse(data["opencode_key_set"])
        self.assertTrue(data["install"]["command"].startswith("curl"))
        self.assertEqual(data["urls"]["web"], f"http://127.0.0.1:{self.server.web_port}")

    def test_index_and_static(self):
        status, headers, body = self.server.request(
            self.server.web_port, "GET", "/", headers=WEB_HEADERS
        )
        self.assertEqual(status, 200)
        self.assertIn(b"opencloddie", body)
        self.assertIn("text/html", headers["content-type"])
        status, headers, body = self.server.request(self.server.web_port, "GET", "/vendor/xterm.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])
        status, _, _ = self.server.request(self.server.web_port, "GET", "/vendor/../etc/passwd")
        self.assertIn(status, (400, 404))

    def test_web_ui_shell_assets(self):
        status, _, body = self.server.request(
            self.server.web_port, "GET", "/", headers=WEB_HEADERS
        )
        self.assertEqual(status, 200)
        text = body.decode()
        for needle in ('id="menu-btn"', 'id="drawer-backdrop"', 'id="rail"', "inert", 'id="layout-toggle"'):
            self.assertIn(needle, text)
        status, _, js = self.server.request(self.server.web_port, "GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function openDrawer", js)
        self.assertIn(b"function closeDrawer", js)
        self.assertIn(b"function computeGrid", js)
        self.assertIn(b"function layoutTiles", js)
        status, _, css = self.server.request(self.server.web_port, "GET", "/style.css")
        self.assertEqual(status, 200)
        self.assertIn(b".rail.is-open", css)
        self.assertIn(b".terminals.tiled", css)
        self.assertIn(b"scrollbar-width: none", css)
        self.assertIn(b"scrollback: 0", js)

    # ------------------------------------------------------------ opencode
    def test_opencode_key_lifecycle(self):
        status, _, data = self.web_json("PUT", "/api/opencode-key", {"key": "oc_sk_secret_1234"})
        self.assertEqual(status, 200)
        status, _, me = self.web_json("GET", "/api/me")
        self.assertTrue(me["opencode_key_set"])
        self.assertTrue(me["opencode_key_hint"].endswith("1234"))

        status, _, tested = self.web_json("POST", "/api/opencode-key/test", {})
        self.assertEqual(status, 200)
        self.assertTrue(tested["ok"])

        status, _, _ = self.web_json("DELETE", "/api/opencode-key")
        self.assertEqual(status, 200)
        status, _, me = self.web_json("GET", "/api/me")
        self.assertFalse(me["opencode_key_set"])

    # ---------------------------------------------------------------- keys
    def test_api_key_lifecycle_and_csrf(self):
        status, _, _ = self.server.request(
            self.server.web_port,
            "POST",
            "/api/keys",
            body={"name": "x"},
            headers=WEB_HEADERS,
        )
        self.assertEqual(status, 403)  # missing X-OpenCloddie-Client

        status, _, data = self.web_json("POST", "/api/keys", {"name": "laptop", "model": "qwen3.8-flash"})
        self.assertEqual(status, 201)
        token = data["key"]["token"]
        key_id = data["key"]["id"]
        self.assertTrue(token.startswith("ocd_sk_"))
        self.assertEqual(data["key"]["model"], "qwen3.8-flash")

        status, _, listing = self.web_json("GET", "/api/keys")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["keys"]), 1)
        self.assertNotIn("token", listing["keys"][0])

        status, _, patched = self.web_json(
            "PATCH", f"/api/keys/{key_id}", {"model": "glm-5.3-flash"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(patched["key"]["model"], "glm-5.3-flash")

        # The key works against the API listener until revoked.
        status, _, me = self.api_json("GET", "/client/me", token)
        self.assertEqual(status, 200)
        self.assertEqual(me["model"], "glm-5.3-flash")

        status, _, _ = self.web_json("PATCH", f"/api/keys/{key_id}", {"revoked": True})
        self.assertEqual(status, 200)
        status, _, _ = self.api_json("GET", "/client/me", token)
        self.assertEqual(status, 401)

        status, _, _ = self.web_json("DELETE", f"/api/keys/{key_id}")
        self.assertEqual(status, 200)

    def test_default_model_setting(self):
        status, _, data = self.web_json("PUT", "/api/settings", {"default_model": "qwen3.8-flash"})
        self.assertEqual(status, 200)
        self.assertEqual(data["default_model"], "qwen3.8-flash")
        status, _, me = self.web_json("GET", "/api/me")
        self.assertEqual(me["default_model"], "qwen3.8-flash")

    def test_csrf_origin(self):
        status, _, _ = self.server.request(
            self.server.web_port,
            "POST",
            "/api/keys",
            body={"name": "x"},
            headers={
                **WEB_HEADERS,
                **CSRF,
                "Origin": "https://evil.example",
                "Host": f"127.0.0.1:{self.server.web_port}",
            },
        )
        self.assertEqual(status, 403)

    def test_proxy_secret(self):
        self.server.cfg["web"]["proxy_secret"] = "proxy-secret-value"
        status, _, _ = self.web_json("GET", "/api/me")
        self.assertEqual(status, 403)
        status, _, data = self.web_json(
            "GET",
            "/api/me",
            extra={"X-OpenCloddie-Proxy-Secret": "proxy-secret-value"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["username"], "alice")

    # --------------------------------------------------------------- system
    def test_system_and_stats(self):
        status, _, system = self.web_json("GET", "/api/system")
        self.assertEqual(status, 200)
        self.assertEqual(system["version"], opencloddie.__version__)
        self.assertIn("gateway", system)
        status, _, stats = self.web_json("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("sessions_total", stats["stats"])

    # ------------------------------------------------------------------ API
    def test_api_requires_key(self):
        status, _, raw = self.server.request(self.server.api_port, "GET", "/client/me")
        self.assertEqual(status, 401)
        data = json.loads(raw)
        self.assertIn("API key", data["error"]["message"])
        status, _, raw = self.server.request(
            self.server.api_port, "POST", "/v1/messages", body={"messages": []}
        )
        self.assertEqual(status, 401)
        data = json.loads(raw)
        self.assertEqual(data["type"], "error")
        self.assertEqual(data["error"]["type"], "authentication_error")

    def test_client_api(self):
        token, _ = self.server.create_user_key(username="bob", model="qwen3.8-flash")
        status, _, me = self.api_json("GET", "/client/me", token)
        self.assertEqual(status, 200)
        self.assertEqual(me["model"], "qwen3.8-flash")
        self.assertIsNone(me["context_tokens"])  # qwen has no published window
        self.assertEqual(me["gateway_base_url"], f"http://127.0.0.1:{self.server.api_port}")
        status, _, sessions = self.api_json("GET", "/client/sessions", token)
        self.assertEqual(status, 200)
        self.assertEqual(sessions["sessions"], [])
        status, _, _ = self.api_json("DELETE", "/client/sessions/nope", token)
        self.assertEqual(status, 404)

    def test_models_and_install(self):
        token, _ = self.server.create_user_key(username="bob")
        status, _, models = self.api_json("GET", "/v1/models", token)
        self.assertEqual(status, 200)
        self.assertEqual(len(models["data"]), 5)
        status, headers, body = self.server.request(self.server.api_port, "GET", "/install/opencloddie")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"#!/usr/bin/env python3"))
        status, _, body = self.server.request(self.server.api_port, "GET", "/install")
        self.assertEqual(status, 200)
        self.assertIn(b"opencloddie", body)

    def test_gateway_missing_opencode_key(self):
        token, _ = self.server.create_user_key(username="bob", opencode_key=None)
        status, _, data = self.api_json(
            "POST",
            "/v1/messages",
            token,
            body={"model": "claude-sonnet-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("OpenCode API key", data["error"]["message"])

    def test_gateway_http_non_stream_and_stream(self):
        token, _ = self.server.create_user_key(username="carol", opencode_key="oc_sk_carol")
        body = {
            "model": "claude-sonnet-5-5",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }
        status, headers, data = self.api_json(
            "POST",
            "/v1/messages",
            token,
            body=body,
            extra={"X-Claude-Code-Session-Id": "sess-http-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["model"], "deepseek-v4.1-flash")
        self.assertEqual(headers["x-opencloddie-model"], "deepseek-v4.1-flash")
        request = self.upstream.requests[0]
        self.assertEqual(request["headers"]["x-opencode-session"], "sess-http-1")
        self.assertEqual(request["headers"]["authorization"], "Bearer oc_sk_carol")

        body["stream"] = True
        status, response_headers, raw = self.server.request(
            self.server.api_port,
            "POST",
            "/v1/messages",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Claude-Code-Session-Id": "sess-http-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", response_headers["content-type"])
        self.assertIn(b"message_stop", raw)
        self.assertIn(b"hello", raw)

    def test_gateway_count_tokens(self):
        token, _ = self.server.create_user_key(username="carol")
        status, _, data = self.api_json(
            "POST",
            "/v1/messages/count_tokens",
            token,
            body={"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertIn("msg_test", data["id"])
        self.assertTrue(self.upstream.requests[0]["path"].endswith("/v1/messages/count_tokens"))


if __name__ == "__main__":
    unittest.main()
