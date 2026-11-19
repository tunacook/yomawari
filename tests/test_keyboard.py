"""入力の方式（--input・--input-mix。fuzz/keyboard.py、DESIGN.md §9「入力の方式」）。"""

import asyncio
import random

import pytest

from yomawari.config import FUZZ_DEFAULTS
from yomawari.fuzz.keyboard import InputError, InputStyle
from yomawari.fuzz.ops import Op, reading_candidates
from yomawari.sensors.window import ScreenRect, WindowInfo

from .test_edge import _game, _session
from .test_window import RecordingDriver

WEIGHTS = FUZZ_DEFAULTS["weights"]


class KeyDriver(RecordingDriver):
    def __init__(self):
        super().__init__()
        self.keys = []

    def key(self, name):
        self.keys.append(name)


def test_from_cfg_checks_values():
    s = InputStyle.from_cfg({"input": "keyboard", "input_mix": 0.2})
    assert s.main == "keyboard" and s.keyboard_share == pytest.approx(0.8)
    assert InputStyle.from_cfg(FUZZ_DEFAULTS).keyboard_share == 0  # 既定は今までどおりマウスだけ
    for bad in ({"input": "pad"}, {"input_mix": 1.5}):
        with pytest.raises(InputError):
            InputStyle.from_cfg(bad)


def test_mouse_without_mix_keeps_every_op():
    s = InputStyle()
    rng = random.Random(1)
    for op in reading_candidates(WEIGHTS):
        assert s.convert(op, rng) is op
    assert s.scale(reading_candidates(WEIGHTS)) == reading_candidates(WEIGHTS)


def test_keyboard_converts_advance_and_menu_only():
    s = InputStyle("keyboard", 0.0)
    rng = random.Random(1)
    assert s.convert(Op("advance"), rng).via_key == "enter"
    assert s.convert(Op("right_click"), rng).via_key == "esc"
    # イレギュラー操作と、ハーネスが確かめるための本文送りは、決まった押し方のまま
    assert s.convert(Op("advance", irregular=True), rng).via_key is None
    assert s.convert(Op("advance", by="mute_check"), rng).via_key is None
    assert s.convert(Op("advance", by="escape"), rng).via_key is None
    assert s.convert(Op("wheel_up", arg=1), rng).via_key is None
    assert s.used == {"advance": 1, "right_click": 1}


def test_keyboard_main_drops_or_scales_mouse_only_ops():
    cands = reading_candidates(WEIGHTS) + [Op("drag", arg="up", group="move")]
    kinds = [op.kind for op in InputStyle("keyboard", 0.0).scale(cands)]
    assert kinds == ["advance", "right_click"]
    scaled = {op.kind: op.weight for op in InputStyle("keyboard", 0.25).scale(cands)}
    assert scaled["wheel_up"] == WEIGHTS["wheel_up"] * 0.25 and scaled["advance"] == WEIGHTS["advance"]
    # ほかに操作がなければ、そのまま残す
    only = [Op("drag", arg="up", group="move")]
    assert InputStyle("keyboard", 0.0).scale(only) == only


def test_mix_share_decides_how_often_keys_are_used():
    rng = random.Random(3)
    s = InputStyle("mouse", 0.2)
    keyed = sum(s.convert(Op("advance"), rng).via_key is not None for _ in range(2000))
    assert 300 < keyed < 500


def test_advance_key_is_learned_from_whether_the_text_moved():
    s = InputStyle("keyboard", 0.0, ("enter", "space"), learn_tries=2)
    rng = random.Random(1)
    # まだ試し足りないキーを先に
    for _ in range(2):
        assert s.convert(Op("advance"), rng).via_key == "enter"
        s.advanced("enter", False)
    for _ in range(2):
        assert s.convert(Op("advance"), rng).via_key == "space"
        s.advanced("space", True)
    assert s.convert(Op("advance"), rng).via_key == "space"
    assert not s.gave_up()


def test_no_advance_key_works_falls_back_to_mouse():
    s = InputStyle("keyboard", 0.0, ("enter",), learn_tries=2)
    rng = random.Random(1)
    for _ in range(2):
        s.advanced(s.convert(Op("advance"), rng).via_key, False)
    assert s.gave_up() is True and s.gave_up() is False  # 記録に書くのは 1 回だけ
    assert s.convert(Op("advance"), rng).via_key is None
    assert s.convert(Op("right_click"), rng).via_key == "esc"  # メニューはキーのまま


def test_session_presses_the_key_and_learns_it():
    s, st, run, _ = _session()
    run.input_style = InputStyle("keyboard", 0.0, ("enter", "space"), learn_tries=1)
    drv = KeyDriver()
    s.pointer.driver = drv
    s.pointer.platform.info = WindowInfo(1, ScreenRect(100, 50, 1280, 748), True)  # 1280x720 に合うウィンドウ
    _game(s, st, "line", line_id="s:1", text="a")
    _game(s, st, "line_done")
    n = [1]

    async def do(op, trigger=None):
        # space でだけ本文が進むゲーム
        s._input(op)
        if op.via_key == "space":
            n[0] += 1
            _game(s, st, "line", line_id=f"s:{n[0]}", text=str(n[0]))
        return True

    s._do = do
    published = []
    s.publish = published.append
    for _ in range(3):
        op = run.input_style.convert(Op("advance"), run.rng)
        asyncio.run(s._normal(op, "reading", False))
    assert drv.keys == ["enter", "space", "space"] and drv.clicks == []
    assert run.input_style.worked == {"space": 2}
    vias = [e.data.get("via") for e in published if e.kind == "op_result"]
    assert vias == ["enter", "space", "space"]


