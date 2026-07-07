"""从运行时 / 工厂 config 读取 thread_id、assistant_id（优先 configurable，回退 metadata）。"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_config


def _config() -> dict[str, Any]:
    try:
        return dict(get_config() or {})
    except Exception:  # noqa: BLE001
        return {}


def _pick(config: dict[str, Any], key: str) -> str | None:
    return (config.get("configurable") or {}).get(key) or (config.get("metadata") or {}).get(key)


def current_thread_id() -> str:
    return _pick(_config(), "thread_id") or "default"


def assistant_id_from_config(config: dict[str, Any] | None) -> str:
    return _pick(config or {}, "assistant_id") or "default"
