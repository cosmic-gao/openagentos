"""外部通道 webhook 回调:MS Teams(Bot Framework),每 agent 一个独立 bot。

路径带**平台 agent id**(稳定,assistant 删除重建也不变):每个 Azure Bot 的 messaging endpoint 指向
/webhooks/msteams/<agentId>,webhook 按该 id 反查 assistant 读取对应 agent 的 bot 凭据(见 agentos/msteams.py)。
平铺进 app.router.routes(见 routes/__init__.py),故为顶层 APIRoute、能被 aegra 鉴权注入。
Pydantic 不能惰性解析注解,本模块用即时注解、勿加 from __future__ import annotations。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agentos import msteams

router = APIRouter()


@router.post("/webhooks/msteams/{agent_id}", tags=["Channels"])
async def msteams_webhook(agent_id: str, request: Request) -> JSONResponse:
    """MS Teams(Bot Framework)回调:验完签名 JWT 立即 200 ACK(15 秒硬超时),
    agent run 与主动回复在后台任务完成——见 agentos/msteams.py。"""
    status, body = await msteams.webhook(
        agent_id, await request.body(), request.headers.get("authorization", "")
    )
    return JSONResponse(body, status_code=status)
