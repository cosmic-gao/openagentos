"""助手资产文件管理:.deepagent/<aid>/ 下的增删改移(app 直接操作共享磁盘)。

该目录存 skills/ 与 .mcp.json,区别于每线程沙箱工作区 /workspace。归属与合法性由上游业务方
保证,本层只做文件操作,让文件系统异常自然上抛(routes 再映射为 HTTP 码)。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Entry:
    path: str  # 相对 assistant 根的 posix 路径
    is_dir: bool
    size: int  # 文件字节数;目录为 0


def _resolve(base: Path, rel: str) -> Path:
    return base.joinpath(*PurePosixPath((rel or "").strip("/")).parts)


def _rel(base: Path, target: Path) -> str:
    return target.relative_to(base).as_posix()


def ls(base: Path, rel: str = "") -> list[Entry]:
    target = _resolve(base, rel)
    if not target.exists():
        return []
    return [
        Entry(_rel(base, c), c.is_dir(), c.stat().st_size if c.is_file() else 0)
        for c in sorted(target.iterdir())
    ]


def read(base: Path, rel: str) -> str:
    return _resolve(base, rel).read_text(encoding="utf-8")


def write(base: Path, rel: str, content: str) -> str:
    target = _resolve(base, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _rel(base, target)


def create(base: Path, rel: str, content: str = "") -> str:
    target = _resolve(base, rel)
    if target.exists():
        raise FileExistsError(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _rel(base, target)


def move(base: Path, src: str, dest: str) -> str:
    source, target = _resolve(base, src), _resolve(base, dest)
    if target.exists():
        raise FileExistsError(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return _rel(base, target)


def delete(base: Path, rel: str) -> bool:
    target = _resolve(base, rel)
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return True
