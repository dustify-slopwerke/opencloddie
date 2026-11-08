"""Model catalogue exposed by the API and selectable in the web UI."""

from __future__ import annotations

from typing import Any

# Context windows: OpenCode Go publishes 1M for DeepSeek/GLM/Haiku.  Qwen and
# MiniMax windows are not published, so we leave them unset and let Claude Code
# use its 200K default (set `context_tokens` in config to override).
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "id": "deepseek-v4.1-flash",
        "label": "DeepSeek V4.1 Flash",
        "context_tokens": 1_000_000,
        "note": "Fast, cheap, long context.",
    },
    {
        "id": "qwen3.8-flash",
        "label": "Qwen3.8 Flash",
        "context_tokens": None,
        "note": "Strong coding model.",
    },
    {
        "id": "glm-5.3-flash",
        "label": "GLM-5.3 Flash",
        "context_tokens": 1_000_000,
        "note": "Long context.",
    },
    {
        "id": "claude-haiku-5-5",
        "label": "Claude Haiku 5.5",
        "context_tokens": 1_000_000,
        "note": "Small, fast.",
    },
    {
        "id": "minimax-m3",
        "label": "MiniMax M3",
        "context_tokens": None,
        "note": "Alternative coding model.",
    },
]


def normalize_models(raw: Any) -> list[dict[str, Any]]:
    """Accept a simple list of ids or a list of model objects."""
    if not isinstance(raw, list) or not raw:
        return [dict(m) for m in DEFAULT_MODELS]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"id": item, "label": item, "context_tokens": None}
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ctx = item.get("context_tokens")
        try:
            ctx = int(ctx) if ctx else None
        except (TypeError, ValueError):
            ctx = None
        out.append(
            {
                "id": model_id,
                "label": str(item.get("label") or model_id),
                "context_tokens": ctx,
                "note": str(item.get("note") or ""),
            }
        )
    return out or [dict(m) for m in DEFAULT_MODELS]


def model_ids(models: list[dict[str, Any]]) -> list[str]:
    return [m["id"] for m in models]


def find_model(models: list[dict[str, Any]], model_id: str) -> dict[str, Any] | None:
    for m in models:
        if m["id"] == model_id:
            return m
    return None


def normalize_model(models: list[dict[str, Any]], model_id: str | None, fallback: str) -> str:
    if model_id and find_model(models, model_id):
        return model_id
    if find_model(models, fallback):
        return fallback
    return models[0]["id"]


def context_tokens_for(models: list[dict[str, Any]], model_id: str) -> int | None:
    model = find_model(models, model_id)
    return model.get("context_tokens") if model else None
