"""ゲームプロセスの生存・終了コード・CPU・RSS・スレッド数を監視する。"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable

import psutil

from ..events import Event


async def watch_process(
    proc: subprocess.Popen[bytes],
    publish: Callable[[Event], None],
    launch: int,
    stop: asyncio.Event,
    interval: float,
    stopped_by_harness: Callable[[], bool],
) -> None:
    """プロセスが終わったら proc_exit を1回出して戻る。生存中は proc_stats を定期的に出す。"""
    try:
        ps = psutil.Process(proc.pid)
        ps.cpu_percent(None)  # 初回呼び出しは基準取りのみ
    except psutil.Error:
        ps = None
    while True:
        code = proc.poll()
        if code is not None:
            publish(
                Event("proc", "proc_exit", {"code": code, "by_harness": stopped_by_harness()}, launch)
            )
            return
        if ps is not None:
            try:
                with ps.oneshot():
                    stats = {
                        "cpu": ps.cpu_percent(None),
                        "rss": ps.memory_info().rss,
                        "threads": ps.num_threads(),
                    }
                publish(Event("proc", "proc_stats", stats, launch))
            except psutil.Error:
                pass
        if stop.is_set() and not stopped_by_harness():
            return
        await asyncio.sleep(interval)
