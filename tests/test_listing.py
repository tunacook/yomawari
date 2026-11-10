"""一覧の台帳と目標（fuzz/listing.py、DESIGN.md §9「一覧の台帳と目標」）。"""

import asyncio
import random

import pytest

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.listing import Ledger, blind_candidates, direction, distance, stuck_rows, view_of
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import normal_candidates
from yomawari.input.pointer import Pointer
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState

from .test_window import FakeWindows


def _grid(dx=0.0, dy=0.0, modal=False, extra=None, scroll=None, st=None):
    """縦横にスクロールする 4x4 の一覧。見えているのは左上の 2x2 だけ。"""
    buttons = [
        {"name": "Slot", "rect": [0.1 + 0.4 * c - dx, 0.1 + 0.4 * r - dy, 0.4 + 0.4 * c - dx, 0.4 + 0.4 * r - dy]}
        for r in range(4) for c in range(4)
    ]
    buttons += extra or []
    st = st or GameState()
    d = {"name": "ListUI", "modal": modal, "buttons": buttons}
    if scroll is not None:
        d["scroll"] = scroll
    st.feed(Event("game", "menu", d, 0))
    return st


def _session(st, cfg=None):
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0, **(cfg or {})}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    events = []
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), events.append, 0, lambda _: None)
    return s, run, events


def test_geometry_direction_and_distance():
    view = (0.0, 0.0, 1.0, 1.0)
    assert distance((0.1, 0.1, 0.3, 0.3), view) == 0
    assert direction((0.1, 1.2, 0.3, 1.4), view) == "down" and distance((0.1, 1.2, 0.3, 1.4), view) == pytest.approx(0.3)
    assert direction((0.1, -0.4, 0.3, -0.2), view) == "up"
    assert direction((1.2, 0.1, 1.4, 0.3), view) == "right"
    # 縦と横の両方にはみ出していれば、縦を先に合わせる
    assert direction((1.2, 1.2, 1.4, 1.4), view) == "down"


def test_scroll_rect_is_the_view():
    # パッケージの scroll の rect（表示される範囲）で、目標が見えるか・どちらへ動かせばよいかを決める
    st = _grid(scroll=[{"rect": [0.05, 0.05, 0.85, 0.85], "up": False, "down": True, "left": False, "right": True}])
    menu = st.menus["ListUI"]
    assert menu.scroll == ((0.05, 0.05, 0.85, 0.85),)
    assert view_of(menu, menu.buttons) == (0.05, 0.05, 0.85, 0.85)
    st = _grid()
    assert view_of(st.menus["ListUI"], st.menus["ListUI"].buttons) == (0.0, 0.0, 1.0, 1.0)


def test_ledger_remembers_text_and_status():
    st = _grid()
    ledger = Ledger()
    rows = ledger.sync(st, "?？")
    assert [r.key for r in rows] == ["ListUI/Slot"] and len(rows[0].labels) == 16
    assert {lb for lb in rows[0].labels if ledger.items[lb].shown} == {"ListUI/Slot", "ListUI/Slot#1", "ListUI/Slot#4",
                                                                       "ListUI/Slot#5"}
    # 伏せ字は押さずに押せないとみなす。押した結果で押せた・押せないを決め、見た目では変えない
    st.set_read_text({"ListUI/Slot#1": "？？？", "ListUI/Slot": "屋上"})
    ledger.sync(st, "?？")
    assert ledger.items["ListUI/Slot#1"].status == "masked"
    ledger.pressed("ListUI/Slot", True, 0, 3)
    ledger.pressed("ListUI/Slot#4", False, 1, 3)
    assert ledger.items["ListUI/Slot"].status == "ok" and ledger.items["ListUI/Slot#4"].status == "unknown"
    ledger.pressed("ListUI/Slot#4", False, 3, 3)
    assert ledger.items["ListUI/Slot#4"].status == "no"
    # 中身が入れ替わった（同じ番号に別の文字）ら未確認に戻す
    st.set_read_text({"ListUI/Slot": "玄関"})
    ledger.sync(st, "?？")
    assert ledger.items["ListUI/Slot"].status == "unknown"
    # 閉じたら、見えたかは忘れる（台帳は覚えておく）。進み具合が入れ替わったら、押せた・押せないを未確認に戻す
    st.feed(Event("game", "menu_close", {"name": "ListUI"}, 0))
    assert ledger.closed(set(st.menus)) == ["ListUI"]
    assert not any(item.shown for item in ledger.items.values()) and ledger.items["ListUI/Slot#4"].status == "no"
    assert ledger.forget_progress() == 1 and ledger.items["ListUI/Slot#4"].status == "unknown"
    assert ledger.items["ListUI/Slot#1"].status == "masked"


