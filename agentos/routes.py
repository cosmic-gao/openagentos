"""Aegra 自定义 HTTP 路由。

- ``/files/{aid}/{tid}/{rel}``:回传线程持久文件(``share_file`` 下载链接指向这里)。
- ``/assistants/{aid}/files`` · ``.../files/{rel}`` · ``/assistants/{aid}/move``:管理该
  assistant 的 ``.deepagent/<aid>/`` 资产(skills、.mcp.json)——列目录 / 读 / 新建 /
  改内容 / 移动改名 / 删除,均限定在该 assistant 目录内。

资产端点挂在 ``/assistants/{aid}/`` 下、共用 ``Assistants`` 分组:子路径 files/move 与
Aegra 自带的 assistant 子资源(latest/versions/schemas/graph/subgraphs)不重名,故互不干扰。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agentos import assets, workspace
from agentos.config import get_settings, safe_segment

app = FastAPI(title="OpenAgentOS files")


@app.get("/files/{assistant_id}/{thread_id}/{rel:path}")
async def download(assistant_id: str, thread_id: str, rel: str) -> FileResponse:
    settings = get_settings()
    # 边界必须是该线程目录本身,而非整个 workspace 根:否则 rel 里的 ../ 可越界到
    # 其它 assistant/thread(含 .deepagent/<aid>/.mcp.json 内的密钥)。
    base = workspace.thread(
        settings, safe_segment(assistant_id, "default"), safe_segment(thread_id, "default")
    ).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


# ── .deepagent/<assistant_id>/ 资产管理 ──────────────────────────────────────


class CreateBody(BaseModel):
    path: str
    content: str = ""


class WriteBody(BaseModel):
    content: str


class MoveBody(BaseModel):
    src: str
    dest: str


# 异常 → HTTP 码;UnicodeDecodeError 是 ValueError 子类,须排在前。
_ERRORS: list[tuple[type[Exception], int]] = [
    (UnicodeDecodeError, 415),
    (FileNotFoundError, 404),
    (FileExistsError, 409),
    (IsADirectoryError, 400),
    (NotADirectoryError, 400),
    (ValueError, 400),
]


def _dir(assistant_id: str) -> Path:
    return workspace.assistant(get_settings(), assistant_id)


def _fail(exc: Exception) -> HTTPException:
    for kind, status in _ERRORS:
        if isinstance(exc, kind):
            detail = "file is not UTF-8 text" if kind is UnicodeDecodeError else str(exc) or kind.__name__
            return HTTPException(status_code=status, detail=detail)
    raise exc


@app.get("/assistants/{assistant_id}/files", tags=["Assistants"])
def list_assets(assistant_id: str, path: str = "") -> list[assets.Entry]:
    try:
        return assets.ls(_dir(assistant_id), path)
    except Exception as exc:
        raise _fail(exc) from exc


@app.get("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def read_asset(assistant_id: str, rel: str) -> dict[str, str]:
    try:
        content = assets.read(_dir(assistant_id), rel)
    except Exception as exc:
        raise _fail(exc) from exc
    return {"path": rel, "content": content}


@app.post("/assistants/{assistant_id}/files", status_code=201, tags=["Assistants"])
def create_asset(assistant_id: str, body: CreateBody) -> dict[str, str]:
    try:
        created = assets.create(_dir(assistant_id), body.path, body.content)
    except Exception as exc:
        raise _fail(exc) from exc
    return {"created": created}


@app.put("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def write_asset(assistant_id: str, rel: str, body: WriteBody) -> dict[str, str]:
    try:
        saved = assets.write(_dir(assistant_id), rel, body.content)
    except Exception as exc:
        raise _fail(exc) from exc
    return {"saved": saved}


@app.post("/assistants/{assistant_id}/move", tags=["Assistants"])
def move_asset(assistant_id: str, body: MoveBody) -> dict[str, str]:
    try:
        dest = assets.move(_dir(assistant_id), body.src, body.dest)
    except Exception as exc:
        raise _fail(exc) from exc
    return {"moved": dest, "from": body.src}


@app.delete("/assistants/{assistant_id}/files/{rel:path}", tags=["Assistants"])
def delete_asset(assistant_id: str, rel: str) -> dict[str, str]:
    try:
        removed = assets.delete(_dir(assistant_id), rel)
    except Exception as exc:
        raise _fail(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": rel}
