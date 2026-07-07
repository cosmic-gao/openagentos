"""Aegra 自定义 HTTP：会话产物下载路由。

`aegra.json` 的 `http.app` 指向本模块的 `app`；Aegra 会把核心路由并入本 app、合并
lifespan，并在 `enable_custom_route_auth=true` 时为这些路由套上 Aegra 鉴权依赖
（当前无鉴权即放行，将来配置 JWT 后自动生效）。路由从 thread 作用域的产物目录
（见 `agentos/artifacts.py`）以附件形式回传文件。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from agentos import artifacts

app = FastAPI(title="OpenAgentOS artifacts")


@app.get("/files/{thread_id}/{name}")
async def download_artifact(thread_id: str, name: str) -> FileResponse:
    """下载某线程导出的产物（以附件形式）。"""
    path = artifacts.resolve(thread_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    # FileResponse 带 filename 时默认 Content-Disposition: attachment。
    return FileResponse(path, filename=path.name)
