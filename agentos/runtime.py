"""运行时上下文读取。

在图执行期间，Aegra/LangGraph 通过 `get_config()` 暴露当前 run 的配置，其中
`configurable.thread_id` 标识会话线程、`metadata.assistant_id` 标识助手。沙箱池按
线程隔离、助手目录按助手隔离，都依赖这两个值。做法与 deepagents 的 `StoreBackend`
一致（调用时读取，而非构图时）。
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_config


def _config() -> dict[str, Any]:
    """返回当前 run 的配置；不在图执行上下文中时返回空 dict。

    `get_config()` 返回 `RunnableConfig`（TypedDict），用 `dict(...)` 归一为普通 dict，
    下游一律按 dict 处理。
    """
    try:
        return dict(get_config() or {})
    except Exception:  # noqa: BLE001 — 不在图执行上下文时优雅降级
        return {}


def current_thread_id() -> str:
    """当前会话线程 id；缺失时回退 "default"。"""
    cfg = _config()
    configurable = cfg.get("configurable") or {}
    metadata = cfg.get("metadata") or {}
    return configurable.get("thread_id") or metadata.get("thread_id") or "default"


def current_assistant_id() -> str:
    """当前助手 id；缺失时回退 "default"。"""
    cfg = _config()
    metadata = cfg.get("metadata") or {}
    configurable = cfg.get("configurable") or {}
    return metadata.get("assistant_id") or configurable.get("assistant_id") or "default"
