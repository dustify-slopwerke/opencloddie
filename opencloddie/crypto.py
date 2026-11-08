"""At-rest encryption for user-supplied OpenCode keys and API-token hashing.

We deliberately keep this dependency-free.  Secrets are encrypted with an
encrypt-then-MAC construction built from HMAC-SHA256:

  enc_key = HMAC(master, "cloddie-enc-v1")
  mac_key = HMAC(master, "cloddie-mac-v1")
  keystream_i = HMAC(enc_key, nonce || counter_i)
  ciphertext = plaintext XOR keystream
  tag = HMAC(mac_key, "v1" || nonce || ciphertext)

This is a PRF-based stream cipher plus a strong MAC.  A fresh 128-bit nonce is
used for every encryption, so keystream reuse does not occur in practice.
It is not a substitute for a KMS, but it keeps keys out of plaintext on disk.

Format: v1.<b64url nonce>.<b64url ciphertext>.<b64url tag>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct

API_KEY_PREFIX = "ocd_sk_"
_VERSION = b"v1"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _master_bytes(master: str) -> bytes:
    return hashlib.sha256(master.encode("utf-8")).digest()


def _xor(data: bytes, stream: bytes) -> bytes:
    n = len(data)
    return bytes(a ^ b for a, b in zip(data, stream[:n]))


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str, master: str) -> str:
    if not plaintext:
        return ""
    enc_key = hmac.new(_master_bytes(master), b"cloddie-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(_master_bytes(master), b"cloddie-mac-v1", hashlib.sha256).digest()
    nonce = os.urandom(16)
    raw = plaintext.encode("utf-8")
    ciphertext = _xor(raw, _keystream(enc_key, nonce, len(raw)))
    tag = hmac.new(mac_key, _VERSION + nonce + ciphertext, hashlib.sha256).digest()
    return ".".join((_VERSION.decode(), _b64e(nonce), _b64e(ciphertext), _b64e(tag)))


def decrypt_secret(token: str, master: str) -> str:
    if not token:
        return ""
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        raise ValueError("unsupported secret format")
    try:
        nonce = _b64d(parts[1])
        ciphertext = _b64d(parts[2])
        tag = _b64d(parts[3])
    except Exception as exc:  # noqa: BLE001 - normalize base64 errors
        raise ValueError("invalid secret encoding") from exc
    if len(nonce) != 16:
        raise ValueError("invalid secret nonce")
    mac_key = hmac.new(_master_bytes(master), b"cloddie-mac-v1", hashlib.sha256).digest()
    expected = hmac.new(mac_key, _VERSION + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("secret authentication failed")
    enc_key = hmac.new(_master_bytes(master), b"cloddie-enc-v1", hashlib.sha256).digest()
    raw = _xor(ciphertext, _keystream(enc_key, nonce, len(ciphertext)))
    return raw.decode("utf-8")


def new_master_secret() -> str:
    return secrets.token_urlsafe(48)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def key_prefix(token: str) -> str:
    """A short, non-secret display prefix."""
    if not token:
        return ""
    body = token[len(API_KEY_PREFIX):] if token.startswith(API_KEY_PREFIX) else token
    return f"{API_KEY_PREFIX}{body[:6]}…"


def hash_api_key(token: str, master: str) -> str:
    return hmac.new(
        _master_bytes(master), b"opencloddie-api-key-v1" + token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
