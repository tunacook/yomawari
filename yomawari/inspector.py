"""yomawari inspect: ビルドを起動し、画面に出ている menu とボタン、描画領域を見る。指定したボタンを押すこともできる。

[guard.buttons] に書くボタン名を調べるときと、入力（座標変換と前面化）を実機で確かめるときに使う。
ラン（watchdog）と同じく、セーブと PlayerPrefs は起動前に退避し、終了後に戻す。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import launcher
from .config import BuildConfig
from .events import Event
from .input.base import InputDriver
from .input.pointer import Pointer, PointerError
from .platform import Platform
from .savestate import SaveState, resolve_target
from .sensors.logtail import YomawariRecord, tail_file
from .sensors.window import ClientArea
from .state import GameState

# 押す対象が出てから、アニメーションが落ち着くまで待つ時間
SETTLE_SEC = 1.0


@dataclass
class InspectResult:
    clicked: list[tuple[str, tuple[float, float]]] = field(default_factory=list)
    not_clicked: list[tuple[str, str]] = field(default_factory=list)  # (key, 理由)
    notes: list[str] = field(default_factory=list)
    exit_code: int | None = None


def _signature(state: GameState) -> Any:
    return (
        state.scene,
        state.resolution and tuple(state.resolution.values()),
        tuple(sorted((m.name, m.modal, tuple(b.key for b in m.buttons)) for m in state.menus.values())),
        tuple(c.key for c in state.choices),
        state.busy,
    )


async def inspect_build(
    exe: Path,
    build: BuildConfig,
    platform: Platform,
    runs_dir: Path,
    seconds: float,
    clicks: list[str],
    sensor_cfg: dict[str, Any],
    quit_timeout: float,
    driver_factory: Callable[[], InputDriver],
    on_change: Callable[[GameState, ClientArea | None], None],
    on_click: Callable[[str, tuple[float, float] | None, str | None], None],
) -> InspectResult:
    work = runs_dir / "_inspect"
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / f"{build.name}.player.log"
    log_path.unlink(missing_ok=True)
    result = InspectResult()

    saves = SaveState(runs_dir, platform)
    if saves.pending():
        result.notes += ["前回の未復元の退避を見つけたため復元しました"] + saves.restore()
    # 退避先は毎回別にする（前回の inspect の退避と混ざらないように）
    backup_dir = work / f"{build.name}_{datetime.now():%Y%m%d_%H%M%S}"
    result.notes += saves.backup(backup_dir, resolve_target(build, platform), build.initial_save)

    state = GameState()
    proc = launcher.launch(exe, build, log_path)
    state.feed(Event("harness", "launch", {"pid": proc.pid}, 0))
    pointer = Pointer(platform, driver_factory(), proc.pid, state) if clicks else None
    pending = list(clicks)
    stop = asyncio.Event()

    def on_records(recs: list[Any]) -> None:
        for r in recs:
            if isinstance(r, YomawariRecord):
                state.feed(Event("game", str(r.data["ev"]), r.data, 0))

    tail = asyncio.create_task(
        tail_file(log_path, on_records, stop, float(sensor_cfg["log_poll_sec"]), float(sensor_cfg["log_flush_sec"]))
    )
    last_sig: Any = None
    seen_at: dict[str, float] = {}
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            sig = _signature(state)
            if sig != last_sig:
                last_sig = sig
                on_change(state, _area_or_none(pointer, platform, proc.pid, state))
            if pending and pointer is not None and not state.busy:
                key = pending[0]
                target = state.find(key)
                if target is None:
                    seen_at.pop(key, None)
                elif time.monotonic() - seen_at.setdefault(key, time.monotonic()) >= SETTLE_SEC:
                    pending.pop(0)
                    try:
                        pos = await asyncio.to_thread(pointer.click, target)
                        result.clicked.append((key, pos))
                        on_click(key, pos, None)
                    except PointerError as e:
                        result.not_clicked.append((key, str(e)))
                        on_click(key, None, str(e))
            await asyncio.sleep(0.1)
        result.not_clicked += [(k, "時間内に画面に出ませんでした") for k in pending]
        result.exit_code = proc.poll()
    finally:
        await asyncio.to_thread(launcher.stop, proc, quit_timeout)
        stop.set()
        await asyncio.gather(tail, return_exceptions=True)
        result.notes += saves.restore()
    return result


def _area_or_none(pointer: Pointer | None, platform: Platform, pid: int, state: GameState) -> ClientArea | None:
    # inspect は押さないときも描画領域を表示したいので、Pointer がなければその場で作る
    p = pointer or Pointer(platform, _NoInput(), pid, state)
    try:
        return p.area()
    except PointerError:
        return None


class _NoInput:
    def move(self, x: float, y: float) -> None: ...
    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None: ...
    def scroll(self, x: float, y: float, amount: int) -> None: ...
    def key(self, name: str) -> None: ...
    def hotkey(self, *names: str) -> None: ...
