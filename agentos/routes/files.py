"""会话交付物下载:从 Store 读回,按 (user identity, assistant) 隔离(与记忆一致)。

identity 取自鉴权(非 URL),故调用者只能下到自己名下的交付物——越权/不存在一律 404;沙箱 ephemeral,
交付物在 download_file 交付时已拷进 Store,故沙箱销毁后链接仍有效。
"""

from urllib.parse import quote

from aegra_api.models.errors import NOT_FOUND
from fastapi import APIRouter, HTTPException, Request, Response

from agentos import artifacts

router = APIRouter()


@router.get("/files/{assistant_id}/{thread_id}/{rel:path}", tags=["Files"], responses={**NOT_FOUND})
async def download(assistant_id: str, thread_id: str, rel: str, request: Request) -> Response:
    """下载会话交付物(从 Store 读回,按调用者 identity + assistant 隔离);不存在/越权 → 404。"""
    user = request.scope.get("user")
    identity = getattr(user, "identity", None) or "anonymous"
    loaded = await artifacts.load(identity, assistant_id, thread_id, rel)
    if loaded is None:
        raise HTTPException(status_code=404, detail="file not found")
    data, media_type = loaded
    filename = quote(rel.rsplit("/", 1)[-1])  # RFC 5987;防文件名破坏/注入响应头
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
