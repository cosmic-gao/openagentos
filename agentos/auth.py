"""自定义鉴权:从请求头 ``x-tenant-id`` / ``x-user-id`` 解析调用方身份。

由 aegra.json 的 ``auth.path`` 指向本模块的 ``auth``。Aegra 每次请求把 headers(dict,
键为小写 str)作为唯一位置参数调用 ``@auth.authenticate``;返回 dict 须含 ``identity``,
其余字段(tenant_id / user_id)透传到 Aegra 的 User 模型,路由与授权处理器可直接取用。

身份取 ``<tenant_id>:<user_id>`` 组合,避免不同租户下 user_id 重名时相互串号;若你的
user_id 本就全局唯一,可改为直接用 user_id。头缺失时回退 ``default`` / ``anonymous``
(即不强制携带;要强制可改为抛 ``Auth.exceptions.HTTPException(status_code=401)``)。
"""

from __future__ import annotations

from typing import Any

from langgraph_sdk import Auth

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict[Any, Any]) -> dict[str, Any]:
    tenant_id = headers.get("x-tenant-id") or "default"
    user_id = headers.get("x-user-id") or "anonymous"
    return {
        "identity": f"{tenant_id}:{user_id}",
        "tenant_id": tenant_id,
        "user_id": user_id,
    }
