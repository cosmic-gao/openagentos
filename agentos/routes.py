"""Aegra 自定义 HTTP:从共享磁盘回传线程文件(share_file 生成的下载链接指向这里)。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from agentos import workspace
from agentos.config import get_settings, safe_segment

app = FastAPI(title="OpenAgentOS files")


@app.get("/files/{assistant_id}/{thread_id}/{rel:path}")
async def download(assistant_id: str, thread_id: str, rel: str) -> FileResponse:
    settings = get_settings()
    base = workspace.thread(
        settings, safe_segment(assistant_id, "default"), safe_segment(thread_id, "default")
    )
    target = (base / rel).resolve()
    if not target.is_relative_to(workspace.root(settings)) or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)