def test_readings_are_kept_only_while_the_list_is_open():
    # 読んだ文字の書き方の違いは、開いているあいだだけ覚える。閉じて開き直すと、同じボタンに別の項目が入っていることがある
    # （run 2026-11-10_2135: ほかの思い出の言葉の一覧で読んだ文字が残り、別の言葉を正解とみなした）
    st = _grid()
    ledger = Ledger()
    st.set_read_text({"ListUI/Slot": "No.002"})
    ledger.sync(st, "?？")
    st.set_read_text({"ListUI/Slot": "屋上の昼"})
    ledger.sync(st, "?？")
    assert ledger.items["ListUI/Slot"].readings == ["No.002", "屋上の昼"]
    st.feed(Event("game", "menu_close", {"name": "ListUI"}, 0))
    ledger.closed(set(st.menus))
    # 最後に読んだ文字は残す（前の手順で選んだものを照らすため）
    assert ledger.items["ListUI/Slot"].readings == [] and ledger.items["ListUI/Slot"].text == "屋上の昼"
    _grid(st=st)
    st.set_read_text({"ListUI/Slot": "公園"})
    ledger.sync(st, "?？")
    assert ledger.items["ListUI/Slot"].readings == ["公園"]


def test_fill_goes_to_the_nearest_unshown_and_keeps_it():
    # 台帳を埋める: まだ見えていない項目のうち、いちばん近いものを目標にして見えるところまで動かす（押さない）。
    # 動かすたびに近さが変わっても、見にいっている途中の項目を続ける（逆の向きへ動かして行き来しないように）
    st = _grid(modal=True)
    s, run, _ = _session(st)
    ops = s._list_ops([])
    goal = run.ledger.goals["ListUI/Slot"]
    assert goal.purpose == "fill" and goal.label in ("ListUI/Slot#2", "ListUI/Slot#8")
    d = "right" if goal.label == "ListUI/Slot#2" else "down"
    want = {"right": {"drag left", "key right"}, "down": {"wheel_down 3", "drag up", "key down"}}[d]
    assert {op.label for op in ops} == want and all(op.by == "list" and op.goal == goal.label for op in ops)
    assert all(not op.novelty and op.weight == FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["list_factor"]
               for op in ops)
    assert any("台帳を埋め始めた" in n for n in run.notes)
    # 目標を持っている間は、モーダルでいつも試す動かす操作を候補に入れない（Enter・抜ける操作は残す）
    cands = s._one_mover(normal_candidates(st, Guard({}), s.cfg["weights"], False) + ops)
    assert {op.label for op in cands if op.group == "move"} == {"key enter"}
    assert {op.label for op in cands if op.group == "leave"} == {"key esc", "right_click"}
    # 少しだけ動いた（ほかの見えていない項目のほうが近くなった）: 目標は変えない
    first = goal.label
    _grid(dx=0.04 if first.endswith("#8") else 0.0, dy=0.04 if first.endswith("#2") else 0.0, modal=True, st=st)
    s._list_ops([])
    assert run.ledger.goals["ListUI/Slot"].label == first


def test_goal_that_does_not_come_closer_is_given_up():
    st = _grid(modal=True)
    s, run, _ = _session(st)
    give_up = FUZZ_DEFAULTS["list_move_give_up"]
    low = FUZZ_DEFAULTS["no_response_factor"]
    s._list_ops([])
    goal = run.ledger.goals["ListUI/Slot"]
    for _ in range(give_up):
        op = next(o for o in s._list_ops([]) if o.kind != "key")
        s._moving = ("ListUI/Slot", goal.label, op.label, goal.dist)
    s._list_ops([])
    assert goal.label in run.ledger.unreachable
    assert run.ledger.goals["ListUI/Slot"].label != goal.label
    assert any("近づかなかった" in n for n in run.notes)
    # 近づかなかった手段は、その画面では後回しにする
    assert any(n > 0 for n in run.ledger.dead.values())
    ops = s._list_ops([])
    full = FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["list_factor"]
    for o in ops:
        assert o.weight == pytest.approx(full * low ** run.ledger.dead[("ListUI", o.label)])
    # 近づいたら、数え直す
    goal = run.ledger.goals["ListUI/Slot"]
    goal.misses = 2
    s._moving = ("ListUI/Slot", goal.label, "drag up", goal.dist + 1.0)
    s._list_ops([])
    assert goal.misses == 0


def test_detour_list_has_no_goal_after_filling():
    # ハーネスがボタンで開いた寄り道の画面は、台帳を埋めたら目標を持たない（押せるものがないのが正常なこともある）
    st = _grid(modal=True)
    s, run, _ = _session(st)
    run.opened_by["ListUI"] = "TitleUI/ListButton"
    for item in run.ledger.items.values():
        item.shown = True
    s._list_ops([])
    for item in run.ledger.items.values():
        item.shown = True
    assert s._list_ops([]) == []
    assert any("寄り道の画面" in n for n in run.notes)


