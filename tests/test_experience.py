"""進め方の経験（fuzz/experience.py）。"""

import json
import random
import time

import pytest

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.experience import Experience, Press
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import Op
from yomawari.fuzz.roles import RoleBook, RoleHints
from yomawari.input.pointer import Pointer
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState

from .test_window import FakeWindows

CFG = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}


def test_reach_counts_only_new_places():
    exp = Experience()
    assert exp.reach("line:a:1")
    assert not exp.reach("line:a:1")  # ロードのあとに読み直す行は進行ではない
    assert exp.reach("scene:main/s2")


def test_credit_once_per_press_and_factor():
    exp = Experience()
    adv = Press("reading", "advance", 0.0)
    load = Press("modal:SaveLoadUI", "click SaveLoadUI/Slot#1", 0.0)
    for _ in range(4):
        exp.pressed("reading", "advance")
    exp.pressed("modal:SaveLoadUI", "click SaveLoadUI/Slot#1")
    exp.credit([adv], "progress")
    exp.credit([adv], "progress")  # 同じ操作は1回だけ
    exp.credit([load], "rewind")
    assert exp.ops["reading"]["advance"] == {"tries": 4, "progress": 1, "rewind": 0}
    assert exp.factor("reading", "advance", 5, 0.1) == pytest.approx(1 + 4 * 0.25)
    assert exp.factor("modal:SaveLoadUI", "click SaveLoadUI/Slot#1", 5, 0.1) == pytest.approx(0.1)
    assert exp.factor("modal:SaveLoadUI", "untried", 5, 0.1) == 1.0
    # 本文送りは当たり前なので記録に載せない。選択画面などで初めて進んだ操作だけ載せる
    assert exp.new == []
    exp.pressed("modal:MemoryListUI", "click MemoryListUI/Slot#48")
    exp.credit([Press("modal:MemoryListUI", "click MemoryListUI/Slot#48", 0.0)], "progress")
    assert len(exp.new) == 1 and "Slot#48" in exp.new[0]


def test_save_keeps_roles_and_loads_next_run(tmp_path):
    path = tmp_path / "_experience" / "Game.json"
    book = RoleBook(RoleHints(), path)
    book.confirm("TitleUI/QuitButton", "exit", "終了コード 0 で終わった")
    book.save()
    exp = Experience(path)
    exp.pressed("modal:MemoryListUI", "click MemoryListUI/Slot#48")
    exp.credit([Press("modal:MemoryListUI", "click MemoryListUI/Slot#48", 0.0)], "progress")
    exp.save()
    book.save()  # 役割を書き直しても、経験は残る
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "TitleUI/QuitButton" in doc["roles"]
    assert doc["progress"]["modal:MemoryListUI"]["click MemoryListUI/Slot#48"] == {"tries": 1, "progress": 1, "rewind": 0}
    # 次のランは、覚えていることを使い、同じ操作をもう一度「初めて進んだ」とは書かない
    again = Experience(path)
    assert again.factor("modal:MemoryListUI", "click MemoryListUI/Slot#48", 5, 0.1) == 5
    again.credit([Press("modal:MemoryListUI", "click MemoryListUI/Slot#48", 0.0)], "progress")
    assert again.new == []


def test_decider_uses_learned_factor():
    d = RandomDecider(random.Random(1), 0.2)
    good, bad = Op("click", arg="good", novelty=True), Op("click", arg="bad", novelty=True)
    d.learned = lambda key, op: {"click good": 5.0, "click bad": 0.1}[op.label]
    assert d.weight("k", good) == 5.0 and d.weight("k", bad) == pytest.approx(0.1)
    advance = Op("advance")  # novelty のない操作にも掛ける
    d.learned = lambda key, op: 3.0
    assert d.weight("reading", advance) == 3.0


def _session():
    st = GameState()
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=CFG, response_timeout=0.1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    return s, st, run


def _game(s, st, kind, **d):
    ev = Event("game", kind, d, 0)
    st.feed(ev)
    s.on_event(ev)


def test_session_credits_progress_and_rewind_to_press_and_opener():
    s, st, run = _session()
    now = time.monotonic()
    # 本文を送って新しい行に届いた → 進行
    run.presses.append(Press("reading", "advance", now))
    _game(s, st, "line", line_id="main/s1:1", text="a")
    assert run.experience.ops["reading"]["advance"]["progress"] == 1
    # 右クリックメニューの「ロード」で開いた画面（寄り道）で、スロットを押してロードが始まった → 両方を巻き戻しとして数える
    _game(s, st, "menu", name="SaveLoadUI", modal=True, buttons=[{"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2]}])
    opener = Press("modal:RClickMenuUI", "click RClickMenuUI/ToLoad", now - 30)  # 開いたのは窓より前
    run.opened_by["SaveLoadUI"] = "RClickMenuUI/ToLoad"
    run.opened_press["SaveLoadUI"] = opener
    run.presses.append(Press("modal:SaveLoadUI", "click SaveLoadUI/Slot", time.monotonic()))
    _game(s, st, "load", phase="start")
    assert run.experience.ops["modal:SaveLoadUI"]["click SaveLoadUI/Slot"]["rewind"] == 1
    assert run.experience.ops["modal:RClickMenuUI"]["click RClickMenuUI/ToLoad"]["rewind"] == 1
    # ロードで戻った先の行は、もう見たので進行ではない
    _game(s, st, "menu_close", name="SaveLoadUI")
    run.presses.append(Press("reading", "advance", time.monotonic()))
    _game(s, st, "line", line_id="main/s1:1", text="a")
    assert run.experience.ops["reading"]["advance"]["progress"] == 1
    # 窓より前に押した操作には数えない
    run.presses.append(Press("reading", "right_click", time.monotonic() - 10))
    _game(s, st, "scene", name="main/s2")
    assert "right_click" not in run.experience.ops.get("reading", {})


def test_choices_are_keyed_by_text():
    s, st, run = _session()
    run.presses.append(Press("reading", "advance", time.monotonic()))
    _game(s, st, "choices", items=[{"id": "x1", "text": "はい", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert run.experience.ops["reading"]["advance"]["progress"] == 1
    run.presses.append(Press("reading", "advance", time.monotonic()))
    _game(s, st, "choices", items=[{"id": "x2", "text": "はい", "rect": [0.1, 0.1, 0.2, 0.2]}])  # id が変わっても同じ選択肢
    assert run.experience.ops["reading"]["advance"]["progress"] == 1
