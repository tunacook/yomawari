"""セーブしたら、セーブの場所（persistentDataPath）のファイルが更新されたかを確かめる（F-01）。

ゲームの `save`（`phase: end`）を受け取ったら check_after_sec 待ち、セーブの場所の下で、そのセーブを始めたとき
（`phase: start` を受け取った時刻から slack_sec 前）より新しいファイルがあるかを見て、`save_file` イベントを出す。
ファイルの名前の付け方はゲーム・エンジンごとに違うので、どのファイルかは問わない。判定は detect/engine.py の F-01 が行う。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from ..events import Event


def newest_file(root: Path) -> tuple[Path, float] | None:
    """root の下でいちばん新しく更新されたファイルと、その更新時刻（time.time() と同じ基準）。"""
    best: tuple[Path, float] | None = None
    if not root.is_dir():
        return None
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            m = p.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best[1]:
            best = (p, m)
    return best


class SaveFileWatch:
    def __init__(self, save_dir: Path, publish: Callable[[Event], None], check_after: float, slack: float,
                 clock: Callable[[], float] = time.time) -> None:
        self.save_dir = save_dir
        self.publish = publish
        self.check_after = check_after
        self.slack = slack
        self.clock = clock
        self.starts: dict[str, float] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def on_event(self, ev: Event) -> None:
        if ev.source != "game" or ev.kind != "save":
            return
        slot = str(ev.data.get("slot"))
        if ev.data.get("phase") == "start":
            self.starts[slot] = self.clock()
        elif ev.data.get("phase") == "end":
            since = self.starts.pop(slot, self.clock())
            task = asyncio.get_running_loop().create_task(self._check(slot, since, ev.launch))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _check(self, slot: str, since: float, launch: int) -> None:
        await asyncio.sleep(self.check_after)
        self.publish(self.check(slot, since, launch))

    def check(self, slot: str, since: float, launch: int) -> Event:
        found = newest_file(self.save_dir)
        updated = found is not None and found[1] >= since - self.slack
        return Event("harness", "save_file", {
            "slot": slot,
            "updated": updated,
            "save_dir": str(self.save_dir),
            "newest": str(found[0].relative_to(self.save_dir)) if found else None,
            # いちばん新しいファイルが、セーブを始めたときの何秒前に更新されたか（更新されていないときの手がかり）
            "newest_before_sec": round(since - found[1], 1) if found else None,
        }, launch)

    def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
