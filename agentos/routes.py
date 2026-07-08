"""Aegra 自定义 HTTP 路由:线程文件下载 + .deepagent/<aid>/ 资产管理(skills、.mcp.json)。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agentos import assets, workspace
from agentos.config import get_settings

app = FastAPI(title="OpenAgentOS files")


@app.get("/files/{assistant_id}/{thread_id}/{rel:path}")
async def download(assistant_id: str, thread_id: str, rel: str) -> FileResponse:
    target = workspace.thread(get_settings(), assistant_id, thread_id) / rel
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


class CreateBody(BaseModel):
    path: str
    content: str = ""


class WriteBody(BaseModel):
    content: str


class MoveBody(BaseModel):
    src: str
    dest: str


# UnicodeDecodeError 是 ValueError 子类,须排在前
_HTTP: dict[type[Exception], int] = {
    UnicodeDecodeError: 415,
    FileNotFoundError: 404,
    FileExistsError: 409,
    IsADirectoryError: 400,
    NotADirectoryError: 400,
    ValueError: 400,
}


def _fail(exc: Exception) -> HTTPException:
    for kind, status in _HTTP.items():
        if isinstance(exc, kind):
            return HTTPException(status_code=status, detail=str(exc) or kind.__name__)
    return HTTPException(status_code=500, detail=str(exc))


def _dir(assistant_id: str):
    return workspace.assistant(get_settings(), assistant_id)


@app.get("/assistants/{assistant_id}/files", tags=["Assistants"])
def list_assets(assistant_id: str, path: str = "") -> list[assets.Entry]:
    try:
        return assets.ls(_dir(assistant_id), path)
    except Exception as exc:
        raise _fail(exc) from exc


@app.get("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def read_asset(assistant_id: str, rel: str) -> dict[str, str]:
    try:
        return {"path": rel, "content": assets.read(_dir(assistant_id), rel)}
    except Exception as exc:
        raise _fail(exc) from exc


@app.post("/assistants/{assistant_id}/files", status_code=201, tags=["Assistants"])
def create_asset(assistant_id: str, body: CreateBody) -> dict[str, str]:
    try:
        return {"created": assets.create(_dir(assistant_id), body.path, body.content)}
    except Exception as exc:
        raise _fail(exc) from exc


@app.put("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def write_asset(assistant_id: str, rel: str, body: WriteBody) -> dict[str, str]:
    try:
        return {"saved": assets.write(_dir(assistant_id), rel, body.content)}
    except Exception as exc:
        raise _fail(exc) from exc


@app.post("/assistants/{assistant_id}/move", tags=["Assistants"])
def move_asset(assistant_id: str, body: MoveBody) -> dict[str, str]:
    try:
        return {"moved": assets.move(_dir(assistant_id), body.src, body.dest), "from": body.src}
    except Exception as exc:
        raise _fail(exc) from exc


@app.delete("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def delete_asset(assistant_id: str, rel: str) -> dict[str, str]:
    try:
        removed = assets.delete(_dir(assistant_id), rel)
    except Exception as exc:
        raise _fail(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": rel}
