"""一次性沙箱执行:新建临时沙箱执行 code、完成即销毁——单次、无状态、无会话。"""

import json
from typing import Annotated, Any

import httpx
from aegra_api.models.errors import BAD_REQUEST, UNAVAILABLE
from fastapi import APIRouter, HTTPException
from opensandbox.exceptions import SandboxException
from pydantic import BaseModel, Field, field_validator

from agentos import sandbox
from agentos.config import get_settings

router = APIRouter()

# 上限(best-effort;硬防护应在反代 client_max_body_size)
_MAX_CHARS = 1024 * 1024
_MAX_ITEMS = 256
_MAX_ITEM_CHARS = 64 * 1024
_MAX_TIMEOUT = 600

_Item = Annotated[str, Field(max_length=_MAX_ITEM_CHARS)]


class ExecuteBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=_MAX_CHARS, description="要执行的源码(单文件)")
    language: str = Field("python", description="解释器:python | bash | sh")
    args: list[_Item] = Field(default_factory=list, max_length=_MAX_ITEMS, description="命令行参数 → python: sys.argv[1:];bash: $1..")
    env: dict[str, _Item] = Field(default_factory=dict, max_length=_MAX_ITEMS, description="环境变量(名须匹配 [A-Za-z_][A-Za-z0-9_]*)")
    stdin: str | None = Field(None, max_length=_MAX_CHARS, description="标准输入文本")
    params: dict[str, Any] = Field(default_factory=dict, description="参数对象:python 里为变量 params;bash/sh 经 $PARAMS(JSON)")
    timeout: int = Field(30, ge=1, le=_MAX_TIMEOUT, description=f"超时秒数,范围 [1, {_MAX_TIMEOUT}]")

    @field_validator("params")
    @classmethod
    def _bound_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > _MAX_CHARS:
            raise ValueError(f"params JSON exceeds {_MAX_CHARS} characters")
        return value


class ExecuteResult(BaseModel):
    output: str = Field(..., description="按时间戳合并的 stdout + stderr")
    exit_code: int | None = Field(None, description="进程退出码,0 为成功")
    truncated: bool = Field(False, description="输出是否因后端上限被截断")


@router.post("/sandboxes/execute", tags=["Sandbox"], responses={**BAD_REQUEST, **UNAVAILABLE})
async def execute(body: ExecuteBody) -> ExecuteResult:
    """新建临时沙箱执行 code、完成即销毁。程序非零退出仍 200;语言不支持/env 名非法 → 400,落盘失败 → 503。"""
    try:
        result = await sandbox.run(
            get_settings(),
            body.code,
            language=body.language,
            args=body.args,
            env=body.env,
            stdin=body.stdin,
            params=body.params,
            timeout=body.timeout,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SandboxException as exc:
        # 后端不可达/就绪超时/配额耗尽:非 OSError/httpx 子类,须单列以兑现 503 契约(否则兜底成 500)。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ExecuteResult(output=result.output, exit_code=result.exit_code, truncated=result.truncated)
