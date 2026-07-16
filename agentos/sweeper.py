"""后台回收空闲超期的会话目录:磁盘 sandbox/<tid>/ + 对应 langgraph checkpoint 行。

对应 langgraph store 的 TTL sweeper 范式:用文件系统 mtime 天然当"最后访问"、独立后台任务批删,
而非在写路径上即删。顺带补上 aegra 未做的 orphan-thread-sweeper——删目录时清 checkpoints/blobs/writes。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from agentos import workspace
from agentos.config import Settings

logger = logging.getLogger(__name__)


def _latest_mtime(path: Path) -> float:
    """目录树里最新的 mtime;取各文件 mtime(改内容即刷新),不依赖父目录 mtime。空目录取自身。"""
    latest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            latest = max(latest, child.stat().st_mtime)
        except OSError:
            continue
    return latest


def _find_expired(settings: Settings) -> list[tuple[str, Path]]:
    """扫 sandbox/ 下空闲超期的会话目录;纯同步 IO,由调用方放线程执行避免阻塞事件循环。"""
    base = workspace.root(settings) / workspace.SANDBOX
    if not base.is_dir():
        return []
    cutoff = time.time() - settings.sandbox_retention
    out: list[tuple[str, Path]] = []
    for entry in base.iterdir():
        try:
            if entry.is_dir() and _latest_mtime(entry) < cutoff:
                out.append((entry.name, entry))
        except OSError as exc:
            logger.warning("stat failed for %s: %s", entry, exc)
    return out


async def _delete_checkpoints(thread_id: str) -> None:
    """尽力清对应 thread 的 checkpoint 行;拿不到 checkpointer 则跳过(降级为只删盘)。

    目录名是 safe_segment(thread_id):thread_id 为 UUID 时与原值一致、精确命中;
    被消毒过则可能匹配不到(无害,孤儿留待人工清)。
    """
    try:
        from aegra_api.core.database import db_manager

        checkpointer = db_manager.get_checkpointer()
    except Exception as exc:
        logger.debug("checkpointer unavailable, skip checkpoint cleanup: %s", exc)
        return
    try:
        await checkpointer.adelete_thread(thread_id)
    except Exception as exc:
        logger.warning("adelete_thread failed for %s: %s", thread_id, exc)


async def _sweep_once(settings: Settings) -> int:
    expired = await asyncio.to_thread(_find_expired, settings)
    removed = 0
    for name, path in expired:
        await _delete_checkpoints(name)
        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except OSError as exc:
            logger.warning("failed to remove %s: %s", path, exc)
            continue
        removed += 1
        logger.info("swept idle session dir %s", name)
    return removed


async def run(settings: Settings) -> None:
    """周期回收空闲超期会话目录;retention/interval <=0 则关闭。单轮异常隔离,不终止循环。"""
    interval = settings.sandbox_sweep_interval
    if settings.sandbox_retention <= 0 or interval <= 0:
        logger.info("sandbox sweeper disabled")
        return
    logger.info(
        "sandbox sweeper started (retention=%ss interval=%ss)", settings.sandbox_retention, interval
    )
    while True:
        try:
            await asyncio.sleep(interval)
            removed = await _sweep_once(settings)
            if removed:
                logger.info("sandbox sweeper reclaimed %d session dir(s)", removed)
        except asyncio.CancelledError:
            logger.info("sandbox sweeper stopped")
            raise
        except Exception as exc:
            logger.warning("sandbox sweep round failed: %s", exc)
