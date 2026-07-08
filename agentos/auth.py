"""自定义鉴权:从 x-tenant-id / x-user-id 解析身份。

仅做身份标注、不强制:缺头回退 default:anonymous,Aegra 侧据 identity 隔离线程/记忆/store。
租户归属交由上游可信网关负责——网关须权威注入这两个头并禁止客户端伪造;自定义资产路由
(routes.py)同样默认信任该前提、不再复核归属。要在本服务内强制,改为缺头即
raise Auth.exceptions.HTTPException(status_code=401)。
"""

from __future__ import annotations

from typing import Any

from langgraph_sdk import Auth

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict[Any, Any]) -> dict[str, Any]:
    tenant_id = headers.get("x-tenant-id") or "default"
    user_id = headers.get("x-user-id") or "anonymous"
    return {"identity": f"{tenant_id}:{user_id}", "tenant_id": tenant_id, "user_id": user_id}
