"""共享磁盘布局:.deepagent/<assistant_id>/ 存助手资产,<assistant_id>/<thread_id>/ 存线程持久文件。

同一块盘挂给 app(读 .mcp.json、回传下载)与每个线程沙箱(bind/PVC + subPath):
- ``.deepagent/<aid>/skills/`` → 沙箱内 ``/workspace/skills``(assistant 级,跨线程共享)
- ``<aid>/<tid>/`` → 沙箱内 ``/workspace``(线程级,沙箱销毁后仍在)
"""

from __future__ import annotations

import json
from pathlib import Path

from agentos.config import Settings, safe_segment

WORKSPACE = "/workspace"
SKILLS = "/workspace/skills"
DEEPAGENT = ".deepagent"
MCP_FILE = ".mcp.json"

# 长期记忆(虚拟路径,非沙箱磁盘):/memories/ 经 CompositeBackend 路由到 StoreBackend,
# 跨线程持久。MEMORY_FILE 为启动时加载的 AGENTS.md,agent 用 edit_file 自维护。
MEMORIES = "/memories"
MEMORY_FILE = "/memories/AGENTS.md"


def root(settings: Settings) -> Path:
    return Path(settings.workspace).expanduser().resolve()


def host_root(settings: Settings) -> str:
    """沙箱 bind mount 用的宿主路径;app 与沙箱运行时不同机时经 AGENTOS_WORKSPACE_HOST 指定。"""
    return settings.workspace_host or str(root(settings))


def assistant(settings: Settings, assistant_id: str) -> Path:
    return root(settings) / DEEPAGENT / safe_segment(assistant_id, "default")


def skills(settings: Settings, assistant_id: str) -> Path:
    return assistant(settings, assistant_id) / "skills"


def mcp(settings: Settings, assistant_id: str) -> Path:
    return assistant(settings, assistant_id) / MCP_FILE


def thread(settings: Settings, assistant_id: str, thread_id: str) -> Path:
    return (
        root(settings)
        / safe_segment(assistant_id, "default")
        / safe_segment(thread_id, "default")
    )


def ensure(settings: Settings, assistant_id: str) -> None:
    """确保助手目录、skills/ 与 .mcp.json 模板存在(幂等)。"""
    skills(settings, assistant_id).mkdir(parents=True, exist_ok=True)
    file = mcp(settings, assistant_id)
    if not file.exists():
        file.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")
