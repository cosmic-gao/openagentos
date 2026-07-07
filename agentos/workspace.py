"""每助手磁盘目录 .deepagent/<assistant_id>/：skills/、.mcp.json、config.json。

仅 skills/ 经 CompositeBackend 的 /assistant/skills/ 暴露给 agent；config.json（含密钥）与
.mcp.json 只由 graph 工厂读取，不进 agent 文件系统。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend

ASSISTANT_ROUTE = "/assistant/skills/"

_UNSAFE = re.compile(r"[^A-Za-z0-9._@+:~-]")


def home() -> Path:
    return Path(os.environ.get("AGENTOS_DATA_DIR", ".deepagent")).resolve()


def _safe_id(assistant_id: str) -> str:
    return _UNSAFE.sub("_", (assistant_id or "").strip()).strip("._") or "default"


def assistant_dir(assistant_id: str) -> Path:
    return home() / _safe_id(assistant_id)


def skills_dir(assistant_id: str) -> Path:
    return assistant_dir(assistant_id) / "skills"


def mcp_file(assistant_id: str) -> Path:
    return assistant_dir(assistant_id) / ".mcp.json"


def config_file(assistant_id: str) -> Path:
    return assistant_dir(assistant_id) / "config.json"


def ensure_assistant(assistant_id: str) -> Path:
    """确保目录及 skills/、.mcp.json、config.json 模板存在。"""
    root = assistant_dir(assistant_id)
    skills_dir(assistant_id).mkdir(parents=True, exist_ok=True)

    mcp = mcp_file(assistant_id)
    if not mcp.exists():
        mcp.write_text(json.dumps({"mcpServers": {}}, indent=2, ensure_ascii=False), encoding="utf-8")

    cfg = config_file(assistant_id)
    if not cfg.exists():
        template = {"OPENAI_BASE_URL": "", "OPENAI_API_KEY": "", "OPENAI_MODEL": ""}
        cfg.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
    return root


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(assistant_id: str) -> dict:
    """读 config.json，剔除空串值（空 → 回退全局 env）。"""
    return {k: v for k, v in _read_json(config_file(assistant_id)).items() if isinstance(v, str) and v.strip()}


def load_mcp_servers(assistant_id: str) -> dict:
    servers = _read_json(mcp_file(assistant_id)).get("mcpServers") or {}
    return servers if isinstance(servers, dict) else {}


def skills_backend(assistant_id: str) -> FilesystemBackend:
    return FilesystemBackend(root_dir=skills_dir(assistant_id), virtual_mode=True)
