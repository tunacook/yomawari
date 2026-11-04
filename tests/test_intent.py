"""プレイの目的と、再起動の前に抜け出す操作（fuzz/loop.py）。"""

import asyncio
import random

import pytest

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import normal_candidates
from yomawari.input.pointer import Pointer
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState

from .test_window import FakeWindows

CFG = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}


def _session(mode="random", cfg=CFG, run=None):
    st = GameState()
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    run = run or FuzzRun(mode=mode, decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                         rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    events = []
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), events.append, 0, lambda _: None)
    return s, st, run, events


def _game(s, st, kind, **d):
    ev = Event("game", kind, d, 0)
    st.feed(ev)
    s.on_event(ev)


def test_first_play_is_progress_and_title_return_chooses_again():
    s, st, run, events = _session(cfg={**CFG, "intent_progress": 1e-9, "intent_explore": 1.0})
    assert run.intent == "progress" and run.plays == 1  # ランの最初のプレイは、重みが小さくても「本編を進める」
    _game(s, st, "menu", name="TitleUI", modal=False, buttons=[{"name": "Start", "rect": [0.1, 0.5, 0.3, 0.6]}])
    _game(s, st, "menu_close", name="TitleUI")
    _game(s, st, "line", line_id="main/s1:1", text="a")
    _game(s, st, "line", line_id="main/s1:2", text="b")
    # タイトルに戻った。プレイを記録して、目的を選び直す
    _game(s, st, "menu", name="TitleUI", modal=False, buttons=[{"name": "Start", "rect": [0.1, 0.5, 0.3, 0.6]}])
    assert run.plays == 2 and run.intent == "explore"
    assert any("プレイ 1" in n and "本編を進める" in n and "2 件" in n for n in run.notes)
    assert [e.data["intent"] for e in events if e.kind == "intent"] == ["progress", "explore"]
    # 起動が終わったら、そのプレイも記録する
    s._end_play()
    assert any("プレイ 2" in n and "画面を見て回る" in n for n in run.notes) and run.intent is None


def test_first_play_follows_weights_when_progress_is_zero():
    # 目的（purpose）が「本編を進める」を選ばないもの（buttons など）なら、最初のプレイから見て回る
    _, _, run, _ = _session(cfg={**CFG, "intent_progress": 0.0, "intent_explore": 1.0})
    assert run.intent == "explore"


def test_fuzz_mode_has_no_intent():
    s, st, run, events = _session(mode="fuzz")
    assert run.intent is None and not [e for e in events if e.kind == "intent"]


def _reading_with_hud(st, s):
    _game(s, st, "menu", name="MemoryScenarioPlaying", modal=False,
          buttons=[{"name": "OpenDialog", "rect": [0, 0.0, 0.23, 0.08]}])
    _game(s, st, "line", line_id="memories/m1:1", text="a")


def test_intent_scales_detour_ops():
    s, st, run, _ = _session()
    _reading_with_hud(st, s)
    w = FUZZ_DEFAULTS["weights"]
    cands = normal_candidates(st, Guard({}), w, False)
    progress = {op.label: op.weight for op in s._apply_intent(cands)}
    assert progress["advance"] == w["advance"]  # 本文送りはそのまま
    assert progress["right_click"] == pytest.approx(w["right_click"] * CFG["progress_detour_factor"])
    assert progress["click MemoryScenarioPlaying/OpenDialog"] == pytest.approx(w["click"] * CFG["progress_detour_factor"])
    run.intent = "explore"
    explore = {op.label: op.weight for op in s._apply_intent(cands)}
    assert explore["right_click"] == pytest.approx(w["right_click"] * CFG["explore_detour_factor"])
    # モーダル（右クリックメニューなど）の中では変えない
    _game(s, st, "menu", name="RClickMenuUI", modal=True, buttons=[{"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2]}])
    cands = normal_candidates(st, Guard({}), w, False)
    assert s._apply_intent(cands) == cands


def test_recover_success_skips_restart():
    s, st, run, events = _session()
    _game(s, st, "line", line_id="main/s1:1", text="a")
    failed = []
    assert s.recover("S-02", lambda: failed.append(True))
    assert not s.recover("C-01", lambda: None)  # 異常終了・ハングは試さない
    # 抜け出す操作のあとに本文が来た
    s._recovery["ops"].append("key esc")
    _game(s, st, "line", line_id="main/s1:2", text="b")
    asyncio.run(s._recover_step())
    assert s._recovery is None and not failed
    assert any("S-02 のあと、`key esc` で抜け出せたので、再起動しなかった" in n for n in run.notes)
    assert [e.kind for e in events if e.kind == "recovered"] == ["recovered"]


def test_recover_gives_up_and_restarts():
    s, st, run, _ = _session()
    _game(s, st, "line", line_id="main/s1:1", text="a")
    failed = []
    s.recover("S-03", lambda: failed.append(True))
    s._recovery["left"] = 0
    s._recovery["ops"] = ["key esc", "advance"]
    asyncio.run(s._recover_step())
    assert failed == [True] and s._recovery is None
    assert any("S-03 のあと、`key esc` → `advance` を試しても抜け出せなかったので、再起動した" in n for n in run.notes)
    assert s.recover("S-02", lambda: None)  # 次の検出では、また試せる


class _NullDriver:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)


def test_recover_step_tries_escape_ops():
    st = GameState()
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg={**CFG, "recover_ops": 2}, response_timeout=0.05)
    driver = _NullDriver()
    s = FuzzSession(run, st, Pointer(windows, driver, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    _game(s, st, "boot")
    _game(s, st, "resolution", w=2000, h=1000, mode="Windowed")
    _game(s, st, "line", line_id="main/s1:1", text="a")
    failed = []
    s.recover("S-02", lambda: failed.append(True))
    asyncio.run(s._recover_step())
    asyncio.run(s._recover_step())
    ops = list(s._recovery["ops"])
    assert len(ops) == 2 and set(ops) <= {"key esc", "right_click", "advance"} and len(set(ops)) == 2  # 違う操作を順に試す
    assert driver.calls
    asyncio.run(s._recover_step())
    assert failed == [True]
