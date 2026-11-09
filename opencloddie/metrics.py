"""In-memory instrumentation for the statistics view.

The registry is intentionally dependency-free and process-local.  Live counters
reset when the server restarts; ``App`` periodically flushes deltas into SQLite
so the UI can chart history across restarts.
"""

from __future__ import annotations

import codecs
import json
import os
import platform
import shutil
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .util import iso

LATENCY_SAMPLES = 512
MAX_SSE_BUFFER = 1024 * 1024

_MODEL_FIELDS = (
    "requests",
    "errors",
    "bytes_in",
    "bytes_out",
    "tokens_in",
    "tokens_out",
)


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (0-100) for a list of numbers."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (float(p) / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _iso_ts(ts: float | None) -> str | None:
    if not ts:
        return None
    return iso(datetime.fromtimestamp(ts, timezone.utc))


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class RequestToken:
    """Opaque handle for one logical gateway request."""

    user_id: int
    key_id: str
    key_name: str
    username: str
    bytes_in: int
    model: str = ""
    finished: bool = False


@dataclass
class UsageDelta:
    """Counter increment persisted as one minute-bucket row."""

    user_id: int
    key_id: str
    key_name: str = ""
    username: str = ""
    model: str = ""
    requests: int = 0
    ok: int = 0
    client_errors: int = 0
    server_errors: int = 0
    rate_limited: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class UsageCounter:
    user_id: int = 0
    key_name: str = ""
    username: str = ""
    model: str = ""
    requests: int = 0
    in_flight: int = 0
    ok: int = 0
    client_errors: int = 0
    server_errors: int = 0
    rate_limited: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    upstream_ms_sum: float = 0.0
    upstream_ms_max: float = 0.0
    last_used_at: float = 0.0
    by_status: Counter[int] = field(default_factory=Counter)
    models: dict[str, dict[str, int]] = field(default_factory=dict)
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=LATENCY_SAMPLES))


