"""交付物存储:把会话交付文件存进 aegra 的 LangGraph Store,发一个不可猜的能力令牌供下载。缺 Store 优雅降级。

会话交付物按 (会话、用户) 隔离:下载凭交付时一次性生成的高熵 token(存进 Store、URL 里不可猜),token 记录
带 owner identity 与 thread;下载路由据此在请求带可信身份头时校验调用者即 owner(不匹配 → 403),无身份头的
纯浏览器导航则凭 token 放行。token 全局唯一,用扁平 namespace ("downloads",)——不落用户树,故不经 REST /store
暴露、不与记忆冲突。沙箱 ephemeral,交付时字节已拷进 Store,故沙箱销毁后链接仍有效。
"""

from __future__ import annotations

import base64
import mimetypes
import secrets
from typing import Any

_DOWNLOADS = ("downloads",)


def _store() -> Any | None:
    try:
        from aegra_api.core.database import db_manager

        return db_manager.get_store()
    except Exception:
        return None


async def save(identity: str, assistant_id: str, thread_id: str, rel: str, data: bytes) -> str | None:
    """存交付物字节(base64)、返回下载 token;记录 owner (identity, assistant, thread) 供下载校验。缺 Store 返回 None。"""
    store = _store()
    if store is None:
        return None
    token = secrets.token_urlsafe(24)
    media = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    await store.aput(
        _DOWNLOADS,
        token,
        {
            "data": base64.b64encode(data).decode("ascii"),
            "media_type": media,
            "identity": identity,
            "assistant": assistant_id,
            "thread": thread_id,
            "rel": rel,
        },
    )
    return token


async def load(token: str) -> dict[str, Any] | None:
    """按 token 取回交付物记录(data/media_type/identity/thread/rel);不存在/缺 Store 返回 None。"""
    store = _store()
    if store is None:
        return None
    item = await store.aget(_DOWNLOADS, token)
    if item is None:
        return None
    value = item.value or {}
    if not value.get("data"):
        return None
    return value
