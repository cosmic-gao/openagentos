"""自定义鉴权:从 x-tenant-id / x-user-id 解析身份(仅标注,不强制;缺头回退 default)。"""

from __future__ import annotations

from typing import Any

from langgraph_sdk import Auth

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict[Any, Any]) -> dict[str, Any]:
    tenant_id = headers.get("x-tenant-id") or "default"
    user_id = headers.get("x-user-id") or "anonymous"
    return {"identity": f"{tenant_id}:{user_id}", "tenant_id": tenant_id, "user_id": user_id}
