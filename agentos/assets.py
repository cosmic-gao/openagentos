"""助手资产文件管理:.deepagent/<aid>/ 下的增删改移;异常上抛由 routes 映射 HTTP 码。"""

from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from agentos import workspace


@dataclass(frozen=True)
class Entry:
    path: str  # 相对 assistant 根的 posix 路径
    is_dir: bool
    size: int  # 文件字节数;目录为 0


def _resolve(base: Path, rel: str) -> Path:
    return workspace.contained(base, rel)


def _rel(base: Path, target: Path) -> str:
    return target.relative_to(base.resolve()).as_posix()


def ls(base: Path, rel: str = "") -> list[Entry]:
    target = _resolve(base, rel)
    if not target.exists():
        return []
    return [
        Entry(_rel(base, c), c.is_dir(), c.stat().st_size if c.is_file() else 0)
        for c in sorted(target.iterdir())
    ]


# 递归遍历子树,返回全部 Entry(目录+文件),供前端一次性构建文件树。
# 剪掉 .git 等 VCS 目录,避免把成千上万条对象文件拉回来。
_SKIP_DIRS = {".git"}


def walk(base: Path, rel: str = "") -> list[Entry]:
    root = _resolve(base, rel)
    if not root.exists():
        return []
    out: list[Entry] = []
    stack = [root]
    while stack:
        for child in sorted(stack.pop().iterdir()):
            if child.is_dir():
                if child.name in _SKIP_DIRS:
                    continue
                out.append(Entry(_rel(base, child), True, 0))
                stack.append(child)
            else:
                out.append(Entry(_rel(base, child), False, child.stat().st_size))
    return out


def read(base: Path, rel: str) -> str:
    return _resolve(base, rel).read_text(encoding="utf-8")


def write(base: Path, rel: str, content: str) -> str:
    target = _resolve(base, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _rel(base, target)


def save(base: Path, rel: str, data: bytes) -> str:
    """写入二进制内容(上传用);父目录自动建,已存在则覆盖。"""
    target = _resolve(base, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _rel(base, target)


def unpack(base: Path, rel: str, data: bytes) -> list[str]:
    """把 zip 解压进 rel 目录;每个成员过 contained 防 zip-slip。返回写入文件的相对路径。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid zip: {exc}") from exc
    out: list[str] = []
    with archive as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            member = info.filename.replace("\\", "/")  # 有些 Windows zip 用反斜杠
            target = _resolve(base, f"{rel}/{member}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            out.append(_rel(base, target))
    return out


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
