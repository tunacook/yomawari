"""エッジケース: 壊れやすいタイミングへの差し込みと、巻き戻しの覚え方（fuzz/rollback.py、DESIGN.md §9「エッジケース」）。"""

import asyncio
import random

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import Op, irregular_candidates
from yomawari.fuzz.rollback import RollbackWatch, edge_timing
from yomawari.input.pointer import Pointer
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState

from .test_window import FakeWindows

WHEEL = ("wheel_up", "1")
ROLLBACK_BUTTON = ("click", "BacklogUI/RollbackButton")


def _session(mode="random", **cfg):
    st = GameState()
    st.feed(Event("game", "boot", {}, 0))
    st.feed(Event("game", "resolution", {"w": 1280, "h": 720, "mode": "Windowed"}, 0))
    run = FuzzRun(mode=mode, decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}), rng=random.Random(1),
                  cfg={**FUZZ_DEFAULTS, "hover_wait_sec": 0, **cfg}, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda e: None, 0, lambda _: None)
    done = []

    async def fake_do(op, trigger=None):
        done.append((op.kind, op.target.label if op.target else op.arg, trigger))
        s._operated(op)
        return True

    s._do = fake_do
    return s, st, run, done


def _game(s, st, kind, **d):
    ev = Event("game", kind, d, 0)
    st.feed(ev)
    s.on_event(ev)


def test_watch_learns_operation_that_went_back_to_an_earlier_line():
    w = RollbackWatch()
    for line in ("a", "b", "c"):
        w.line(line)
    w.operated((WHEEL,))
    assert w.line("b") is True
    assert w.best() == (WHEEL,)
    # ロード・scene の切り替えを挟んで前の行が出たのは、操作による巻き戻しではない
    w.operated((("click", "TitleUI/ContinueButton"),))
    w.jumped()
    w.line("a")
    assert w.learned[(("click", "TitleUI/ContinueButton"),)] == 0


def test_rollback_button_in_backlog_is_learned_with_the_op_that_opened_it():
    # Rachel: ホイール上でバックログが開き、その巻き戻しボタンで前の行に戻る（run 2026-11-06_1012）
    s, st, run, done = _session()
    for n in range(5):
        _game(s, st, "line", line_id=f"main/s1:{n}", text="t")
    s._operated(Op("wheel_up", arg=1))
    _game(s, st, "menu", name="BacklogUI", modal=True,
          buttons=[{"name": "RollbackButton", "rect": [0.8, 0.1 + 0.1 * i, 0.9, 0.15 + 0.1 * i]} for i in range(3)])
    s._operated(Op("click", st.targets()[2]))
    _game(s, st, "menu_close", name="BacklogUI")
    _game(s, st, "line", line_id="main/s1:2", text="t")
    assert run.rollback.best() == (WHEEL, ROLLBACK_BUTTON)


def test_rollback_edge_replays_learned_sequence():
    s, st, run, done = _session(rollback_check_sec=0, rollback_menu_wait_sec=0)
    run.rollback.learned[(WHEEL, ROLLBACK_BUTTON)] = 1
    _game(s, st, "menu", name="BacklogUI", modal=True,
          buttons=[{"name": "RollbackButton", "rect": [0.8, 0.1, 0.9, 0.15]}])
    assert asyncio.run(s._rollback_edge("choice", run.rollback.best())) is True
    assert done[0][:2] == ("wheel_up", 1) and done[1][:2] == ("click", "BacklogUI/RollbackButton")
    assert all(t == "rollback:choice" for *_, t in done)
    # 前の行に戻らなかったことを数える（巻き戻せない場面なら正常、戻るはずなら不具合の手がかり）
    assert run.rollback_dead["choice"] == 1


def test_edge_timings_and_candidates():
    assert edge_timing("choices", {"items": []}) == "choice"
    assert edge_timing("choices", {"items": [{"id": "a"}]}) == "choices"
    assert edge_timing("load", {"phase": "end"}) == "load_end"
    assert edge_timing("transition", {"phase": "end"}) is None
    st = GameState()
    kinds = {op.kind for op in irregular_candidates(st, Guard({}), 10)}
    assert "menu_flash" in kinds and "rollback" not in kinds
    assert "rollback" in {op.kind for op in irregular_candidates(st, Guard({}), 10, rollback=True)}


def test_play_inserts_few_edges_and_not_on_every_line():
    async def run_events(mode, prob_key, prob):
        s, st, run, done = _session(mode, **{prob_key: prob, "trigger_delay_max_sec": 0})
        for n in range(5):
            _game(s, st, "line", line_id=f"main/s1:{n}", text="t")
        lines = s._trigger
        _game(s, st, "scene", name="main/s2")
        scene = s._trigger
        if scene is not None:
            await scene
        return lines, scene, run

    # 通常のプレイ（random）: 本文が出るたびには差し込まず、scene の境目などで edge_prob の確率で
    lines, scene, run = asyncio.run(run_events("random", "edge_prob", 1.0))
    assert lines is None and scene is not None
    assert sum(run.edge_sent.values()) == 1 and next(iter(run.edge_sent))[0] == "scene"
    lines, scene, run = asyncio.run(run_events("random", "edge_prob", 0.0))
    assert scene is None
    # ガチャプレイの区間（fuzz）: 本文が出たときにも trigger_prob の確率で
    lines, scene, run = asyncio.run(run_events("fuzz", "trigger_prob", 1.0))
    assert lines is not None
