"""助手资产文件(.deepagent/<aid>/)CRUD + 跨助手批量运维。归属鉴权由上游负责,本层只认证。"""

import asyncio
from pathlib import Path
from typing import Literal

from aegra_api.models.errors import BAD_REQUEST, CONFLICT, NOT_FOUND
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agentos import assets, workspace
from agentos.config import get_settings, safe_segment
from agentos.routes.common import _http_errors, _message, _status

router = APIRouter()

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _dir(assistant_id: str) -> Path:
    return workspace.assistant(get_settings(), assistant_id)


def _guard_upload(files: list[UploadFile]) -> None:
    if sum((f.size or 0) for f in files) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"upload exceeds {_MAX_UPLOAD_BYTES} bytes limit")


def _guard_bytes(data: bytes) -> None:
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"body exceeds {_MAX_UPLOAD_BYTES} bytes limit")


def _put(base: Path, blobs: list[tuple[str, bytes]], dest: str, extract: bool) -> list[str]:
    written: list[str] = []
    for name, data in blobs:
        if extract:
            written += assets.unpack(base, dest, data)
        else:
            written.append(assets.save(base, f"{dest}/{name}" if dest else name, data))
    return written


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


class BatchDelete(BaseModel):
    assistant_ids: list[str]
    path: str


@router.get("/assistants/{assistant_id}/files", tags=["Assistants"])
def list_files(
    assistant_id: str,
    path: str = "",
    recursive: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> FileList:
    """列目录项(recursive 递归子树);按 limit/offset 分页。"""
    with _http_errors():
        entries = (assets.walk if recursive else assets.ls)(_dir(assistant_id), path)
    return FileList(items=entries[offset : offset + limit], total=len(entries), limit=limit, offset=offset)


@router.get("/assistants/{assistant_id}/download", tags=["Assistants"])
def download_assets(assistant_id: str) -> Response:
    """把该助手 .deepagent/<aid>/ 打包成 zip 下载(跳过符号链接与 VCS 目录)。"""
    data = assets.pack(_dir(assistant_id))
    filename = f"{safe_segment(assistant_id)}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=data, media_type="application/zip", headers=headers)


@router.get("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"], responses={**NOT_FOUND})
def read_file(
    assistant_id: str,
    rel: str,
    request: Request,
    fmt: Literal["raw", "json"] | None = Query(None, alias="format"),
) -> Response:
    """读文件。默认原始字节流(支持 Range);Accept: application/json 或 ?format=json → {path,content}(仅 UTF-8);?format=raw 强制字节流。"""
    base = _dir(assistant_id)
    want_json = fmt == "json" or (fmt is None and "application/json" in request.headers.get("accept", ""))
    with _http_errors():
        target = workspace.contained(base, rel)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if want_json:
            return JSONResponse(FileContent(path=rel, content=assets.read(base, rel)).model_dump())
    return FileResponse(target, filename=target.name)


@router.put("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"], responses={**BAD_REQUEST})
async def put_file(assistant_id: str, rel: str, request: Request, response: Response) -> FileMeta:
    """幂等 upsert 写文件:体为原始字节。新建 → 201、覆盖 → 200。"""
    data = await request.body()
    _guard_bytes(data)
    with _http_errors():
        path, created = await asyncio.to_thread(assets.put, _dir(assistant_id), rel, data)
    response.status_code = 201 if created else 200
    return FileMeta(path=path, size=len(data))


@router.post("/assistants/{assistant_id}/move", tags=["Assistants"], responses={**CONFLICT})
def move_file(assistant_id: str, body: MoveBody) -> FileMeta:
    """移动/重命名 src → dest(目标已存在 → 409)。"""
    with _http_errors():
        path = assets.move(_dir(assistant_id), body.src, body.dest)
    return FileMeta(path=path)


@router.delete("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"], responses={**NOT_FOUND})
def delete_file(assistant_id: str, rel: str) -> DeleteResult:
    """删除文件或目录(目录递归);不存在 → 404。"""
    with _http_errors():
        deleted = assets.delete(_dir(assistant_id), rel)
    if not deleted:
        raise HTTPException(status_code=404, detail="file not found")
    return DeleteResult(path=rel, deleted=True)


@router.post("/assistants/{assistant_id}/upload", status_code=201, tags=["Assistants"])
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


@router.post("/files/upload", tags=["Assistants"])
async def batch_upload(
    response: Response,
    assistant_ids: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    dest: str = Form(""),
    extract: bool = Form(False),
) -> BatchResult:
    """把同一批文件分发到多个 assistant;逐个汇总,整体 207 Multi-Status。"""
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


@router.post("/files/delete", tags=["Assistants"])
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
