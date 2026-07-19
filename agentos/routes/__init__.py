"""Aegra 自定义 HTTP 路由包:沙箱交付物下载 + 助手资产管理 + 一次性沙箱执行 + 用户反馈 score。

- 各资源用 APIRouter 定义,在此平铺进 app.router.routes(见 include 段):Aegra 的 enable_custom_route_auth
  只对顶层 APIRoute 注入鉴权,不下钻 Mount / FastAPI 0.139 include_router 的惰性 _IncludedRouter 包装。
- 显式重注册 HTTPException handler 产出 Agent Protocol 标准错误体:aegra 只补 user app 未定义的类型,而
  FastAPI 已占用该 handler,不重注册连核心路由都退化成 FastAPI 默认 {detail}。
- Pydantic 不能惰性解析注解,故各路由模块用即时注解、**勿加 from __future__ import annotations**。
- 归属鉴权:只认证、不复核资源归属;多租户隔离由上游负责。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from aegra_api.models.errors import AgentProtocolError, get_error_type
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agentos import scoring
from agentos.config import get_settings
from agentos.routes import assets, execute, feedback, files


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """关停时冲刷缓冲的 Langfuse score。会话/checkpoint 回收交给 aegra 原生 TTL(CHECKPOINTER_TTL_ENABLED)。"""
    try:
        yield
    finally:
        scoring.flush()


# root_path 取 public_url 的路径部分:反代挂子路径时令 /docs、openapi 指向带前缀 URL。
app = FastAPI(
    title="OpenAgentOS files",
    root_path=urlparse(get_settings().public_url).path.rstrip("/"),
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def _agent_protocol_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=AgentProtocolError(error=get_error_type(exc.status_code), message=str(exc.detail)).model_dump(),
        headers=getattr(exc, "headers", None),
    )


# 平铺进 app.router.routes(非 include_router:0.139 的 include_router 惰性包一层会藏住 APIRoute,
# 令 aegra 鉴权注入不到)。顺序:download 的 /files/{tid}/{rel} 与 batch 的 /files/upload|delete 不冲突。
for _module in (files, execute, assets, feedback):
    app.router.routes.extend(_module.router.routes)
