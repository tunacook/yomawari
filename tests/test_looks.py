"""ボタンの見た目（fuzz/looks.py、sensors/screen.py）と、ほかの UI に覆われたボタン（covered）。"""

import asyncio
import random

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.looks import dim_buttons
from yomawari.fuzz.ops import normal_candidates
from yomawari.input.pointer import Pointer
from yomawari.sensors.screen import Shot, brightness
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState, Target

from .test_window import FakeWindows

W = H = 100
CFG = {**FUZZ_DEFAULTS, "settle_sec": 0}


def _shot(fills, bg=0):
    """fills: [(rect, 明るさ 0〜255)]。四角を塗った BGRA の画面。"""
    data = bytearray([bg, bg, bg, 255] * (W * H))
    for (x0, y0, x1, y1), v in fills:
        for y in range(int(y0 * H), int(y1 * H)):
            for x in range(int(x0 * W), int(x1 * W)):
                p = (y * W + x) * 4
                data[p:p + 3] = bytes([v, v, v])
    return Shot(W, H, W * 4, bytes(data))


def _slot(i, menu="MemoryListUI", name="Slot", w=0.15, nth=None):
    x = 0.05 + 0.2 * i
    return Target("button", menu, name, (x, 0.1, x + w, 0.3), nth=i if nth is None else nth)


def test_brightness_mean_and_peak():
    rect = (0.0, 0.0, 0.5, 0.5)
    mean, peak = brightness(_shot([(rect, 255)]), rect)
    assert abs(mean - 1) < 1e-6 and abs(peak - 1) < 1e-6
    # 文字だけ明るい（背景は暗い）ボタン: 平均は低いが、明るい部分は明るい
    mean, peak = brightness(_shot([((0.0, 0.0, 0.5, 0.1), 255)]), rect)
    assert mean < 0.3 and peak > 0.9
    assert brightness(_shot([]), (1.2, 1.2, 1.5, 1.5)) is None


def test_dark_slots_are_dim_among_same_name():
    slots = [_slot(i) for i in range(4)]
    # 回想一覧: サムネイルを明るさ 0.2 にしたロック中のスロット（Rachel と同じ）と、そのままのスロット
    shot = _shot([(slots[0].rect, 230), (slots[1].rect, 46), (slots[2].rect, 46), (slots[3].rect, 46)])
    dim, groups = dim_buttons(slots, shot, ratio=0.6, min_gap=0.1)
    assert dim == {s.label for s in slots[1:]}
    assert [(g.key, g.total, g.dim) for g in groups] == [("MemoryListUI/Slot", 4, 3)]


def test_grey_text_buttons_are_dim_among_same_size():
    # タイトル画面: 名前の違う同じ大きさのボタン。「つづきから」だけ文字が灰色（背景は透けている）
    names = ["NewGame", "Continue", "Config", "Quit"]
    btns = [_slot(i, menu="TitleUI", name=n, nth=0) for i, n in enumerate(names)]
    text = lambda r: (r[0], r[1], r[2], r[1] + 0.05)
    shot = _shot([(text(b.rect), 90 if b.name == "Continue" else 240) for b in btns], bg=20)
    dim, groups = dim_buttons(btns, shot, ratio=0.6, min_gap=0.1)
    assert dim == {"TitleUI/Continue"}
    assert groups[0].key.startswith("TitleUI/（")


def test_even_buttons_and_small_groups_are_not_dim():
    slots = [_slot(i) for i in range(4)]
    shot = _shot([(s.rect, 200 - 10 * i) for i, s in enumerate(slots)])  # 少しずつ違うだけ
    assert dim_buttons(slots, shot, 0.6, 0.1)[0] == set()
    two = slots[:2]
    shot = _shot([(two[0].rect, 230), (two[1].rect, 30)])
    assert dim_buttons(two, shot, 0.6, 0.1)[0] == set()  # 2つだけでは比べない


def test_dim_buttons_are_tried_later_and_noted():
    st = GameState()
    st.feed(Event("game", "resolution", {"w": 2000, "h": 1000}, 0))
    slots = [_slot(i) for i in range(4)]
    st.feed(Event("game", "menu", {"name": "MemoryListUI", "modal": True, "buttons": [
        {"name": "Slot", "rect": list(s.rect)} for s in slots]}, 0))
    shot = _shot([(slots[2].rect, 230)] + [(s.rect, 46) for i, s in enumerate(slots) if i != 2])
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1, shot=shot)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=CFG, response_timeout=0.1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda e: None, 0, lambda _: None)
    asyncio.run(s._look())
    assert s._dim == {"MemoryListUI/Slot#1", "MemoryListUI/Slot", "MemoryListUI/Slot#3"}
    ops = normal_candidates(st, run.guard, CFG["weights"], False, None, s._dim, CFG["dim_factor"])
    w = {op.target.label: op.weight for op in ops if op.target}
    assert w["MemoryListUI/Slot#2"] == CFG["weights"]["click"]
    assert w["MemoryListUI/Slot#1"] == CFG["weights"]["click"] * CFG["dim_factor"]
    assert len(run.notes) == 1 and "4 個のうち 3 個" in run.notes[0]
    asyncio.run(s._look())
    assert len(run.notes) == 1  # 同じ並びは1回だけ記録する
    windows.shot = None  # 画面収録の権限がないなど
    asyncio.run(s._look())
    assert s._dim == set()


def test_covered_buttons_are_not_targets():
    st = GameState()
    st.feed(Event("game", "menu", {"name": "RClickMenuUI", "modal": True, "buttons": [
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "CloseButton", "rect": [0.1, 0.3, 0.2, 0.4]},
    ]}, 0))
    assert [t.name for t in st.targets()] == ["CloseButton"]
    before = st.signature()
    st.feed(Event("game", "menu", {"name": "RClickMenuUI", "modal": True, "buttons": [
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "CloseButton", "rect": [0.1, 0.3, 0.2, 0.4], "covered": True},
    ]}, 0))
    assert st.signature() != before and st.targets() == []
    # 全部覆われていても、モーダルの画面なので動かす・抜ける操作は試す
    ops = normal_candidates(st, Guard({}), FUZZ_DEFAULTS["weights"], False)
    assert ops and all(op.target is None for op in ops) and {op.group for op in ops} == {"move", "leave"}


def test_v07_dim_button_that_responds():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    ok = {"kind": "click", "target": "MemoryListUI/Slot#3", "dim": True, "responded": True,
          "interfered": False, "heartbeat_age": 0.5}
    out = engine.feed(Event("harness", "op_result", ok, 0))
    assert [i.rule_id for i in out] == ["V-07"] and "Slot#3" in out[0].detail
    assert engine.feed(Event("harness", "op_result", ok, 0)) == []  # 同じボタンは1回だけ
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/B", "responded": False}, 0)) == []
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/C", "dim": False}, 0)) == []
