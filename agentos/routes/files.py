"""会话交付物下载:凭下载 token 从 Store 取回,按 (会话、用户) 隔离。

token 由 download_file 交付时生成(高熵、不可猜)、记录 owner identity+thread。此路由据 token 取回,并在请求
带可信身份头时校验调用者即 owner(不匹配 → 403);无身份头的纯浏览器导航凭 token 放行。不存在 → 404。
"""

import base64
from urllib.parse import quote

from aegra_api.models.errors import NOT_FOUND
from fastapi import APIRouter, HTTPException, Request, Response

from agentos import artifacts

router = APIRouter()


@router.get("/files/{token}", tags=["Files"], responses={**NOT_FOUND})
async def download(token: str, request: Request) -> Response:
    """下载会话交付物(凭 token 取回;带可信身份头则须为 owner);不存在 → 404,越权 → 403。"""
    record = await artifacts.load(token)
    if record is None:
        raise HTTPException(status_code=404, detail="file not found")
    user = request.scope.get("user")
    caller = getattr(user, "identity", None) or "anonymous"
    owner = record.get("identity") or "anonymous"
    # 带可信身份头的请求须匹配 owner(防泄露链接被他人已登录会话盗用);无身份头的纯导航凭 token 放行。
    if caller != "anonymous" and caller != owner:
        raise HTTPException(status_code=403, detail="forbidden")
    data = base64.b64decode(record["data"])
    media_type = record.get("media_type") or "application/octet-stream"
    filename = quote((record.get("rel") or "download").rsplit("/", 1)[-1])  # RFC 5987;防文件名破坏/注入响应头
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
