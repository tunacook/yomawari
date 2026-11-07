"""ゲームプロセスの生存・終了コード・CPU・GPU・RSS・スレッド数と、マシンの状態を監視する。

マシンの状態（全体の CPU・GPU、熱、メモリの逼迫、画面のスリープ、前面かどうか、ハーネスが AI に聞いているか）は、
パフォーマンスの判定で、ゲーム以外が原因の時間を外すのに使う（DESIGN.md §10「パフォーマンス」）。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
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
    perf_stats: Callable[[int], dict] | None = None,
    ai_busy_since: Callable[[float], bool] | None = None,
) -> None:
    """プロセスが終わったら proc_exit を1回出して戻る。生存中は proc_stats を定期的に出す。

    after_op は、終わる直前（role_window_sec のうち）にハーネスが押したボタンを返す（DESIGN.md §9「役割の推定と判定」）。
    perf_stats は Platform.perf_stats（ゲームの GPU の累計時間とマシンの状態）、ai_busy_since(t) は、時刻 t 以降に
    ハーネスが AI（Ollama）に聞いていたか。
    """
    try:
        ps = psutil.Process(proc.pid)
        ps.cpu_percent(None)  # 初回呼び出しは基準取りのみ
        psutil.cpu_percent(None)
    except psutil.Error:
        ps = None
    ncpu = psutil.cpu_count() or 1
    last_gpu: tuple[float, int] | None = None  # (時刻, GPU の累計時間 ns)
    last_t = time.monotonic()
    while True:
        code = proc.poll()
        if code is not None:
            data = {"code": code, "by_harness": stopped_by_harness(), "after_op": after_op() if after_op else None}
            publish(Event("proc", "proc_exit", data, launch))
            return
        if ps is not None:
            try:
                with ps.oneshot():
                    stats: dict = {
                        "cpu": ps.cpu_percent(None),
                        "rss": ps.memory_info().rss,
                        "threads": ps.num_threads(),
                    }
                now = time.monotonic()
                # マシン全体の CPU（全コアに対する %）。ゲームの cpu は 1 コアを 100% とした値
                sysd: dict = {"cpu": psutil.cpu_percent(None), "ncpu": ncpu,
                              "swap": psutil.swap_memory().used}
                if perf_stats is not None:
                    more = await asyncio.to_thread(perf_stats, proc.pid)
                    gpu_ns = more.pop("gpu_ns", None)
                    if gpu_ns is not None:
                        if last_gpu is not None and now > last_gpu[0] and gpu_ns >= last_gpu[1]:
                            stats["gpu"] = round((gpu_ns - last_gpu[1]) / 1e9 / (now - last_gpu[0]) * 100, 1)
                        last_gpu = (now, gpu_ns)
                    sysd.update(more)
                if ai_busy_since is not None:
                    sysd["ai"] = ai_busy_since(last_t)
                last_t = now
                stats["sys"] = sysd
                publish(Event("proc", "proc_stats", stats, launch))
            except psutil.Error:
                pass
        if stop.is_set() and not stopped_by_harness():
            return
        await asyncio.sleep(interval)
