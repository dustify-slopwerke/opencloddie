"""Configuration loading, defaults and first-run initialisation."""

from __future__ import annotations

import copy
import json
import os
import secrets
from pathlib import Path
from typing import Any

from .crypto import new_master_secret
from .models import DEFAULT_MODELS, normalize_models
from .util import atomic_write_json, ensure_dir, expand_path, read_json

APP_NAME = "opencloddie"

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        # The web listener is *internal*: nginx + Authelia sit in front and add
        # the Remote-User header.  Never publish this port.
        "web_host": "127.0.0.1",
        "web_port": 8787,
        # The API listener is public (still key-authenticated) and speaks the
        # Anthropic Messages API to Claude Code.
        "api_host": "127.0.0.1",
        "api_port": 8788,
        "public_web_url": "",
        "public_api_url": "",
        "log_level": "info",
        "state_dir": "",
        "tls_cert": "",
        "tls_key": "",
        "behind_proxy": False,
    },
    "web": {
        "user_header": "Remote-User",
        "groups_header": "Remote-Groups",
        "email_header": "Remote-Email",
        "name_header": "Remote-Name",
        # Optional defence in depth: require this value in the request header.
        "proxy_secret": "",
        "proxy_secret_header": "X-OpenCloddie-Proxy-Secret",
        # For local development only (no nginx/Authelia).  Empty in production.
        "dev_user": "",
        "auth_portal_url": "",
        "title": "opencloddie",
    },
    "gateway": {
        "upstream": "https://opencode.ai/inference/go/anthropic",
        "auth_style": "bearer",  # bearer | x-api-key
        "connect_timeout": 30,
        "read_timeout": 900,
        "max_body_bytes": 32 * 1024 * 1024,
        "fallback_model": "",
        "default_model": DEFAULT_MODELS[0]["id"],
        "models": DEFAULT_MODELS,
        "allow_client_model_override": False,
        "rate_limit_per_minute": 120,
        "user_agent": "opencloddie-gateway/0.1",
    },
    "secrets": {
        "master": "",
    },
    "database": {
        "path": "",
    },
}


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / APP_NAME


def default_config_path() -> Path:
    return Path(os.environ.get("OPENCLODDIE_CONFIG") or config_home() / "config.json")


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / APP_NAME


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | os.PathLike[str] | None = None, *, create: bool = False) -> dict[str, Any]:
    cfg_path = Path(path) if path else default_config_path()
    raw = read_json(cfg_path)
    if raw is None:
        if not create:
            raise FileNotFoundError(
                f"config not found: {cfg_path}\nRun `opencloddie-server init` first."
            )
        raw = build_default_config()
        atomic_write_json(cfg_path, raw)
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a JSON object: {cfg_path}")

    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    _apply_env(cfg)
    cfg["_config_path"] = str(cfg_path)
    cfg["gateway"]["models"] = normalize_models(cfg["gateway"].get("models"))

    state_dir = cfg["server"].get("state_dir") or str(default_state_dir())
    state_dir = str(expand_path(state_dir))
    cfg["_state_dir"] = state_dir
    ensure_dir(state_dir)

    db_path = cfg["database"].get("path") or os.path.join(state_dir, "opencloddie.db")
    cfg["database"]["path"] = str(expand_path(db_path))
    ensure_dir(Path(cfg["database"]["path"]).parent)

    master = cfg["secrets"].get("master", "")
    if not master:
        raise ValueError(
            "secrets.master is empty; run `opencloddie-server init` to generate one "
            "(or set OPENCLODDIE_MASTER_SECRET)."
        )
    _validate(cfg)
    return cfg


