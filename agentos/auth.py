"""自定义鉴权:从 x-tenant-id / x-user-id 解析身份,不强制(缺头回退 default:anonymous)。

租户归属由上游可信网关负责——须权威注入这两个头并禁止客户端伪造;routes.py 同样信任此前提。
x-user-id == "system" 为管理态,identity 取裸 "system",命中 Aegra 全局共享命名空间
(user_id == "system" 的资源对所有租户可见),故仅应由网关下发;其余按 "{tenant_id}:{user_id}" 隔离。
"""

from __future__ import annotations

from typing import Any

from langgraph_sdk import Auth

auth = Auth()

SYSTEM_IDENTITY = "system"


@auth.authenticate
async def authenticate(headers: dict[Any, Any]) -> dict[str, Any]:
    tenant_id = headers.get("x-tenant-id") or "default"
    user_id = headers.get("x-user-id") or "anonymous"
    identity = SYSTEM_IDENTITY if user_id == SYSTEM_IDENTITY else f"{tenant_id}:{user_id}"
    return {"identity": identity, "tenant_id": tenant_id, "user_id": user_id}
