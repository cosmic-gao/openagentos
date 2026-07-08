"""共享磁盘布局:.deepagent/<aid>/ 存助手资产,<aid>/<tid>/ 存线程持久文件。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from agentos.config import Settings, safe_segment

WORKSPACE = "/workspace"
SKILLS = "/workspace/skills"
DEEPAGENT = ".deepagent"
MCP_FILE = ".mcp.json"

# 虚拟路径(非磁盘):/memories/ 经 CompositeBackend 路由到持久 store,跨线程
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


def thread(settings: Settings, assistant_id: str, thread_id: str) -> Path:
    return root(settings) / safe_segment(assistant_id) / safe_segment(thread_id)


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
