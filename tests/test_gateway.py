import json
import unittest

from tests.harness import FakeUpstream, TestServer, make_headers

from opencloddie.auth import ApiIdentity
from opencloddie.gateway import GatewayError


def identity_from(server: TestServer, token: str) -> ApiIdentity:
    row = server.app.db.find_api_key(token)
    assert row is not None
    return ApiIdentity(
        user_id=row["user_id"],
        username=row["username"],
        key_id=row["id"],
        key_name=row["name"],
        model=row["model"],
        opencode_key_enc=row["opencode_key_enc"],
        key_row=row,
    )


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.server = TestServer(self.upstream.base_url)

    def tearDown(self):
        self.server.stop()
        self.upstream.stop()

    def _identity(self, *, opencode_key="oc_sk_user_key", model="deepseek-v4.1-flash"):
        token, _ = self.server.create_user_key(opencode_key=opencode_key, model=model)
        return identity_from(self.server, token)

    def _headers(self, **extra):
        base = {
            "Content-Type": "application/json",
            "X-Claude-Code-Session-Id": "session-abc-123",
            "anthropic-beta": "interleaved-thinking-2025-05-14",
            "User-Agent": "claude-cli/2.1.0",
            "Authorization": "Bearer opencloddie-client-token",
            "X-Stainless-Lang": "js",
        }
        base.update(extra)
        return make_headers(base)

    def test_non_stream_rewrites_model_and_auth(self):
        identity = self._identity()
        body = {
            "model": "claude-sonnet-5-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
        result = self.server.app.gateway.proxy(
            identity, "/v1/messages", "beta=true", self._headers(), json.dumps(body).encode()
        )
        self.assertFalse(result.is_stream)
        self.assertEqual(result.status, 200)
        payload = json.loads(result.read_all())
        self.assertEqual(payload["model"], "deepseek-v4.1-flash")

        request = self.upstream.requests[0]
        self.assertEqual(request["path"], "/inference/go/anthropic/v1/messages?beta=true")
        self.assertEqual(request["headers"]["authorization"], "Bearer oc_sk_user_key")
        self.assertEqual(request["headers"]["x-opencode-session"], "session-abc-123")
        self.assertEqual(request["headers"]["x-claude-code-session-id"], "session-abc-123")
        self.assertEqual(request["headers"]["anthropic-beta"], "interleaved-thinking-2025-05-14")
        self.assertEqual(request["headers"]["user-agent"], "claude-cli/2.1.0")
        forwarded_body = json.loads(request["body"])
        self.assertEqual(forwarded_body["model"], "deepseek-v4.1-flash")
        self.assertEqual(forwarded_body["max_tokens"], 16)
        headers = dict(result.headers)
        self.assertEqual(headers["x-opencloddie-model"], "deepseek-v4.1-flash")
        self.assertTrue(headers["x-opencloddie-request-id"].startswith("req_"))
        self.assertEqual(headers["x-request-id"], "upstream-request-1")

    def test_streaming_relays_sse(self):
        identity = self._identity()
        body = {
            "model": "claude-opus-5-5",
            "stream": True,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }
        result = self.server.app.gateway.proxy(
            identity, "/v1/messages", "", self._headers(), json.dumps(body).encode()
        )
        self.assertTrue(result.is_stream)
        data = b"".join(result.iter_bytes())
        self.assertIn(b"event: message_start", data)
        self.assertIn(b"message_stop", data)
        self.assertIn(b'"model":"deepseek-v4.1-flash"', data)
        self.assertEqual(self.upstream.requests[0]["headers"]["x-opencode-session"], "session-abc-123")

    def test_count_tokens(self):
        identity = self._identity()
        body = {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}]}
        result = self.server.app.gateway.proxy(
            identity, "/v1/messages/count_tokens", "", self._headers(), json.dumps(body).encode()
        )
        self.assertEqual(result.status, 200)
        self.assertIn(b"msg_test", result.read_all())
        self.assertTrue(self.upstream.requests[0]["path"].endswith("/v1/messages/count_tokens"))

    def test_missing_opencode_key(self):
        identity = self._identity(opencode_key=None)
        with self.assertRaises(GatewayError) as ctx:
            self.server.app.gateway.proxy(
                identity,
                "/v1/messages",
                "",
                self._headers(),
                json.dumps({"model": "x", "messages": []}).encode(),
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("OpenCode", ctx.exception.message)
        self.assertEqual(self.upstream.requests, [])

    def test_model_is_forced_even_if_client_requests_another(self):
        identity = self._identity(model="qwen3.8-flash")
        body = {"model": "claude-sonnet-5-5", "messages": []}
        self.server.app.gateway.proxy(
            identity, "/v1/messages", "", self._headers(), json.dumps(body).encode()
        )
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["model"], "qwen3.8-flash")

    def test_client_model_override_when_enabled(self):
        self.server.cfg["gateway"]["allow_client_model_override"] = True
        identity = self._identity(model="qwen3.8-flash")
        body = {"model": "glm-5.3-flash", "messages": []}
        self.server.app.gateway.proxy(
            identity, "/v1/messages", "", self._headers(), json.dumps(body).encode()
        )
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["model"], "glm-5.3-flash")

    def test_fallback_retry(self):
        self.server.cfg["gateway"]["fallback_model"] = "qwen3.8-flash"
        self.upstream.fail_first_status = 503
        identity = self._identity(model="deepseek-v4.1-flash")
        result = self.server.app.gateway.proxy(
            identity,
            "/v1/messages",
            "",
            self._headers(),
            json.dumps({"model": "claude-sonnet-5-5", "messages": []}).encode(),
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["model"], "deepseek-v4.1-flash")
        self.assertEqual(json.loads(self.upstream.requests[1]["body"])["model"], "qwen3.8-flash")
        self.assertEqual(dict(result.headers)["x-opencloddie-model"], "qwen3.8-flash")

    def test_x_api_key_auth_style(self):
        self.server.cfg["gateway"]["auth_style"] = "x-api-key"
        identity = self._identity()
        self.server.app.gateway.proxy(
            identity,
            "/v1/messages",
            "",
            self._headers(),
            json.dumps({"model": "m", "messages": []}).encode(),
        )
        headers = self.upstream.requests[0]["headers"]
        self.assertEqual(headers["x-api-key"], "oc_sk_user_key")
        self.assertNotIn("authorization", headers)

    def test_stable_session_header_without_claude(self):
        identity = self._identity()
        headers = make_headers({"Content-Type": "application/json"})
        self.server.app.gateway.proxy(
            identity, "/v1/messages", "", headers, json.dumps({"messages": []}).encode()
        )
        session = self.upstream.requests[0]["headers"]["x-opencode-session"]
        self.assertTrue(session.startswith("opencloddie-"))
        # Same key -> same affinity id.
        self.server.app.gateway.proxy(
            identity, "/v1/messages", "", headers, json.dumps({"messages": []}).encode()
        )
        self.assertEqual(self.upstream.requests[1]["headers"]["x-opencode-session"], session)

    def test_rate_limit(self):
        self.server.cfg["gateway"]["rate_limit_per_minute"] = 1
        identity = self._identity()
        body = json.dumps({"messages": []}).encode()
        self.server.app.gateway.proxy(identity, "/v1/messages", "", self._headers(), body)
        with self.assertRaises(GatewayError) as ctx:
            self.server.app.gateway.proxy(identity, "/v1/messages", "", self._headers(), body)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.headers.get("Retry-After"), "60")

    def test_key_test(self):
        ok = self.server.app.gateway.test_opencode_key("oc_sk_test", "deepseek-v4.1-flash")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["status"], 200)
        self.upstream.status = 401
        bad = self.server.app.gateway.test_opencode_key("oc_sk_bad", "deepseek-v4.1-flash")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["status"], 401)
        # Settings → Test is not gateway traffic.
        self.assertEqual(self.server.app.gateway.metrics.snapshot()["requests"], 0)

    def test_metrics_records_bytes_and_tokens(self):
        identity = self._identity()
        body = {
            "model": "x",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload = json.dumps(body).encode()
        result = self.server.app.gateway.proxy(
            identity, "/v1/messages", "", self._headers(), payload
        )
        response = result.read_all()
        snapshot = self.server.app.gateway.metrics.snapshot(identity.user_id)
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["ok"], 1)
        self.assertEqual(snapshot["in_flight"], 0)
        self.assertEqual(snapshot["bytes_in"], len(payload))
        self.assertEqual(snapshot["bytes_out"], len(response))
        self.assertEqual(snapshot["tokens"]["input"], 3)
        self.assertEqual(snapshot["tokens"]["output"], 1)
        self.assertEqual(snapshot["by_status"], {"200": 1})
        self.assertEqual(snapshot["by_key"][0]["id"], identity.key_id)

    def test_metrics_stream_records_tokens_and_bytes(self):
        identity = self._identity()
        body = {
            "model": "x",
            "stream": True,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        }
        result = self.server.app.gateway.proxy(
            identity, "/v1/messages", "", self._headers(), json.dumps(body).encode()
        )
        data = b"".join(result.iter_bytes())
        snapshot = self.server.app.gateway.metrics.snapshot(identity.user_id)
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["in_flight"], 0)
        self.assertEqual(snapshot["bytes_out"], len(data))
        self.assertEqual(snapshot["tokens"]["input"], 3)
        self.assertEqual(snapshot["tokens"]["output"], 1)

    def test_metrics_error_and_rate_limit(self):
        missing = self._identity(opencode_key=None)
        with self.assertRaises(GatewayError):
            self.server.app.gateway.proxy(
                missing,
                "/v1/messages",
                "",
                self._headers(),
                json.dumps({"messages": []}).encode(),
            )
        self.server.cfg["gateway"]["rate_limit_per_minute"] = 1
        identity = self._identity(opencode_key="oc_sk_user_key")
        body = json.dumps({"messages": []}).encode()
        result = self.server.app.gateway.proxy(
            identity, "/v1/messages", "", self._headers(), body
        )
        result.read_all()
        with self.assertRaises(GatewayError) as ctx:
            self.server.app.gateway.proxy(identity, "/v1/messages", "", self._headers(), body)
        self.assertEqual(ctx.exception.status, 429)

        snapshot = self.server.app.gateway.metrics.snapshot(identity.user_id)
        self.assertEqual(snapshot["requests"], 3)
        self.assertEqual(snapshot["ok"], 1)
        self.assertEqual(snapshot["client_errors"], 1)
        self.assertEqual(snapshot["rate_limited"], 1)
        rates = self.server.app.gateway.rate_snapshot({identity.key_id})
        self.assertEqual(rates[identity.key_id]["used"], 1)
        self.assertEqual(rates[identity.key_id]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
