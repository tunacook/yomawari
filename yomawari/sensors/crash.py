"""クラッシュダンプフォルダを監視し、起動後に出現したダンプを報告する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from ..events import Event


def find_new_dumps(
    crash_dir: Path, exe_name: str, since: float, suffixes: tuple[str, ...], seen: set[Path]
) -> list[Path]:
    if not crash_dir.is_dir():
        return []
    found = []
    for p in sorted(crash_dir.iterdir()):
        if p in seen or p.suffix not in suffixes or not p.name.startswith(exe_name):
            continue
        try:
            if p.stat().st_mtime < since:
                continue
        except OSError:
            continue
        seen.add(p)
        found.append(p)
    return found


async def watch_crash_dir(
    crash_dir: Path,
    exe_name: str,
    since: float,
    suffixes: tuple[str, ...],
    publish: Callable[[Event], None],
    launch: int,
    stop: asyncio.Event,
    interval: float,
    seen: set[Path],
) -> None:
    """since（wall clock の epoch 秒）以降に更新された exe_name のダンプを crash_dump として出す。"""
    while True:
        for p in await asyncio.to_thread(find_new_dumps, crash_dir, exe_name, since, suffixes, seen):
            publish(Event("crash", "crash_dump", {"path": str(p)}, launch))
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
