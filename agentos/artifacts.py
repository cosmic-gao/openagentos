"""交付物存储:把会话交付文件存进 aegra 的 LangGraph Store,供下载。缺 Store 优雅降级。

隔离与记忆一致:namespace 按 aegra 官方 ["users", identity, assistant] 布局(与 memory、REST /store 同一棵用户树),
key 含 thread 以区分同一 (user, assistant) 下不同会话的同名文件。
"""

from __future__ import annotations

import base64
import mimetypes
from typing import Any

from agentos.config import safe_segment


def _store() -> Any | None:
    try:
        from aegra_api.core.database import db_manager

        return db_manager.get_store()
    except Exception:
        return None


def _namespace(identity: str, assistant_id: str) -> tuple[str, ...]:
    # aegra 官方 store 布局 ["users", <user_id>, …];与 memory、REST /store 同一棵用户树。
    return ("users", identity, safe_segment(assistant_id), "artifacts")


def _key(thread_id: str, rel: str) -> str:
    return f"{safe_segment(thread_id)}/{rel}"


async def save(identity: str, assistant_id: str, thread_id: str, rel: str, data: bytes) -> bool:
    """把交付物字节存进 Store(base64),按 (identity, assistant) 隔离;缺 Store 返回 False。"""
    store = _store()
    if store is None:
        return False
    media = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    await store.aput(
        _namespace(identity, assistant_id),
        _key(thread_id, rel),
        {"data": base64.b64encode(data).decode("ascii"), "media_type": media},
    )
    return True


async def load(identity: str, assistant_id: str, thread_id: str, rel: str) -> tuple[bytes, str] | None:
    """从 Store 取回交付物字节与 media_type;不存在/缺 Store 返回 None。"""
    store = _store()
    if store is None:
        return None
    item = await store.aget(_namespace(identity, assistant_id), _key(thread_id, rel))
    if item is None:
        return None
    value = item.value or {}
    encoded = value.get("data")
    if not encoded:
        return None
    return base64.b64decode(encoded), value.get("media_type") or "application/octet-stream"
