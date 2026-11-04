"""通常のプレイの途中で、ある地点でときどき切り替える区間（ガチャプレイ・ボタンの網羅）。"""

import asyncio
import random
import time
from pathlib import Path

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event, Issue
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.input.pointer import Pointer
from yomawari.purpose import Burst, resolve
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState
from yomawari.watchdog import Harness

from .test_window import FakeWindows


def _session(purpose: str, **burst):
    p = resolve(purpose)
    plan = Burst(**{**p.burst.__dict__, "prob": 1.0, "share": 1.0, **burst})
    base = p.apply(FUZZ_DEFAULTS)
    st = GameState()
    run = FuzzRun(mode=p.mode, decider=RandomDecider(random.Random(1)), guard=Guard({}), rng=random.Random(1),
                  cfg=base, response_timeout=0.1, burst_plan=plan, burst_cfg=plan.apply(base))
    run.started -= 1000  # ランを始めてしばらくたった
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    events = []
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), events.append, 0, lambda _: None)
    return s, st, run, base, events


def _game(s, st, kind, **d):
    ev = Event("game", kind, d, 0)
    st.feed(ev)
    s.on_event(ev)


def test_burst_starts_at_a_new_screen_once_and_ends_after_its_time():
    s, st, run, base, events = _session("gacha")
    run.intent = "progress"
    assert run.mode == "random" and s.cfg["learn"] is True
    _game(s, st, "menu", name="ShopUI", modal=True, buttons=[{"name": "Buy", "rect": [0.1, 0.1, 0.2, 0.2]}])
    # 初めて来た画面で、ガチャプレイ（fuzz モード、イレギュラーな操作を多めに。経験・流れ・AI は使わない）に切り替える
    assert run.burst is not None and run.burst[0] == "画面 `ShopUI`"
    assert run.mode == "fuzz" and s.cfg["irregular_prob"] == 0.3 and s.cfg["learn"] is False
    assert s.cfg["use_flow"] is False and s.cfg["ai_choice"] is False
    # 時間になったら、通常のプレイに戻す
    run.burst = (run.burst[0], run.burst[1], time.monotonic() - 1, run.burst[3])
    st.feed(Event("game", "boot", {}, 0))
    st.feed(Event("game", "resolution", {"w": 2000, "h": 1000}, 0))
    asyncio.run(s._step())
    assert run.burst is None and run.mode == "random" and s.cfg is base and run.intent == "progress"
    assert [e.data["phase"] for e in events if e.kind == "burst"] == ["start", "end"]
    # 同じ画面では2回目は始めない。scene は別の地点
    _game(s, st, "menu_close", name="ShopUI")
    _game(s, st, "menu", name="ShopUI", modal=True, buttons=[{"name": "Buy", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert run.burst is None
    _game(s, st, "scene", name="main/s2")
    assert run.burst is not None and run.burst[0] == "scene `main/s2`"
    s._end_burst("テスト")
    notes = run.burst_notes()
    assert "2 か所で、区間（fuzz）に切り替えた" in notes[0] and "画面 `ShopUI`" in notes[0]


def test_buttons_burst_explores_and_keeps_its_intent():
    s, st, run, base, _ = _session("buttons")
    run.intent = "progress"
    _game(s, st, "menu", name="ConfigUI", modal=True, buttons=[{"name": "Back", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert run.mode == "random" and run.intent == "explore" and s.cfg["weights"]["click"] == 10
    s._choose_intent("タイトルに戻った")
    assert run.intent == "explore"  # 区間の間は、画面を見て回るまま
    s._end_burst("テスト")
    assert run.intent == "progress" and s.cfg["weights"]["click"] == FUZZ_DEFAULTS["weights"]["click"]


def test_burst_respects_share_of_the_run():
    s, st, run, base, _ = _session("gacha", share=0.5, sec=60)
    run.started = time.monotonic()  # 始めたばかり: 区間に使ってよい時間がまだない
    _game(s, st, "menu", name="ShopUI", modal=True, buttons=[{"name": "Buy", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert run.burst is None


def test_issue_found_during_burst_is_marked(tmp_path: Path):
    h = Harness(None, Path("x"), None, {}, Path("rules.yaml"), tmp_path, {}, {})
    published = []
    h._publish = published.append
    s, st, run, base, _ = _session("gacha")
    h._fuzz = run
    _game(s, st, "menu", name="ShopUI", modal=True, buttons=[{"name": "Buy", "rect": [0.1, 0.1, 0.2, 0.2]}])
    h._handle_issues([Issue("C-01", "critical", "異常終了", "落ちた", "k", 0, 0.0, "w")])
    assert "区間 fuzz の間: 画面 `ShopUI`" in h.issues[0].detail and h.issues[0].evidence["burst"] == "画面 `ShopUI`"
