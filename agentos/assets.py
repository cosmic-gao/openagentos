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
    """path 为相对 assistant 根的 posix 路径;目录的 size 记 0。"""

    path: str
    is_dir: bool
    size: int


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


_SKIP_DIRS = {".git"}
_MAX_UNPACK_BYTES = 256 * 1024 * 1024  # 解压总字节上限,防 zip bomb


def _iter_files(top: Path):
    """深度遍历产出文件,**不跟随符号链接**、跳过 VCS 目录。"""
    if not top.is_dir():
        return
    stack = [top]
    while stack:
        try:
            children = sorted(stack.pop().iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink():  # 不跟随 symlink,防越界读
                continue
            if child.is_dir():
                if child.name not in _SKIP_DIRS:
                    stack.append(child)
            elif child.is_file():
                yield child


def walk(base: Path, rel: str = "") -> list[Entry]:
    """递归子树返回全部 Entry(供前端一次性建树);跳过 VCS 目录与符号链接。"""
    root = _resolve(base, rel)
    if not root.exists():
        return []
    out: list[Entry] = []
    stack = [root]
    while stack:
        for child in sorted(stack.pop().iterdir()):
            if child.is_symlink():  # 不跟随 symlink,防越界枚举外部文件名/大小
                continue
            if child.is_dir():
                if child.name in _SKIP_DIRS:
                    continue
                out.append(Entry(_rel(base, child), True, 0))
                stack.append(child)
            elif child.is_file():
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
    target = _resolve(base, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _rel(base, target)


def put(base: Path, rel: str, data: bytes) -> tuple[str, bool]:
    """upsert 写字节(PUT 语义);返回 (相对路径, 是否新建)。"""
    target = _resolve(base, rel)
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _rel(base, target), created


def unpack(base: Path, rel: str, data: bytes) -> list[str]:
    """把 zip 解压进 rel 目录(成员过 contained 防 zip-slip,兼容反斜杠路径)。返回写入的相对路径。

    解压总量设上限防 zip bomb:先按头部声明的 file_size 预检,再按实际写入字节累计兜底(防声明撒谎)。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid zip: {exc}") from exc
    out: list[str] = []
    with archive as zf:
        declared = sum(info.file_size for info in zf.infolist() if not info.is_dir())
        if declared > _MAX_UNPACK_BYTES:
            raise ValueError(f"zip expands to {declared} bytes, exceeds limit {_MAX_UNPACK_BYTES}")
        written = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            member = info.filename.replace("\\", "/")
            target = _resolve(base, f"{rel}/{member}")
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = zf.read(info)
            written += len(payload)
            if written > _MAX_UNPACK_BYTES:
                raise ValueError(f"zip expansion exceeds limit {_MAX_UNPACK_BYTES}")
            target.write_bytes(payload)
            out.append(_rel(base, target))
    return out


def pack(base: Path, rel: str = "") -> bytes:
    """把 base/rel 目录打包成 zip 字节(供整目录下载);跳过 VCS 目录与符号链接,目录不存在则空包。

    经 `_iter_files` 不跟随 symlink:否则沙箱内不可信代码在共享卷放 symlink,打包会读出 app 宿主任意文件。
    """
    top = _resolve(base, rel)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for child in _iter_files(top):
            zf.write(child, child.relative_to(top).as_posix())
    return buf.getvalue()


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
