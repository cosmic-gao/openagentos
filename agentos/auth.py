"""自定义鉴权:从 x-tenant-id / x-user-id 解析身份,不强制(缺头回退 default:anonymous)。租户归属由上游可信网关负责。

x-user-id == "system" 取裸 identity "system"、命中 Aegra 全局共享命名空间(对所有租户可见),故仅应由网关下发。
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
    identity = SYSTEM_IDENTITY if user_id == SYSTEM_IDENTITY else f"{user_id}"
    return {"identity": identity, "tenant_id": tenant_id, "user_id": user_id}