class Metrics:
    """Thread-safe gateway usage registry."""

    def __init__(
        self,
        *,
        started_at: float | None = None,
        sample_seconds: int = 60,
        retention_days: int = 7,
        admin_groups: list[str] | None = None,
    ):
        self.started_at = started_at or time.time()
        self.sample_seconds = max(1, int(sample_seconds))
        self.retention_days = max(1, int(retention_days))
        self.admin_groups = list(admin_groups or ["god"])
        self._lock = threading.Lock()
        self._keys: dict[str, UsageCounter] = {}
        self._pending: dict[tuple[int, str, str], UsageDelta] = {}
        self._last_flush = self.started_at
        self._last_purge = 0.0

    # ------------------------------------------------------------- counters
    def begin(
        self,
        *,
        user_id: int,
        key_id: str,
        key_name: str,
        username: str,
        bytes_in: int = 0,
    ) -> RequestToken:
        token = RequestToken(
            user_id=user_id,
            key_id=key_id,
            key_name=key_name,
            username=username,
            bytes_in=int(bytes_in),
        )
        with self._lock:
            counter = self._keys.get(key_id)
            if counter is None:
                counter = UsageCounter(user_id=user_id, key_name=key_name, username=username)
                self._keys[key_id] = counter
            counter.user_id = user_id
            counter.key_name = key_name
            counter.username = username
            counter.requests += 1
            counter.in_flight += 1
            counter.bytes_in += token.bytes_in
        return token

    def set_model(self, token: RequestToken | None, model: str) -> None:
        if token is None or token.finished:
            return
        token.model = model or ""
        with self._lock:
            counter = self._keys.get(token.key_id)
            if counter is not None:
                counter.model = token.model

    def finish(
        self,
        token: RequestToken | None,
        status: int,
        *,
        bytes_out: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        upstream_ms: float = 0.0,
        rate_limited: bool = False,
    ) -> None:
        if token is None or token.finished:
            return
        token.finished = True
        status = int(status)
        bytes_out = int(bytes_out)
        usage = {
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "cache_read_tokens": int(cache_read_tokens),
            "cache_creation_tokens": int(cache_creation_tokens),
        }
        ok = 200 <= status < 300
        client_error = 400 <= status < 500 and not rate_limited and not ok
        server_error = status >= 500
        error = (not ok) and not rate_limited

        with self._lock:
            counter = self._keys.get(token.key_id)
            if counter is None:
                counter = UsageCounter(
                    user_id=token.user_id, key_name=token.key_name, username=token.username
                )
                self._keys[token.key_id] = counter
            counter.in_flight = max(0, counter.in_flight - 1)
            if ok:
                counter.ok += 1
            elif rate_limited:
                counter.rate_limited += 1
            elif client_error:
                counter.client_errors += 1
            elif server_error:
                counter.server_errors += 1
            counter.bytes_out += bytes_out
            counter.tokens_in += usage["tokens_in"]
            counter.tokens_out += usage["tokens_out"]
            counter.cache_read_tokens += usage["cache_read_tokens"]
            counter.cache_creation_tokens += usage["cache_creation_tokens"]
            counter.by_status[status] += 1
            if upstream_ms > 0:
                counter.upstream_ms_sum += float(upstream_ms)
                counter.upstream_ms_max = max(counter.upstream_ms_max, float(upstream_ms))
                counter.latencies.append(float(upstream_ms))
            counter.last_used_at = time.time()
            counter.model = token.model or counter.model

            model = token.model or ""
            model_stats = counter.models.get(model)
            if model_stats is None:
                model_stats = {name: 0 for name in _MODEL_FIELDS}
                counter.models[model] = model_stats
            model_stats["requests"] += 1
            if error or rate_limited:
                model_stats["errors"] += 1
            model_stats["bytes_in"] += token.bytes_in
            model_stats["bytes_out"] += bytes_out
            model_stats["tokens_in"] += usage["tokens_in"]
            model_stats["tokens_out"] += usage["tokens_out"]

            pending_key = (token.user_id, token.key_id, model)
            delta = self._pending.get(pending_key)
            if delta is None:
                delta = UsageDelta(
                    user_id=token.user_id,
                    key_id=token.key_id,
                    key_name=token.key_name,
                    username=token.username,
                    model=model,
                )
                self._pending[pending_key] = delta
            delta.requests += 1
            if ok:
                delta.ok += 1
            elif rate_limited:
                delta.rate_limited += 1
            elif client_error:
                delta.client_errors += 1
            elif server_error:
                delta.server_errors += 1
            delta.bytes_in += token.bytes_in
            delta.bytes_out += bytes_out
            delta.tokens_in += usage["tokens_in"]
            delta.tokens_out += usage["tokens_out"]
            delta.cache_read_tokens += usage["cache_read_tokens"]
            delta.cache_creation_tokens += usage["cache_creation_tokens"]

    # -------------------------------------------------------------- snapshot
    def snapshot(self, user_id: int | None = None) -> dict[str, Any]:
        with self._lock:
            counters = [
                (key_id, counter)
                for key_id, counter in self._keys.items()
                if user_id is None or counter.user_id == user_id
            ]
            by_status: Counter[str] = Counter()
            by_model: dict[str, dict[str, int]] = {}
            latencies: list[float] = []
            by_key: list[dict[str, Any]] = []
            totals: Counter[str] = Counter()
            for key_id, counter in counters:
                for name in (
                    "requests",
                    "in_flight",
                    "ok",
                    "client_errors",
                    "server_errors",
                    "rate_limited",
                    "bytes_in",
                    "bytes_out",
                    "tokens_in",
                    "tokens_out",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                ):
                    totals[name] += getattr(counter, name)
                for status, count in counter.by_status.items():
                    by_status[str(status)] += count
                latencies.extend(counter.latencies)
                for model, stats in counter.models.items():
                    target = by_model.setdefault(model, {name: 0 for name in _MODEL_FIELDS})
                    for name in _MODEL_FIELDS:
                        target[name] += stats.get(name, 0)
                by_key.append(
                    {
                        "id": key_id,
                        "name": counter.key_name,
                        "user": counter.username,
                        "model": counter.model,
                        "requests": counter.requests,
                        "errors": counter.client_errors
                        + counter.server_errors
                        + counter.rate_limited,
                        "rate_limited": counter.rate_limited,
                        "bytes_in": counter.bytes_in,
                        "bytes_out": counter.bytes_out,
                        "tokens_in": counter.tokens_in,
                        "tokens_out": counter.tokens_out,
                        "cache_read_tokens": counter.cache_read_tokens,
                        "cache_creation_tokens": counter.cache_creation_tokens,
                        "last_used_at": _iso_ts(counter.last_used_at),
                    }
                )
            latency_count = len(latencies)
            avg_ms = (sum(latencies) / latency_count) if latency_count else 0.0
            max_ms = max(latencies) if latencies else 0.0
        by_key.sort(key=lambda item: (-item["requests"], item["name"]))
        models = [
            {"model": model, **stats}
            for model, stats in sorted(by_model.items(), key=lambda item: -item[1]["requests"])
        ]
        return {
            "since": _iso_ts(self.started_at),
            "requests": totals["requests"],
            "in_flight": totals["in_flight"],
            "ok": totals["ok"],
            "client_errors": totals["client_errors"],
            "server_errors": totals["server_errors"],
            "rate_limited": totals["rate_limited"],
            "bytes_in": totals["bytes_in"],
            "bytes_out": totals["bytes_out"],
            "tokens": {
                "input": totals["tokens_in"],
                "output": totals["tokens_out"],
                "cache_read": totals["cache_read_tokens"],
                "cache_creation": totals["cache_creation_tokens"],
            },
            "upstream_ms": {
                "avg": round(avg_ms, 1),
                "p50": round(percentile(latencies, 50), 1),
                "p95": round(percentile(latencies, 95), 1),
                "max": round(max_ms, 1),
            },
            "by_status": dict(sorted(by_status.items(), key=lambda item: int(item[0]))),
            "by_model": models,
            "by_key": by_key,
        }

    # --------------------------------------------------------------- flush
    def pending(self) -> list[UsageDelta]:
        with self._lock:
            rows = list(self._pending.values())
            self._pending.clear()
            return rows

    def flush_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now - self._last_flush >= self.sample_seconds

    def mark_flushed(self, now: float | None = None) -> None:
        self._last_flush = time.time() if now is None else now

    def purge_due(self, now: float | None = None, interval: float = 3600.0) -> bool:
        now = time.time() if now is None else now
        return now - self._last_purge >= interval

    def mark_purged(self, now: float | None = None) -> None:
        self._last_purge = time.time() if now is None else now

    def retention_cutoff(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return int(now - self.retention_days * 86400)


class SSEUsageParser:
    """Extract token usage from a streamed Anthropic SSE response."""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0

    def feed(self, chunk: bytes) -> None:
        try:
            text = self._decoder.decode(chunk, final=False)
        except Exception:  # noqa: BLE001 - best effort
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._line(line)
        if len(self._buffer) > MAX_SSE_BUFFER:
            # Malformed/oversized stream: keep only a trailing window.
            self._buffer = self._buffer[-65536:]

    def _line(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            usage = message.get("usage") if isinstance(message, dict) else None
            self._merge_start(usage if isinstance(usage, dict) else {})
        elif kind == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.output_tokens = max(self.output_tokens, _as_int(usage.get("output_tokens")))

    def _merge_start(self, usage: dict[str, Any]) -> None:
        self.input_tokens = max(self.input_tokens, _as_int(usage.get("input_tokens")))
        self.cache_read_tokens = max(
            self.cache_read_tokens, _as_int(usage.get("cache_read_input_tokens"))
        )
        self.cache_creation_tokens = max(
            self.cache_creation_tokens, _as_int(usage.get("cache_creation_input_tokens"))
        )
        self.output_tokens = max(self.output_tokens, _as_int(usage.get("output_tokens")))

    def usage(self) -> dict[str, int]:
        return {
            "tokens_in": self.input_tokens,
            "tokens_out": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


def usage_from_json(body: bytes) -> dict[str, int]:
    """Token usage from a non-streaming Anthropic JSON response."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {
        "tokens_in": _as_int(usage.get("input_tokens")),
        "tokens_out": _as_int(usage.get("output_tokens")),
        "cache_read_tokens": _as_int(usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": _as_int(usage.get("cache_creation_input_tokens")),
    }


# --------------------------------------------------------------- process
def db_size_bytes(path: str) -> int | None:
    if not path:
        return None
    total = 0
    found = False
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
            found = True
        except OSError:
            continue
    return total if found else None


def _proc_status() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name not in ("VmRSS", "VmSize", "VmPeak"):
                    continue
                parts = rest.split()
                if parts:
                    try:
                        values[name] = int(parts[0]) * 1024
                    except ValueError:
                        pass
    except OSError:
        pass
    return values


def _proc_io() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/self/io", "r", encoding="utf-8") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name in ("read_bytes", "write_bytes"):
                    try:
                        values[name] = int(rest.strip())
                    except ValueError:
                        pass
    except OSError:
        pass
    return values


def _fd_count() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def process_snapshot(*, started_at: float, db_path: str = "") -> dict[str, Any]:
    """Best-effort process health.  Unavailable values are ``None``."""
    status = _proc_status()
    io = _proc_io()
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (None, None, None)
    times = os.times()
    disk = None
    directory = os.path.dirname(os.path.abspath(db_path)) if db_path else ""
    if directory:
        try:
            usage = shutil.disk_usage(directory)
            disk = {"total_bytes": usage.total, "free_bytes": usage.free}
        except OSError:
            disk = None
    return {
        "uptime_seconds": max(0, int(time.time() - started_at)),
        "pid": os.getpid(),
        "python": platform.python_version(),
        "platform": sys.platform,
        "threads": threading.active_count(),
        "open_fds": _fd_count(),
        "memory": {
            "rss_bytes": status.get("VmRSS"),
            "vms_bytes": status.get("VmSize"),
            "peak_rss_bytes": status.get("VmPeak"),
        },
        "cpu": {
            "user_seconds": round(float(times.user), 3),
            "system_seconds": round(float(times.system), 3),
        },
        "load": {
            "1m": round(load[0], 3) if load[0] is not None else None,
            "5m": round(load[1], 3) if load[1] is not None else None,
            "15m": round(load[2], 3) if load[2] is not None else None,
        },
        "io": {
            "read_bytes": io.get("read_bytes"),
            "write_bytes": io.get("write_bytes"),
        },
        "disk": disk,
        "database": {"size_bytes": db_size_bytes(db_path)},
    }
