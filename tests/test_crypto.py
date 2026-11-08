import unittest

from tests.harness import ROOT  # noqa: F401  (sets sys.path)

from opencloddie.crypto import (
    API_KEY_PREFIX,
    decrypt_secret,
    encrypt_secret,
    hash_api_key,
    key_prefix,
    new_api_key,
    new_master_secret,
)


class CryptoTest(unittest.TestCase):
    def test_roundtrip(self):
        master = new_master_secret()
        for value in ("oc_sk_" + "a" * 60, "short", "unicode-ü-🔑"):
            token = encrypt_secret(value, master)
            self.assertNotIn(value, token)
            self.assertEqual(decrypt_secret(token, master), value)

    def test_unique_nonces(self):
        master = new_master_secret()
        a = encrypt_secret("same-value", master)
        b = encrypt_secret("same-value", master)
        self.assertNotEqual(a, b)
        self.assertEqual(decrypt_secret(a, master), "same-value")
        self.assertEqual(decrypt_secret(b, master), "same-value")

    def test_wrong_master_and_tampering(self):
        master = new_master_secret()
        token = encrypt_secret("super-secret", master)
        with self.assertRaises(ValueError):
            decrypt_secret(token, new_master_secret())
        parts = token.split(".")
        raw = list(parts[2])
        raw[len(raw) // 2] = "A" if raw[len(raw) // 2] != "A" else "B"
        parts[2] = "".join(raw)
        with self.assertRaises(ValueError):
            decrypt_secret(".".join(parts), master)

    def test_empty(self):
        self.assertEqual(encrypt_secret("", new_master_secret()), "")
        self.assertEqual(decrypt_secret("", new_master_secret()), "")

    def test_api_key_shape_and_hash(self):
        master = new_master_secret()
        token = new_api_key()
        self.assertTrue(token.startswith(API_KEY_PREFIX))
        self.assertGreater(len(token), 30)
        self.assertEqual(hash_api_key(token, master), hash_api_key(token, master))
        self.assertNotEqual(hash_api_key(token, master), hash_api_key(token + "x", master))
        prefix = key_prefix(token)
        self.assertTrue(prefix.startswith(API_KEY_PREFIX))
        self.assertNotIn(token, prefix)


if __name__ == "__main__":
    unittest.main()
