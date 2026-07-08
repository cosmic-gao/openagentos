"""共享磁盘布局:.deepagent/<aid>/ 存助手资产,sandbox/<tid>/{storage,tmp} 存会话沙箱文件。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from agentos.config import Settings, safe_segment

WORKSPACE = "/workspace"
SKILLS = "/workspace/skills"
DEEPAGENT = ".deepagent"
SANDBOX = "sandbox"
MCP_FILE = ".mcp.json"

MEMORIES = "/memories"
MEMORY_FILE = "/memories/AGENTS.md"


def root(settings: Settings) -> Path:
    return Path(settings.workspace).expanduser().resolve()


def host_root(settings: Settings) -> str:
    """沙箱 bind mount 用的宿主路径;app 与沙箱不同机时经 AGENTOS_WORKSPACE_HOST 指定。"""
    return settings.workspace_host or str(root(settings))


def assistant(settings: Settings, assistant_id: str) -> Path:
    return root(settings) / DEEPAGENT / safe_segment(assistant_id)


def skills(settings: Settings, assistant_id: str) -> Path:
    return assistant(settings, assistant_id) / "skills"


def mcp(settings: Settings, assistant_id: str) -> Path:
    return assistant(settings, assistant_id) / MCP_FILE


def storage(settings: Settings, thread_id: str) -> Path:
    """沙箱持久文件区(挂到沙箱 /workspace);下载接口从此处取,只按 thread 分区。"""
    return root(settings) / SANDBOX / safe_segment(thread_id) / "storage"


def tmp(settings: Settings, thread_id: str) -> Path:
    """沙箱临时文件区(挂到沙箱 /tmp);只按 thread 分区。"""
    return root(settings) / SANDBOX / safe_segment(thread_id) / "tmp"


def under(settings: Settings, path: Path) -> str:
    """path 相对 workspace 根的 posix 子路径(沙箱挂载 subPath 用)。"""
    return path.relative_to(root(settings)).as_posix()


def contained(base: Path, rel: str) -> Path:
    """把相对路径限制在 base 内;越界(../绝对/符号链接逃逸)抛 ValueError。"""
    base = base.resolve()
    target = base.joinpath(*PurePosixPath((rel or "").strip("/")).parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"path escapes base: {rel!r}")
    return target


def ensure(settings: Settings, assistant_id: str) -> None:
    """确保助手目录、skills/ 与 .mcp.json 模板存在(幂等)。"""
    skills(settings, assistant_id).mkdir(parents=True, exist_ok=True)
    file = mcp(settings, assistant_id)
    if not file.exists():
        file.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")
