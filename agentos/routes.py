"""Aegra 自定义 HTTP 路由:沙箱产物下载 + .deepagent/<aid>/ 资产管理 + 一次性沙箱执行。

必须直接用 `@app` 装饰器挂路由(勿 include_router / mount 子应用):Aegra 的
`enable_custom_route_auth` 只对根 app 上的 APIRoute 注入鉴权。

错误处理:端点把 assets/workspace 的领域异常(ValueError/OSError 家族)转成 `HTTPException`,并**显式
注册 HTTPException handler** 产出 Agent Protocol 标准错误体 `{error, message, details}`(复用 aegra 的
`AgentProtocolError`)。必须显式注册的原因:aegra 有 custom app 时只 `merge_exception_handlers`(仅填
user app 未定义的类型),而 FastAPI 默认已占用 HTTPException handler,故 aegra 标准 handler 合并不进来,
不注册则连核心路由都退化成 FastAPI 默认 `{detail}`;因产出格式与 aegra 一致,注册它不破坏核心路由。
**不**注册裸 Exception/ValueError/OSError 处理器(避免顶掉核心兜底、污染核心路由)。

Aegra 以 custom_app_module 从文件加载本模块,Pydantic 无法惰性解析非内置类型注解,故用即时注解、
勿加 `from __future__ import annotations`。

归属鉴权:默认信任上游可信网关按 assistant_id/thread_id 分派(见 auth.py),不复核资源归属;多租户
隔离由上游负责,本层不做 user scoping。
"""

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal
from urllib.parse import urlparse

from aegra_api.models.errors import (
    BAD_REQUEST,
    CONFLICT,
    NOT_FOUND,
    UNAVAILABLE,
    AgentProtocolError,
    get_error_type,
)
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from agentos import assets, sandbox, scoring, sweeper, workspace
from agentos.config import get_settings, safe_segment


@asynccontextmanager
async def lifespan(_app):
    """后台 sweeper 回收空闲会话目录。aegra 核心 lifespan 外层包裹本 lifespan,故此处
    DB/checkpointer 已初始化。"""
    task = asyncio.create_task(sweeper.run(get_settings()))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        scoring.flush()


# root_path 取自 public_url 的路径部分:反代挂子路径(如 /aegra)时,令 /docs、/redoc、openapi
# 指向带前缀的 URL(反代须剥该前缀转发);与下载直链共用 AGENTOS_PUBLIC_URL 一个开关,独立部署留空。
app = FastAPI(
    title="OpenAgentOS files",
    root_path=urlparse(get_settings().public_url).path.rstrip("/"),
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def _agent_protocol_error(_request: Request, exc: HTTPException) -> JSONResponse:
    """HTTPException → Agent Protocol 标准错误体(为何须显式注册见模块 docstring)。"""
    return JSONResponse(
        status_code=exc.status_code,
        content=AgentProtocolError(
            error=get_error_type(exc.status_code), message=str(exc.detail)
        ).model_dump(),
        headers=getattr(exc, "headers", None),
    )


# ── 领域异常 → HTTP 码(端点内转 HTTPException,交上面的 handler 出标准错误体)──
_STATUS: dict[type[Exception], int] = {
    UnicodeDecodeError: 415,  # ValueError 子类,须排 ValueError 前
    FileNotFoundError: 404,
    FileExistsError: 409,
    IsADirectoryError: 400,
    NotADirectoryError: 400,
    ValueError: 400,
}


def _status(exc: Exception) -> int:
    return next((code for kind, code in _STATUS.items() if isinstance(exc, kind)), 500)


def _message(exc: Exception) -> str:
    # FileNotFoundError 的 str 常含宿主绝对路径,用通用文案不回显;其余是受控消息。
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    return str(exc) or type(exc).__name__


@contextmanager
def _http_errors() -> Iterator[None]:
    """assets/workspace 的领域异常(ValueError/OSError 家族)→ HTTPException。"""
    try:
        yield
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=_status(exc), detail=_message(exc)) from exc


def _dir(assistant_id: str) -> Path:
    return workspace.assistant(get_settings(), assistant_id)


# ── 上限(best-effort;硬防护应在反代 client_max_body_size)──
_MAX_CHARS = 1024 * 1024
_MAX_ITEMS = 256
_MAX_ITEM_CHARS = 64 * 1024
_MAX_TIMEOUT = 600
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_Item = Annotated[str, Field(max_length=_MAX_ITEM_CHARS)]


