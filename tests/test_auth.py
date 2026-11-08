import io
import unittest

from tests.harness import ROOT  # noqa: F401

from opencloddie.auth import extract_api_token, is_loopback


class FakeHandler:
    def __init__(self, headers=None, path="/"):
        self.headers = headers or {}
        self.path = path


class AuthHelpersTest(unittest.TestCase):
    def test_bearer(self):
        h = FakeHandler({"Authorization": "Bearer ocd_sk_abc"})
        self.assertEqual(extract_api_token(h), "ocd_sk_abc")

    def test_bearer_case_insensitive(self):
        h = FakeHandler({"Authorization": "bearer token-value"})
        self.assertEqual(extract_api_token(h), "token-value")

    def test_x_api_key(self):
        h = FakeHandler({"x-api-key": "key-from-header"})
        self.assertEqual(extract_api_token(h), "key-from-header")

    def test_query(self):
        h = FakeHandler({}, "/client/me?token=query-token")
        self.assertEqual(extract_api_token(h), "query-token")

    def test_missing(self):
        self.assertIsNone(extract_api_token(FakeHandler()))

    def test_loopback(self):
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertFalse(is_loopback("192.168.1.2"))
        self.assertFalse(is_loopback("not-an-ip"))


if __name__ == "__main__":
    unittest.main()
