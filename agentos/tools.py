"""agent 工具:把会话产物交给用户下载。

两个工具都用 LangChain `content_and_artifact`:content 是给模型看的简短确认(不含 URL,避免被复述成
失效链接),artifact 是给前端 UI 渲染下载块的结构化标识 {kind,scope,filename,path}——UI 据此打前端 BFF
同源代理下载,不直连后端。

- download_file —— 会话交付物拷进 Store、按 (会话、用户) 隔离(不可猜 token,记录 owner);artifact.path=/files/<token>。
- download_skill —— skill 包拷进 assistant 共享盘(.deepagent/<aid>/skills/);artifact.path=/assistants/<aid>/files/<rel>。
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote

from langchain_core.tools import BaseTool, tool

from agentos import artifacts, assets, sandbox, workspace
from agentos.config import Settings, current_thread_id


def relative(path: str) -> str:
    """沙箱路径 → /workspace 内相对路径。"""
    parts = [p for p in PurePosixPath(path).parts if p not in ("/", ".")]
    if parts[:1] == ["workspace"]:
        parts = parts[1:]
    return "/".join(parts)


async def _fetch(settings: Settings, assistant_id: str, identity: str, path: str) -> bytes | None:
    """从沙箱取回单个文件字节;路径穿越/不存在/读失败一律 None。"""
    if not relative(path) or ".." in PurePosixPath(path).parts:  # 拒绝路径穿越
        return None
    results = await sandbox.session(settings, assistant_id, identity).adownload_files([path])
    result = results[0] if results else None
    if result is None or result.error or result.content is None:
        return None
    return result.content


def build_download(settings: Settings, assistant_id: str, identity: str) -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def download_file(path: str) -> tuple[str, dict | None]:
        """Deliver a file from /workspace to the user.

        Use this for per-user deliverables the user should receive (reports,
        spreadsheets, images, archives). The file is copied from the sandbox
        into durable storage and shown to the user as a download panel in the
        chat UI — you do NOT need to print any link or URL, just briefly
        confirm the delivery. For a packaged skill use download_skill instead.
        Do NOT expose scratch or intermediate files.

        Args:
            path: Path of the file inside the sandbox (e.g. "/workspace/report.xlsx").
        """
        content = await _fetch(settings, assistant_id, identity, path)
        if content is None:
            return f"File not found: {path!r}", None
        rel = relative(path)
        thread_id = current_thread_id()
        token = await artifacts.save(identity, assistant_id, thread_id, rel, content)
        if token is None:
            return "Download unavailable: artifact store not configured.", None
        name = PurePosixPath(rel).name
        artifact = {"kind": "download", "scope": "file", "filename": name, "path": f"/files/{quote(token)}"}
        return f"Delivered {name} to the user's download panel.", artifact

    return download_file


def build_download_skill(settings: Settings, assistant_id: str, identity: str) -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def download_skill(path: str) -> tuple[str, dict | None]:
        """Deliver a packaged skill to the user.

        A skill is a reusable, assistant-shared capability. Package it as a
        STANDARD skill first: a `<name>/` directory holding a `SKILL.md` (YAML
        frontmatter with `name:` == <name> and `description:`, then the
        instructions) plus any supporting files, archived as a `.skill` **ZIP**
        (never tar/gzip). The package is shown to the user as a download panel
        in the chat UI — you do NOT need to print any link or URL, just briefly
        confirm the delivery. For regular per-user deliverables use download_file.

        Args:
            path: Path of the `.skill` ZIP inside the sandbox (e.g. "/workspace/foo.skill").
        """
        content = await _fetch(settings, assistant_id, identity, path)
        if content is None:
            return f"File not found: {path!r}", None
        # 必须是标准 skill 包:zip 且含 SKILL.md(平台按 zip 解包、按 <name>/SKILL.md 枚举/安装)。
        try:
            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except zipfile.BadZipFile:
            return (
                "Not a valid skill package: a .skill must be a ZIP archive, not tar/gzip. "
                "Rebuild as zip, e.g. `cd /workspace && "
                "python -c \"import shutil; shutil.make_archive('<name>','zip','.','<name>')\" "
                "&& mv <name>.zip <name>.skill`.",
                None,
            )
        if not any(n.rsplit("/", 1)[-1] == "SKILL.md" for n in names):
            return (
                "Invalid skill package: no SKILL.md found. A skill is a `<name>/` directory "
                "containing SKILL.md with `name:` and `description:` frontmatter.",
                None,
            )
        name = PurePosixPath(relative(path)).name
        base_dir = workspace.assistant(settings, assistant_id)
        # 拷进共享盘 skills/(磁盘 I/O 挪出事件循环);资产端点按 assistant 隔离、免 identity 即可取回。
        stored = await asyncio.to_thread(assets.save, base_dir, f"skills/{name}", content)
        artifact = {
            "kind": "download",
            "scope": "skill",
            "filename": name,
            "path": f"/assistants/{quote(assistant_id)}/files/{quote(stored)}",
        }
        return f"Delivered the skill {name} to the user's download panel.", artifact

    return download_skill
