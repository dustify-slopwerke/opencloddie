"""Small shared helpers. Standard library only."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def expand_path(value: str | os.PathLike[str], base: str | None = None) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(value)))
    p = Path(raw)
    if not p.is_absolute():
        p = Path(base or os.getcwd()) / p
    return p


def ensure_dir(path: str | os.PathLike[str], mode: int = 0o700) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, mode)
    except OSError:
        pass
    return p


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: str | os.PathLike[str], data: Any, mode: int = 0o600) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def slugify(value: str, default: str = "session", maxlen: int = 40) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    return (text[:maxlen] or default).strip("-._") or default


def safe_username(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}", value):
        raise ValueError("invalid user name")
    return value


def mask_secret(value: str, visible: int = 4) -> str:
    value = value or ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def human_bytes(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def human_age(iso_ts: str | None) -> str:
    dt = parse_iso(iso_ts)
    if not dt:
        return "never"
    seconds = max(0, int((utcnow() - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def new_short_id(prefix: str = "s") -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def redact_query(path: str) -> str:
    """Remove token-ish query values from a request line before logging."""
    path = re.sub(r"(?i)([?&](?:token|api_key|key|access_token)=)[^&]*", r"\1<redacted>", path)
    return path


def json_dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]
