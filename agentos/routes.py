"""Aegra 自定义 HTTP 路由:线程文件下载 + .deepagent/<aid>/ 资产管理(skills、.mcp.json)。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from agentos import assets, workspace
from agentos.config import get_settings, safe_segment

# 必须直接挂 @app,勿 include_router:Aegra 的自定义路由鉴权只扫 app.routes 里的真 APIRoute。
app = FastAPI(title="OpenAgentOS files")


# 领域异常 → HTTP 码;UnicodeDecodeError 是 ValueError 子类,须排在前。
_STATUS: dict[type[Exception], int] = {
    UnicodeDecodeError: 415,
    FileNotFoundError: 404,
    FileExistsError: 409,
    IsADirectoryError: 400,
    NotADirectoryError: 400,
    ValueError: 400,
}


# 只认窄类型(assets/workspace 抛的),免去每个处理器重复 try/except;勿注册 Exception,会顶掉 Aegra 核心路由的处理器。
@app.exception_handler(ValueError)
@app.exception_handler(OSError)
async def _on_error(_request, exc: Exception) -> JSONResponse:
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


# 沙箱产物只按 thread 分区,不暴露 assistant_id;取自 sandbox/<tid>/storage/。
@app.get("/files/{thread_id}/{rel:path}")
def download(thread_id: str, rel: str) -> FileResponse:
    target = workspace.contained(workspace.storage(get_settings(), thread_id), rel)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


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


# 上传多文件(各带相对路径即重建目录树);extract=true 时按 zip 解压进 dest。
@app.post("/assistants/{assistant_id}/upload", status_code=201, tags=["Assistants"])
async def upload_files(
    assistant_id: str,
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> dict[str, list[str]]:
    blobs = [(f.filename or "", await f.read()) for f in files]
    return {"written": _put(_dir(assistant_id), blobs, dest, extract)}


@app.delete("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def delete_file(assistant_id: str, rel: str) -> dict[str, str]:
    if not assets.delete(_dir(assistant_id), rel):
        raise HTTPException(status_code=404, detail="file not found")
    return {"path": rel}


# ---- 跨多个 assistant 的批量分发;单个 assistant 失败只记录、不中断其余 ----
class BatchDelete(BaseModel):
    assistant_ids: list[str]
    path: str  # 要删的文件或目录(目录递归删)


@app.post("/files/upload", tags=["Assistants"])
async def batch_upload(
    assistant_ids: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> dict:
    blobs = [(f.filename or "", await f.read()) for f in files]  # 只读一次,复用到每个 assistant
    results: dict[str, dict] = {}
    for aid in assistant_ids:
        try:
            results[aid] = {"written": _put(_dir(aid), blobs, dest, extract)}
        except (ValueError, OSError) as exc:
            results[aid] = {"error": str(exc) or type(exc).__name__}
    return {"results": results}


@app.post("/files/delete", tags=["Assistants"])
def batch_delete(body: BatchDelete) -> dict:
    results: dict[str, dict] = {}
    for aid in body.assistant_ids:
        try:
            results[aid] = {"deleted": assets.delete(_dir(aid), body.path)}
        except (ValueError, OSError) as exc:
            results[aid] = {"error": str(exc) or type(exc).__name__}
    return {"results": results}
