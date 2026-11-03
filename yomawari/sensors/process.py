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
    after_op: Callable[[], str | None] | None = None,
) -> None:
    """プロセスが終わったら proc_exit を1回出して戻る。生存中は proc_stats を定期的に出す。

    after_op は、終わる直前（role_window_sec のうち）にハーネスが押したボタンを返す（DESIGN.md §9「役割の推定と判定」）。
    """
    try:
        ps = psutil.Process(proc.pid)
        ps.cpu_percent(None)  # 初回呼び出しは基準取りのみ
    except psutil.Error:
        ps = None
    while True:
        code = proc.poll()
        if code is not None:
            data = {"code": code, "by_harness": stopped_by_harness(), "after_op": after_op() if after_op else None}
            publish(Event("proc", "proc_exit", data, launch))
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
