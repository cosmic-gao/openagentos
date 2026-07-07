"""助手资产文件管理:``.deepagent/<assistant_id>/`` 下的增删改移(app 直接操作共享磁盘)。

该目录存 ``skills/`` 与 ``.mcp.json``,由 app 管理(区别于每线程沙箱工作区 ``/workspace``)。
所有操作限定在单个 assistant 目录内:相对路径消毒,``..``、绝对路径、符号链接越界一律拒绝。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Entry:
    """assistant 目录内的一条目录项。"""

    path: str  # 相对 assistant 根的 posix 路径
    is_dir: bool
    size: int  # 文件字节数;目录为 0


def _resolve(base: Path, rel: str) -> Path:
    """相对路径 → base 内的绝对路径;穿越或越界(含符号链接)抛 ValueError。"""
    parts = [p for p in PurePosixPath((rel or "").strip("/")).parts if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"invalid path (traversal): {rel!r}")
    target = base.joinpath(*parts)
    resolved, root = target.resolve(), base.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError(f"path escapes assistant directory: {rel!r}")
    return target


def _rel(base: Path, target: Path) -> str:
    return target.relative_to(base).as_posix()


def ls(base: Path, rel: str = "") -> list[Entry]:
    """列目录项(目录不存在 → 空列表);目标是文件 → NotADirectoryError。"""
    target = _resolve(base, rel)
    if not target.exists():
        return []
    if not target.is_dir():
        raise NotADirectoryError(rel)
    return [
        Entry(
            path=_rel(base, child),
            is_dir=child.is_dir(),
            size=child.stat().st_size if child.is_file() else 0,
        )
        for child in sorted(target.iterdir())
    ]


def read(base: Path, rel: str) -> str:
    """读文本文件(UTF-8);不存在 → FileNotFoundError,非文本 → UnicodeDecodeError。"""
    target = _resolve(base, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target.read_text(encoding="utf-8")


def write(base: Path, rel: str, content: str) -> str:
    """写文件(存在则覆盖),按需建父目录;目标是目录 → IsADirectoryError。"""
    target = _resolve(base, rel)
    if target.is_dir():
        raise IsADirectoryError(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _rel(base, target)


def create(base: Path, rel: str, content: str = "") -> str:
    """新建文件,已存在 → FileExistsError。"""
    target = _resolve(base, rel)
    if target.exists():
        raise FileExistsError(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _rel(base, target)


def move(base: Path, src: str, dest: str) -> str:
    """移动/改名(文件或目录);源缺失 → FileNotFoundError,目标已存在 → FileExistsError。"""
    source, dest_path = _resolve(base, src), _resolve(base, dest)
    if source == base:
        raise ValueError("cannot move assistant root")
    if not source.exists():
        raise FileNotFoundError(src)
    if dest_path.exists():
        raise FileExistsError(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest_path))
    return _rel(base, dest_path)


def delete(base: Path, rel: str) -> bool:
    """删文件或目录(目录递归);不存在返回 False;不可删 assistant 根。"""
    target = _resolve(base, rel)
    if target == base:
        raise ValueError("cannot delete assistant root")
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return True
