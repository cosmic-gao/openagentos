"""会话产物落盘：thread 作用域的独立宿主目录，供 Aegra 下载路由回传给用户。

与每助手持久磁盘（`.deepagent/<assistant_id>/` 的 skill/config）**分离**——产物按
`thread_id` 存到独立根目录 `AGENTOS_ARTIFACTS_DIR`（默认 `.artifacts`），绝不写入
`.deepagent/`。`export_artifact` 工具把沙箱内文件字节写到这里；Aegra 的
`/files/{thread}/{name}` 路由（见 `agentos/routes.py`）从这里读出并以附件下载。

安全：`thread_id` 与文件名逐段消毒（去斜杠、剥首尾点）、只取 basename，并在 `resolve`
里断言最终路径仍落在根目录内，杜绝路径穿越。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# 下载路由前缀（与 agentos/routes.py 的路由保持一致）。
DOWNLOAD_PREFIX = "/files"


def root() -> Path:
    """产物根目录（`AGENTOS_ARTIFACTS_DIR`，默认 `.artifacts`）。"""
    return Path(os.environ.get("AGENTOS_ARTIFACTS_DIR", ".artifacts")).resolve()


def _safe(part: str, fallback: str) -> str:
    """消毒单段路径：仅留安全字符，剥离首尾点/下划线，空则回退。"""
    cleaned = _UNSAFE.sub("_", (part or "").strip()).strip("._")
    return cleaned or fallback


def public_url(rel_path: str) -> str:
    """把相对下载路径拼上可选的公开 base URL（`AGENTOS_PUBLIC_BASE_URL`）。"""
    base = os.environ.get("AGENTOS_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{rel_path}" if base else rel_path


def store_bytes(thread_id: str, name: str, data: bytes) -> str:
    """把字节写到 `<root>/<thread>/<name>`，返回下载 URL 的相对路径。"""
    thread = _safe(thread_id, "default")
    filename = _safe(Path(name).name, "artifact")
    target = root() / thread / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"{DOWNLOAD_PREFIX}/{thread}/{filename}"


def resolve(thread_id: str, name: str) -> Path | None:
    """把 (thread, name) 解析为磁盘路径；越界/不存在/非普通文件返回 None。"""
    base = root()
    thread = _safe(thread_id, "default")
    filename = _safe(Path(name).name, "")
    if not filename:
        return None
    target = (base / thread / filename).resolve()
    if not target.is_relative_to(base):  # 防穿越兜底
        return None
    return target if target.is_file() else None
