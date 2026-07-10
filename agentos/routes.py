"""Aegra 自定义 HTTP 路由:沙箱产物下载 + .deepagent/<aid>/ 资产管理。

必须直接用 `@app` 装饰器挂路由(勿 include_router / mount 子应用):Aegra 的
`enable_custom_route_auth` 只对根 app 上的 APIRoute 注入鉴权。异常处理器只注册
assets/workspace 会抛的窄类型(ValueError/OSError),勿注册 Exception/HTTPException——
那会顶掉 Aegra 核心路由的 Agent Protocol 错误处理器。

归属鉴权:本路由默认信任上游可信网关按 assistant_id/thread_id 分派(见 auth.py),
不复核资源归属;要在本服务内强制,按 user 校验或加 @auth.on 处理器。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from agentos import assets, sandbox, workspace
from agentos.config import get_settings, safe_segment

# root_path 取自 public_url 的路径部分:反代挂子路径(如 /aegra)时,令 /docs、/redoc、openapi
# 指向带前缀的 URL(反代须剥该前缀转发);与下载直链共用 AGENTOS_PUBLIC_URL 一个开关,独立部署留空。
app = FastAPI(title="OpenAgentOS files", root_path=urlparse(get_settings().public_url).path.rstrip("/"))


_STATUS: dict[type[Exception], int] = {
    UnicodeDecodeError: 415,
    FileNotFoundError: 404,
    FileExistsError: 409,
    IsADirectoryError: 400,
    NotADirectoryError: 400,
    ValueError: 400,
}


@app.exception_handler(ValueError)
@app.exception_handler(OSError)
async def _on_error(_request, exc: Exception) -> JSONResponse:
    """领域异常 → HTTP 码。UnicodeDecodeError 是 ValueError 子类,须在 _STATUS 中排其前才命中 415。"""
    status = next((s for k, s in _STATUS.items() if isinstance(exc, k)), 500)
    return JSONResponse({"detail": str(exc) or type(exc).__name__}, status_code=status)


def _dir(assistant_id: str) -> Path:
    return workspace.assistant(get_settings(), assistant_id)


def _put(base: Path, blobs: list[tuple[str, bytes]], dest: str, extract: bool) -> list[str]:
    """把 (文件名, 字节) 落盘到 base/dest;extract 时按 zip 解压。返回写入的相对路径。"""
    written: list[str] = []
    for name, data in blobs:
        if extract:
            written += assets.unpack(base, dest, data)
        else:
            written.append(assets.save(base, f"{dest}/{name}" if dest else name, data))
    return written


class CreateBody(BaseModel):
    path: str
    content: str = ""


class WriteBody(BaseModel):
    content: str


class MoveBody(BaseModel):
    src: str
    dest: str


@app.get("/files/{thread_id}/{rel:path}")
def download(thread_id: str, rel: str) -> FileResponse:
    """下载会话沙箱产物;只按 thread 分区、不暴露 assistant_id,取自 sandbox/<tid>/storage/。"""
    target = workspace.contained(workspace.storage(get_settings(), thread_id), rel)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


class ExecuteBody(BaseModel):
    code: str
    language: str = "python"
    timeout: int | None = None


class ExecuteResult(BaseModel):
    output: str  # 按时间戳合并的 stdout + stderr
    exit_code: int | None = None  # 0 为成功
    truncated: bool = False


@app.post("/sandboxes/execute", tags=["Sandbox"])
async def execute(body: ExecuteBody) -> ExecuteResult:
    """新建临时沙箱执行 code(默认 python)、完成即销毁——单次、无状态、无会话。

    返回按时间戳合并的 stdout+stderr 与退出码。程序非零退出仍返回 200(结果在 exit_code /
    output);语言不支持返回 400。
    """
    result = await sandbox.run(
        get_settings(),
        body.code,
        language=body.language,
        timeout=body.timeout,
    )
    return ExecuteResult(output=result.output, exit_code=result.exit_code, truncated=result.truncated)


@app.get("/assistants/{assistant_id}/files", tags=["Assistants"])
def list_files(assistant_id: str, path: str = "", recursive: bool = False) -> list[assets.Entry]:
    lister = assets.walk if recursive else assets.ls
    return lister(_dir(assistant_id), path)


@app.get("/assistants/{assistant_id}/download", tags=["Assistants"])
def download_assets(assistant_id: str) -> Response:
    """把该助手 .deepagent/<aid>/ 下全部内容打包成 zip 下载。"""
    data = assets.pack(_dir(assistant_id))
    filename = f"{safe_segment(assistant_id)}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=data, media_type="application/zip", headers=headers)


@app.get("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def read_file(assistant_id: str, rel: str) -> dict[str, str]:
    return {"path": rel, "content": assets.read(_dir(assistant_id), rel)}


@app.post("/assistants/{assistant_id}/files", status_code=201, tags=["Assistants"])
def create_file(assistant_id: str, body: CreateBody) -> dict[str, str]:
    return {"path": assets.create(_dir(assistant_id), body.path, body.content)}


@app.put("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def write_file(assistant_id: str, rel: str, body: WriteBody) -> dict[str, str]:
    return {"path": assets.write(_dir(assistant_id), rel, body.content)}


@app.post("/assistants/{assistant_id}/move", tags=["Assistants"])
def move_file(assistant_id: str, body: MoveBody) -> dict[str, str]:
    return {"path": assets.move(_dir(assistant_id), body.src, body.dest)}


@app.post("/assistants/{assistant_id}/upload", status_code=201, tags=["Assistants"])
async def upload_files(
    assistant_id: str,
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> dict[str, list[str]]:
    """上传多文件(各带相对路径即重建目录树);extract=true 时按 zip 解压进 dest。"""
    blobs = [(f.filename or "", await f.read()) for f in files]
    return {"written": _put(_dir(assistant_id), blobs, dest, extract)}


@app.delete("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def delete_file(assistant_id: str, rel: str) -> dict[str, str]:
    if not assets.delete(_dir(assistant_id), rel):
        raise HTTPException(status_code=404, detail="file not found")
    return {"path": rel}


class BatchDelete(BaseModel):
    assistant_ids: list[str]
    path: str


@app.post("/files/upload", tags=["Assistants"])
async def batch_upload(
    assistant_ids: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> dict:
    """把同一批文件分发到多个 assistant;单个失败只记录、不中断其余。"""
    blobs = [(f.filename or "", await f.read()) for f in files]
    results: dict[str, dict] = {}
    for aid in assistant_ids:
        try:
            results[aid] = {"written": _put(_dir(aid), blobs, dest, extract)}
        except (ValueError, OSError) as exc:
            results[aid] = {"error": str(exc) or type(exc).__name__}
    return {"results": results}


@app.post("/files/delete", tags=["Assistants"])
def batch_delete(body: BatchDelete) -> dict:
    """从多个 assistant 删同一路径(目录递归);单个失败只记录、不中断其余。"""
    results: dict[str, dict] = {}
    for aid in body.assistant_ids:
        try:
            results[aid] = {"deleted": assets.delete(_dir(aid), body.path)}
        except (ValueError, OSError) as exc:
            results[aid] = {"error": str(exc) or type(exc).__name__}
    return {"results": results}
