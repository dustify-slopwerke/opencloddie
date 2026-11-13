"""Authentication for the two listeners.

* The web listener trusts the identity that nginx/Authelia forwards in
  Remote-User (and friends).  It is never exposed publicly.
* The API listener is public and authenticates `ocd_sk_...` API keys.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .crypto import constant_time_eq
from .db import Database
from .models import normalize_model
from .util import safe_username, split_csv


class AuthError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        error_type: str = "authentication_error",
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type
        self.headers = headers or {}
        self.extra = extra or {}


@dataclass
class WebIdentity:
    user_id: int
    username: str
    name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    user: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApiIdentity:
    user_id: int
    username: str
    key_id: str
    key_name: str
    model: str
    opencode_key_enc: str
    key_row: dict[str, Any] = field(default_factory=dict)


def _header(handler: Any, name: str) -> str:
    return (handler.headers.get(name) or "").strip()


def _query(handler: Any) -> dict[str, list[str]]:
    return parse_qs(urlsplit(handler.path).query, keep_blank_values=True)


def is_loopback(address: str | None) -> bool:
    try:
        return ipaddress.ip_address(address or "").is_loopback
    except ValueError:
        return False


def extract_api_token(handler: Any) -> str | None:
    auth = _header(handler, "Authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    for name in ("x-api-key", "x-opencloddie-key", "api-key"):
        value = _header(handler, name)
        if value:
            return value
    q = _query(handler)
    for name in ("token", "api_key", "key"):
        if q.get(name):
            return q[name][0].strip()
    return None


def api_identity(handler: Any, app: Any) -> ApiIdentity:
    token = extract_api_token(handler)
    if not token:
        raise AuthError(
            401,
            "missing API key: send 'Authorization: Bearer ocd_sk_...' "
            "(create one in the opencloddie web UI)",
        )
    row = app.db.find_api_key(token)
    if not row:
        raise AuthError(401, "invalid or revoked opencloddie API key")
    models = app.cfg["gateway"]["models"]
    model = normalize_model(models, row.get("model"), app.cfg["gateway"]["default_model"])
    return ApiIdentity(
        user_id=row["user_id"],
        username=row["username"],
        key_id=row["id"],
        key_name=row["name"],
        model=model,
        opencode_key_enc=row.get("opencode_key_enc", ""),
        key_row=row,
    )


def web_identity(handler: Any, app: Any) -> WebIdentity:
    cfg = app.cfg
    web_cfg = cfg["web"]

    secret = str(web_cfg.get("proxy_secret") or "")
    if secret:
        supplied = _header(handler, web_cfg.get("proxy_secret_header") or "X-OpenCloddie-Proxy-Secret")
        if not supplied or not constant_time_eq(supplied, secret):
            raise AuthError(403, "missing or invalid proxy secret (request must come through nginx)")

    username = _header(handler, web_cfg.get("user_header") or "Remote-User")
    if not username:
        dev_user = str(web_cfg.get("dev_user") or "")
        if dev_user and is_loopback(handler.client_address[0]):
            username = dev_user
    if not username:
        raise AuthError(
            401,
            "not authenticated: nginx/Authelia must forward a Remote-User header",
        )
    try:
        username = safe_username(username)
    except ValueError as exc:
        raise AuthError(403, "invalid user name from auth proxy") from exc

    email = _header(handler, web_cfg.get("email_header") or "Remote-Email")
    name = _header(handler, web_cfg.get("name_header") or "Remote-Name")
    groups = split_csv(_header(handler, web_cfg.get("groups_header") or "Remote-Groups"))
    user = app.db.ensure_user(
        username,
        email=email,
        name=name,
        groups=groups,
        default_model=cfg["gateway"]["default_model"],
    )
    return WebIdentity(
        user_id=user["id"],
        username=user["username"],
        name=user.get("name") or user["username"],
        email=user.get("email") or "",
        groups=groups,
        user=user,
    )