def test_game_list_probes_off_screen_items_when_nothing_visible_looks_pressable():
    # ゲームが開いた画面で、見えている項目に押せそうなものがなければ、画面の外の押せた項目 → 見た目で押せそうな未確認 →
    # 未確認、の順にいちばん近いものを目標にして押しにいく
    st = _grid(modal=True)
    s, run, _ = _session(st)
    s._list_ops([])
    for item in run.ledger.items.values():
        item.shown = True
    visible = ["ListUI/Slot", "ListUI/Slot#1", "ListUI/Slot#4", "ListUI/Slot#5"]
    # 見えているものに押せそうなもの（見た目で押せそうな未確認）があれば、目標を持たない（ふだんどおり押す）
    assert s._list_ops([]) == []
    s._dim = set(visible)
    run.ledger.items["ListUI/Slot#15"].status = "ok"  # 前に押せた（右下の端）
    ops = s._list_ops([])
    assert {op.goal for op in ops} == {"ListUI/Slot#15"}
    # 目標が見えたら、押す（list_factor を掛ける）
    _grid(dx=0.8, dy=0.8, modal=True, st=st)
    ops = s._list_ops([])
    assert [op.label for op in ops] == ["click ListUI/Slot#15"]
    assert ops[0].weight == FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["list_factor"]
    # 押して反応がなかった: 台帳に書き、次の目標を決め直す
    run.miss_streak["ListUI/Slot#15"] = 3
    run.answered.add("ListUI/Slot#15")
    s._list_pressed("ListUI/Slot#15", False)
    assert run.ledger.items["ListUI/Slot#15"].status == "no" and "ListUI/Slot" not in run.ledger.goals


def test_list_left_behind_is_not_a_detour_when_shown_again():
    # run 2026-11-07_0849: 思い出を読むあいだ、ゲームが開いた思い出の一覧がボタンなし・モーダルでない状態で残り、
    # 要約を閉じたらボタン付きで出し直された。押す前から開いていた画面は、押したボタンで開いた寄り道としない
    st = GameState()
    st.feed(Event("game", "boot", {"version": "x"}, 0))
    st.feed(Event("game", "resolution", {"w": 1000, "h": 500, "mode": "Windowed"}, 0))
    st.feed(Event("game", "menu", {"name": "ListUI", "modal": False, "buttons": []}, 0))
    st.feed(Event("game", "menu", {"name": "SummaryUI", "modal": True,
                                   "buttons": [{"name": "Close", "rect": [0.1, 0.1, 0.2, 0.2]}]}, 0))
    s, run, _ = _session(st)
    async def press():
        op = normal_candidates(st, Guard({}), s.cfg["weights"], False)[0]
        assert op.label == "click SummaryUI/Close"

        async def close():
            await asyncio.sleep(0.02)
            st.feed(Event("game", "menu_close", {"name": "SummaryUI"}, 0))
            _grid(st=st)
        task = asyncio.create_task(close())
        await s._normal(op, "k", False)
        await task
    asyncio.run(press())
    assert "ListUI" in st.menus and st.menus["ListUI"].buttons
    assert "ListUI" not in run.opened_by


def test_blind_search_only_for_lists_without_off_screen_items():
    # 画面の外の項目が分からない一覧（矩形が来ない・ページ送り）で、ゲームが開いた画面の見えている項目がどれも押せない
    # ときだけ、向きを決めずにすべての向きへ動かして探す
    st = GameState()
    st.feed(Event("game", "menu", {"name": "PageUI", "modal": True, "buttons": [
        {"name": "Slot", "rect": [0.1 + 0.25 * i, 0.3, 0.3 + 0.25 * i, 0.5]} for i in range(3)]}, 0))
    rows = stuck_rows(st, {"PageUI/Slot", "PageUI/Slot#1", "PageUI/Slot#2"})
    assert [(m, n) for m, n, _p in rows] == [("PageUI", "Slot")]
    ops = blind_candidates(rows[0][2], 1.0)
    assert {op.label for op in ops} >= {"wheel_down 3", "wheel_up 3", "drag up", "drag left", "key right"}
    assert all(op.novelty and op.group == "search" and op.by == "blind" for op in ops)
    s, run, _ = _session(st)
    s._list_ops([])
    for item in run.ledger.items.values():
        item.shown = True
    run.silent |= {"PageUI/Slot", "PageUI/Slot#1", "PageUI/Slot#2"}
    for lb in run.silent:
        run.ledger.items[lb].status = "no"
    ops = s._list_ops([])
    assert ops and all(op.by == "blind" for op in ops)
    # 画面の外の項目が分かる一覧では、向きを決めずに探さない
    st2 = _grid(modal=True)
    s2, run2, _ = _session(st2)
    run2.silent |= {"ListUI/Slot", "ListUI/Slot#1", "ListUI/Slot#4", "ListUI/Slot#5"}
    assert not any(op.by == "blind" for op in s2._list_ops([]))
