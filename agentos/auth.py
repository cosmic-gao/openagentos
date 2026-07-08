"""自定义鉴权:从请求头 x-tenant-id / x-user-id 解析身份(仅标注,不强制;缺头回退 default)。

aegra.json 的 auth.path 指向本模块的 auth。Aegra 每请求以 headers(dict,键小写)调用
@auth.authenticate;返回 dict 须含 identity,其余字段透传到 Aegra 的 User。归属鉴权
(能否访问某 assistant/thread)由上游业务方处理,本层不做。
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
