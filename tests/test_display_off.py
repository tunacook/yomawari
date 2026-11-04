"""ゲームが起動したあと、画面を消す（--display-off）。"""

import asyncio
from pathlib import Path

from yomawari.events import Event
from yomawari.platform.mac import parse_screen_lock
from yomawari.watchdog import Harness


def test_parse_screen_lock():
    assert parse_screen_lock("2026-11-04 sysadminctl[1:2] screenLock delay is immediate") == "immediate"
    assert parse_screen_lock("screenLock delay is 300 seconds") == "300 seconds"
    assert parse_screen_lock("screenLock is off") == "off"
    assert parse_screen_lock("unknown") is None


class _Plat:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def display_off(self):
        self.calls += 1
        return self.ok

    def screen_lock_delay(self):
        return "immediate"


def _harness(tmp_path: Path, plat) -> tuple[Harness, list[Event]]:
    h = Harness(None, Path("x"), plat, {}, Path("rules.yaml"), tmp_path, {}, {})
    published: list[Event] = []
    h._publish = published.append
    return h, published


def test_display_is_turned_off_after_the_game_boots(tmp_path: Path):
    plat = _Plat()
    h, published = _harness(tmp_path, plat)

    async def go():
        task = asyncio.create_task(h._display_off(0))
        await asyncio.sleep(0.6)
        assert plat.calls == 0  # ゲームが起動するまでは消さない
        h.state.booted = True
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert plat.calls == 1
    assert any("画面を消した（画面が消えてからロックされるまで: すぐ）" in n for n in h.notes)
    assert [e.kind for e in published] == ["display_off"]


def test_display_off_failure_is_noted(tmp_path: Path):
    h, _ = _harness(tmp_path, _Plat(ok=False))
    h.state.booted = True
    asyncio.run(h._display_off(0))
    assert any("画面を消せなかった" in n for n in h.notes)