def _apply_env(cfg: dict[str, Any]) -> None:
    if os.environ.get("OPENCLODDIE_MASTER_SECRET"):
        cfg["secrets"]["master"] = os.environ["OPENCLODDIE_MASTER_SECRET"]
    if os.environ.get("OPENCLODDIE_WEB_PORT"):
        cfg["server"]["web_port"] = int(os.environ["OPENCLODDIE_WEB_PORT"])
    if os.environ.get("OPENCLODDIE_API_PORT"):
        cfg["server"]["api_port"] = int(os.environ["OPENCLODDIE_API_PORT"])
    if os.environ.get("OPENCLODDIE_WEB_HOST"):
        cfg["server"]["web_host"] = os.environ["OPENCLODDIE_WEB_HOST"]
    if os.environ.get("OPENCLODDIE_API_HOST"):
        cfg["server"]["api_host"] = os.environ["OPENCLODDIE_API_HOST"]
    if "OPENCLODDIE_DEV_USER" in os.environ:
        cfg["web"]["dev_user"] = os.environ["OPENCLODDIE_DEV_USER"]
    if os.environ.get("OPENCLODDIE_STATE_DIR"):
        cfg["server"]["state_dir"] = os.environ["OPENCLODDIE_STATE_DIR"]
    if os.environ.get("OPENCLODDIE_PUBLIC_WEB_URL"):
        cfg["server"]["public_web_url"] = os.environ["OPENCLODDIE_PUBLIC_WEB_URL"]
    if os.environ.get("OPENCLODDIE_PUBLIC_API_URL"):
        cfg["server"]["public_api_url"] = os.environ["OPENCLODDIE_PUBLIC_API_URL"]


def _validate(cfg: dict[str, Any]) -> None:
    server = cfg["server"]
    for key in ("web_port", "api_port"):
        try:
            port = int(server[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"server.{key} must be a number") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"server.{key} out of range")
        server[key] = port
    if server["web_port"] == server["api_port"] and str(server["web_host"]) == str(
        server["api_host"]
    ):
        raise ValueError("web and api listeners cannot share host:port")
    if cfg["gateway"].get("auth_style") not in ("bearer", "x-api-key"):
        raise ValueError("gateway.auth_style must be 'bearer' or 'x-api-key'")


def build_default_config(*, dev: bool = False) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["secrets"]["master"] = new_master_secret()
    if dev:
        cfg["web"]["dev_user"] = "dev"
    else:
        cfg["web"]["dev_user"] = ""
    return cfg


def init_config(
    path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    dev: bool = False,
    public_web_url: str = "",
    public_api_url: str = "",
) -> Path:
    cfg_path = Path(path) if path else default_config_path()
    if cfg_path.exists() and not force:
        raise FileExistsError(f"config already exists: {cfg_path} (use --force to replace)")
    cfg = build_default_config(dev=dev)
    cfg["server"]["public_web_url"] = public_web_url
    cfg["server"]["public_api_url"] = public_api_url
    env_state = os.environ.get("OPENCLODDIE_STATE_DIR")
    if env_state:
        cfg["server"]["state_dir"] = env_state
    ensure_dir(cfg_path.parent)
    # Resolve and persist the state dir so a read-only home is never touched
    # on later starts (important in the container image).
    state_dir = Path(expand_path(cfg["server"]["state_dir"] or default_state_dir()))
    cfg["server"]["state_dir"] = str(state_dir)
    atomic_write_json(cfg_path, cfg)
    ensure_dir(state_dir)
    ensure_dir(state_dir / "logs")
    return cfg_path


def effective_public_urls(cfg: dict[str, Any], request_headers: Any = None) -> dict[str, str]:
    """Public web/api base URLs, preferring config then proxy headers."""
    web = str(cfg["server"].get("public_web_url") or "").rstrip("/")
    api = str(cfg["server"].get("public_api_url") or "").rstrip("/")
    if not web and request_headers is not None:
        proto = request_headers.get("X-Forwarded-Proto") or "http"
        host = request_headers.get("Host") or ""
        if host:
            web = f"{proto}://{host}"
    return {"web": web, "api": api}


def write_config(path: str | os.PathLike[str], cfg: dict[str, Any]) -> None:
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    atomic_write_json(path, clean)


def config_json_for_display(cfg: dict[str, Any]) -> str:
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    clean = copy.deepcopy(clean)
    if clean.get("secrets", {}).get("master"):
        clean["secrets"]["master"] = "<redacted>"
    if clean.get("web", {}).get("proxy_secret"):
        clean["web"]["proxy_secret"] = "<redacted>"
    return json.dumps(clean, indent=2)