def _guard_upload(files: list[UploadFile]) -> None:
    if sum((f.size or 0) for f in files) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"upload exceeds {_MAX_UPLOAD_BYTES} bytes limit")


def _guard_bytes(data: bytes) -> None:
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"body exceeds {_MAX_UPLOAD_BYTES} bytes limit")


def _put(base: Path, blobs: list[tuple[str, bytes]], dest: str, extract: bool) -> list[str]:
    """blobs 落盘到 base/dest;extract 时按 zip 解包。返回写入的相对路径。"""
    written: list[str] = []
    for name, data in blobs:
        if extract:
            written += assets.unpack(base, dest, data)
        else:
            written.append(assets.save(base, f"{dest}/{name}" if dest else name, data))
    return written


# ── 响应模型 ──
class FileContent(BaseModel):
    path: str
    content: str


class FileMeta(BaseModel):
    path: str
    size: int | None = None


class DeleteResult(BaseModel):
    path: str
    deleted: bool


class FileList(BaseModel):
    items: list[assets.Entry]
    total: int
    limit: int
    offset: int


class UploadResult(BaseModel):
    written: list[str]


class BatchItem(BaseModel):
    assistant_id: str
    status: int
    written: list[str] | None = None
    deleted: bool | None = None
    error: str | None = None


class BatchResult(BaseModel):
    results: list[BatchItem]


class MoveBody(BaseModel):
    src: str
    dest: str


# ── 会话沙箱产物下载 ──
@app.get("/files/{thread_id}/{rel:path}", tags=["Files"], responses={**NOT_FOUND})
def download(thread_id: str, rel: str) -> FileResponse:
    """下载会话沙箱产物;只按 thread 分区、不暴露 assistant_id。FileResponse 原生支持 Range/ETag。"""
    with _http_errors():
        target = workspace.contained(workspace.storage(get_settings(), thread_id), rel)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


# ── 一次性沙箱执行 ──
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


@app.post("/sandboxes/execute", tags=["Sandbox"], responses={**BAD_REQUEST, **UNAVAILABLE})
async def execute(body: ExecuteBody) -> ExecuteResult:
    """新建临时沙箱执行 code(默认 python)、完成即销毁——单次、无状态、无会话。

    可选参数化:args 作命令行参数、env 注入环境变量、stdin 喂标准输入、params 传键值对对象
    (python 里为变量 params,bash/sh 为 $PARAMS);均经 shell 安全转义。
    程序非零退出仍返回 200(结果在 exit_code / output);语言不支持/env 变量名非法 → 400,沙箱落盘失败 → 503。
    """
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
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ExecuteResult(output=result.output, exit_code=result.exit_code, truncated=result.truncated)


# ── 助手资产文件(.deepagent/<aid>/)──
@app.get("/assistants/{assistant_id}/files", tags=["Assistants"])
def list_files(
    assistant_id: str,
    path: str = "",
    recursive: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> FileList:
    """列目录项(recursive 递归子树);按 limit/offset 分页,total 为该目录项总数。"""
    with _http_errors():
        entries = (assets.walk if recursive else assets.ls)(_dir(assistant_id), path)
    return FileList(items=entries[offset : offset + limit], total=len(entries), limit=limit, offset=offset)


@app.get("/assistants/{assistant_id}/download", tags=["Assistants"])
def download_assets(assistant_id: str) -> Response:
    """把该助手 .deepagent/<aid>/ 下全部内容打包成 zip 下载(跳过符号链接与 VCS 目录)。"""
    data = assets.pack(_dir(assistant_id))
    filename = f"{safe_segment(assistant_id)}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=data, media_type="application/zip", headers=headers)


@app.get("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"], responses={**NOT_FOUND})
def read_file(
    assistant_id: str,
    rel: str,
    request: Request,
    fmt: Literal["raw", "json"] | None = Query(None, alias="format"),
) -> Response:
    """读文件。内容协商(GitHub Contents 风格):默认原始字节流(FileResponse,按扩展名判 Content-Type、
    支持 Range);`Accept: application/json` 或 `?format=json` 返回 `{path, content}`(仅 UTF-8 文本,
    二进制会 415);`?format=raw` 强制字节流、优先于 Accept。"""
    base = _dir(assistant_id)
    want_json = fmt == "json" or (fmt is None and "application/json" in request.headers.get("accept", ""))
    with _http_errors():
        target = workspace.contained(base, rel)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if want_json:
            return JSONResponse(FileContent(path=rel, content=assets.read(base, rel)).model_dump())
    return FileResponse(target, filename=target.name)