# --- ボタンを方向キーでたどって押す ---


def test_next_direction_follows_the_target():
    from yomawari.fuzz.keyboard import next_direction

    a, below, right = (0.1, 0.1, 0.2, 0.2), (0.1, 0.5, 0.2, 0.6), (0.6, 0.1, 0.7, 0.2)
    assert next_direction(a, below, set()) == "down"
    assert next_direction(a, right, set()) == "right"
    assert next_direction(a, (0.6, 0.3, 0.7, 0.4), {"right"}) == "down"  # 右下: 右が動かなければ下
    assert next_direction(a, below, {"down"}) is None  # 目的のほうへ向かない向きは選ばない


def test_click_becomes_navigation_unless_unreachable():
    from yomawari.fuzz.keyboard import NAV
    from yomawari.state import Target

    s = InputStyle("keyboard", 0.0)
    t = Target("button", "UI", "A", (0.1, 0.1, 0.2, 0.2))
    rng = random.Random(1)
    assert s.convert(Op("click", t), rng).via_key == NAV
    s.navigated("UI/A", False)
    assert s.convert(Op("click", t), rng).via_key is None
    assert any("たどり着けなかったボタン" in n and "`UI/A`" in n for n in s.notes())


class GridDriver(KeyDriver):
    """方向キーで、縦に並んだボタンのフォーカスを動かすゲームの代わり。moves=False ならフォーカスが動かない。"""

    def __init__(self, s, st, names, moves=True):
        super().__init__()
        self.s, self.st, self.names, self.moves = s, st, names, moves
        self.at = 0

    def key(self, name):
        super().key(name)
        if self.moves and name in ("down", "up"):
            self.at = max(0, min(len(self.names) - 1, self.at + (1 if name == "down" else -1)))
            _game(self.s, self.st, "selected", menu="UI", name=self.names[self.at], nth=0)


def _menu_session(moves=True):
    s, st, run, _ = _session(nav_wait_sec=0.2)
    s.pointer.platform.info = WindowInfo(1, ScreenRect(100, 50, 1280, 748), True)
    names = ["A", "B", "C"]
    _game(s, st, "menu", name="UI", modal=True, buttons=[
        {"name": n, "rect": [0.1, 0.1 + 0.2 * i, 0.3, 0.2 + 0.2 * i], "type": "button"} for i, n in enumerate(names)])
    _game(s, st, "selected", menu="UI", name="A", nth=0)
    drv = GridDriver(s, st, names, moves)
    s.pointer.driver = drv
    run.input_style = InputStyle("keyboard", 0.0)
    published = []
    s.publish = published.append
    return s, st, run, drv, published


def test_session_walks_to_the_button_and_presses_enter():
    from yomawari.fuzz.keyboard import NAV

    s, st, run, drv, published = _menu_session()
    op = asyncio.run(s._navigate(Op("click", st.find("UI/C"), via_key=NAV)))
    assert drv.keys == ["down", "down"] and op.via_key == "enter"
    s._input(op)
    assert drv.keys[-1] == "enter" and drv.clicks == []
    nav = [e.data for e in published if e.kind == "key_nav"]
    assert nav == [{"target": "UI/C", "path": ["down", "down"], "reached": True}]
    assert run.input_style.reached == {"UI/C": 1}


def test_session_falls_back_to_mouse_when_focus_does_not_move():
    from yomawari.fuzz.keyboard import NAV

    s, st, run, drv, published = _menu_session(moves=False)
    op = asyncio.run(s._navigate(Op("click", st.find("UI/C"), via_key=NAV)))
    assert drv.keys == ["down"] and op.via_key is None  # 下が効かず、ほかに目的へ向かう向きがない
    s._input(op)
    assert len(drv.clicks) == 1
    assert run.input_style.unreachable == {"UI/C": 1}
    assert any("`UI/C` に方向キーでたどり着けなかった（down" in n for n in run.notes)


def test_modal_opened_by_a_keyboard_advance_is_closed_with_the_same_key():
    # 本文送りを Space で押したら UI を隠すパネルが出た。Space を覚えていなかったので、Esc・右クリック・本文送りしか試さず、
    # 15 分抜けられなかった
    from yomawari.fuzz.loop import FuzzSession

    s, st, run, _ = _session()
    s.pointer.driver = KeyDriver()
    s.pointer.platform.info = WindowInfo(1, ScreenRect(100, 50, 1280, 748), True)
    s.publish = lambda e: None
    assert asyncio.run(FuzzSession._do(s, Op("advance", via_key="space"))) is True
    _game(s, st, "menu", name="HidePanel", modal=True, buttons=[])
    assert s._toggle_keys == {"HidePanel": "space"}
    toggle = s._toggle_op("escape")
    assert toggle is not None and toggle.label == "key space"


def test_arrow_keys_on_the_way_to_a_button_are_remembered():
    from yomawari.fuzz.keyboard import NAV

    s, st, run, drv, published = _menu_session()
    asyncio.run(s._navigate(Op("click", st.find("UI/C"), via_key=NAV)))
    assert [k for _, k in s._key_ops] == ["down", "down"]