@app.put("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"], responses={**BAD_REQUEST})
async def put_file(assistant_id: str, rel: str, request: Request, response: Response) -> FileMeta:
    """幂等 upsert 写文件:请求体为原始字节(文本即 UTF-8)。新建 → 201、覆盖 → 200,返回 {path, size}。"""
    data = await request.body()
    _guard_bytes(data)
    with _http_errors():
        path, created = await asyncio.to_thread(assets.put, _dir(assistant_id), rel, data)
    response.status_code = 201 if created else 200
    return FileMeta(path=path, size=len(data))


@app.post("/assistants/{assistant_id}/move", tags=["Assistants"], responses={**CONFLICT})
def move_file(assistant_id: str, body: MoveBody) -> FileMeta:
    """移动/重命名 src → dest(目标已存在 → 409)。"""
    with _http_errors():
        path = assets.move(_dir(assistant_id), body.src, body.dest)
    return FileMeta(path=path)


@app.delete("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"], responses={**NOT_FOUND})
def delete_file(assistant_id: str, rel: str) -> DeleteResult:
    """删除文件或目录(目录递归);不存在 → 404。"""
    with _http_errors():
        deleted = assets.delete(_dir(assistant_id), rel)
    if not deleted:
        raise HTTPException(status_code=404, detail="file not found")
    return DeleteResult(path=rel, deleted=True)


@app.post("/assistants/{assistant_id}/upload", status_code=201, tags=["Assistants"])
async def upload_files(
    assistant_id: str,
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> UploadResult:
    """上传多文件(各带相对路径即重建目录树);extract=true 时按 zip 解压进 dest。"""
    _guard_upload(files)
    blobs = [(f.filename or "", await f.read()) for f in files]
    with _http_errors():
        written = await asyncio.to_thread(_put, _dir(assistant_id), blobs, dest, extract)
    return UploadResult(written=written)


# ── 跨 assistant 批量(运维)──
class BatchDelete(BaseModel):
    assistant_ids: list[str]
    path: str


@app.post("/files/upload", tags=["Assistants"])
async def batch_upload(
    response: Response,
    assistant_ids: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> BatchResult:
    """把同一批文件分发到多个 assistant;逐个成功/失败汇总,整体 207 Multi-Status。"""
    _guard_upload(files)
    blobs = [(f.filename or "", await f.read()) for f in files]
    results: list[BatchItem] = []
    for aid in assistant_ids:
        try:
            written = await asyncio.to_thread(_put, _dir(aid), blobs, dest, extract)
            results.append(BatchItem(assistant_id=aid, status=201, written=written))
        except (ValueError, OSError) as exc:
            results.append(BatchItem(assistant_id=aid, status=_status(exc), error=_message(exc)))
    response.status_code = 207
    return BatchResult(results=results)


@app.post("/files/delete", tags=["Assistants"])
def batch_delete(body: BatchDelete, response: Response) -> BatchResult:
    """从多个 assistant 删同一路径(目录递归);逐个汇总,整体 207 Multi-Status。"""
    results: list[BatchItem] = []
    for aid in body.assistant_ids:
        try:
            deleted = assets.delete(_dir(aid), body.path)
            results.append(BatchItem(assistant_id=aid, status=200, deleted=deleted))
        except (ValueError, OSError) as exc:
            results.append(BatchItem(assistant_id=aid, status=_status(exc), error=_message(exc)))
    response.status_code = 207
    return BatchResult(results=results)


# ── 用户反馈 → Langfuse score ──
class FeedbackBody(BaseModel):
    trace_id: str  # 该 run 的 OTEL trace id(32-hex);score 靠它关联到 aegra 导出的 trace
    value: float | str
    name: str = "user_feedback"
    data_type: Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"] = "NUMERIC"
    comment: str | None = None


@app.post("/feedback", tags=["Feedback"])
def feedback(body: FeedbackBody) -> dict:
    """把用户反馈(👍/👎/评分)作为 Langfuse score 关联到指定 trace;缺 Langfuse 凭据则静默忽略。"""
    scoring.score(body.name, body.value, trace_id=body.trace_id, data_type=body.data_type, comment=body.comment)
    return {"ok": True}
