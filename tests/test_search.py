"""画面の外を探す（fuzz/search.py）。"""

from yomawari.events import Event
from yomawari.fuzz.search import blind_candidates, known_leads, leads, search_candidates, stuck_rows
from yomawari.state import GameState


def _grid(dx=0.0, dy=0.0, modal=False, extra=None):
    """縦横にスクロールする 4x4 の一覧。見えているのは左上の 2x2 だけ。"""
    buttons = [
        {"name": "Slot", "rect": [0.1 + 0.4 * c - dx, 0.1 + 0.4 * r - dy, 0.4 + 0.4 * c - dx, 0.4 + 0.4 * r - dy]}
        for r in range(4) for c in range(4)
    ]
    buttons += extra or []
    st = GameState()
    st.feed(Event("game", "menu", {"name": "ListUI", "modal": modal, "buttons": buttons}, 0))
    return st


def test_leads_point_to_unseen_buttons_in_both_axes():
    st = _grid()
    found = {(lead.direction, lead.unseen) for lead in leads(st, set())}
    # 斜め（右下）の 4 つは、縦に動かしても見えてこないので右にだけ数える
    assert found == {("down", 4), ("right", 8)}
    lead = leads(st, set())[0]
    assert lead.menu == "ListUI" and lead.name == "Slot"
    assert lead.point == (0.45, 0.45)  # 見えているボタンの範囲の中心


def test_leads_skip_seen_and_covered_and_hidden_flag_works():
    st = _grid()
    seen = {f"ListUI/Slot#{i}" for i in (2, 3, 6, 7, 10, 11, 14, 15)}  # 右側の2列は見たことがある
    assert {lead.direction for lead in leads(st, seen)} == {"down"}
    # 右へスクロールしたあとは、左に見ていないものが残る
    st = _grid(dx=0.8)
    assert {lead.direction for lead in leads(st, set())} == {"left", "down"}
    # ほかの UI に覆われているボタンは、スクロールでは出てこないので向きにしない
    st = GameState()
    st.feed(Event("game", "menu", {"name": "ListUI", "modal": False, "buttons": [
        {"name": "Slot", "rect": [0.1, 0.1, 0.3, 0.3]},
        {"name": "Slot", "rect": [0.1, 1.2, 0.3, 1.4], "covered": True},
        {"name": "Slot", "rect": [0.1, 0.5, 0.3, 0.7], "hidden": True},  # マスクで隠れている（中心は描画領域の中）
    ]}, 0))
    assert [(lead.direction, lead.unseen) for lead in leads(st, set())] == [("down", 1)]


def test_leads_do_not_go_up_and_down_for_columns_outside_the_view():
    # run 2026-11-06_1846: 横に7列並ぶうち左の4列しか見えない回想の一覧。上の行にも下の行にも右の3列があり、
    # それを「上にも下にもある」と数えて、上下に動かし続けた。見えている列の外は、横の向きにだけ数える
    buttons = [{"name": "Slot", "rect": [0.08 + 0.25 * c, 0.1 + 0.3 * r, 0.3 + 0.25 * c, 0.35 + 0.3 * r]}
               for r in range(-3, 6) for c in range(7)]
    st = GameState()
    st.feed(Event("game", "menu", {"name": "ListUI", "modal": True, "buttons": buttons}, 0))
    vis = {b.label for b in st.menus["ListUI"].visible_buttons}
    hidden_left_columns = {b.label for b in st.menus["ListUI"].buttons if b.rect[0] < 0.9} - vis
    # 左の4列はすべて見たことがある。残りは右の3列だけ
    found = leads(st, vis | hidden_left_columns)
    assert [lead.direction for lead in found] == ["right"]


def test_nothing_to_search_when_all_seen():
    st = _grid()
    seen = {"ListUI/Slot"} | {f"ListUI/Slot#{i}" for i in range(1, 16)}
    assert leads(st, seen) == []


def test_known_leads_go_back_to_bright_button():
    # 明るかった（開示済みと分かっている）項目が画面の外にあれば、そちらへ戻る。見ていない項目を探すのとは別の向き
    st = _grid()
    seen = {"ListUI/Slot"} | {f"ListUI/Slot#{i}" for i in range(1, 16)}
    assert leads(st, seen) == []
    found = known_leads(st, {"ListUI/Slot#15"})
    assert [(lead.direction, lead.unseen, lead.kind) for lead in found] == [("right", 1, "known")]  # 右下は右へ
    assert {lead.kind for lead in leads(st, set())} == {"unseen"}
    # 見えている項目に明るかったものがあれば、それを押せばよいので戻らない
    assert known_leads(st, {"ListUI/Slot#15", "ListUI/Slot#1"}) == []


def test_search_candidates_are_not_novelty_and_dedupe():
    st = _grid()
    found = leads(st, set())
    ops = search_candidates(found, {"ListUI/Slot": 7.5})
    labels = sorted(op.label for op in ops)
    assert labels == ["drag left", "drag up", "key down", "key right", "wheel_down 3"]
    assert all(op.group == "search" and not op.novelty and op.weight == 7.5 for op in ops)
    assert all(op.point == (0.45, 0.45) for op in ops if op.kind != "key")
    assert search_candidates(found, {}) == []  # 重みがなければ出さない


def test_blind_search_only_when_all_visible_are_stuck():
    # 画面の外の矩形が来ない（向きが分からない）とき。見えている並びがどれも押せそうにないときだけ探す
    st = GameState()
    st.feed(Event("game", "menu", {"name": "Gallery", "modal": False, "buttons": [
        {"name": "Thumb", "rect": [0.1 + 0.25 * i, 0.3, 0.3 + 0.25 * i, 0.5]} for i in range(3)
    ] + [{"name": "Back", "rect": [0.8, 0.9, 0.95, 0.95]}]}, 0))
    labels = {"Gallery/Thumb", "Gallery/Thumb#1", "Gallery/Thumb#2"}
    assert stuck_rows(st, labels - {"Gallery/Thumb#2"}) == []
    rows = stuck_rows(st, labels)
    assert [(m, n) for m, n, _ in rows] == [("Gallery", "Thumb")]
    ops = blind_candidates(rows[0][2], 1.0)
    assert {op.label for op in ops} >= {"wheel_down 3", "wheel_up 3", "drag up", "drag left", "key right"}
    assert all(op.novelty and op.group == "search" for op in ops)


def test_session_prefers_survey_and_skips_dead_means():
    import random

    import pytest

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun, FuzzSession
    from yomawari.input.pointer import Pointer
    from yomawari.sensors.window import ScreenRect, WindowInfo

    from .test_window import FakeWindows

    st = _grid(modal=True)
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    base = cfg["weights"]["click"] * cfg["search_factor"]
    # 見た目に差がない（明るさを比べられていない）並びは、押して確かめるより先に見て回る
    ops = {op.label: op.weight for op in s._search_candidates()}
    assert ops["wheel_down 3"] == pytest.approx(base * cfg["explore_factor"])
    # この画面で動かしても反応しなかった手段（方向キー）は後回しにする
    run.search_dead[("ListUI", "key down")] = 2
    ops = {op.label: op.weight for op in s._search_candidates()}
    assert ops["key down"] == pytest.approx(base * cfg["explore_factor"] * cfg["no_response_factor"] ** 2)
    # 明るさに差のある並びでは、明るいものを押せばよいので、見て回るほうを強めない
    s._varied = {"ListUI/Slot"}
    ops = {op.label: op.weight for op in s._search_candidates()}
    assert ops["wheel_down 3"] == pytest.approx(base)


def test_session_goes_straight_to_known_and_searches_otherwise():
    import random

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun, FuzzSession
    from yomawari.input.pointer import Pointer
    from yomawari.sensors.window import ScreenRect, WindowInfo

    from .test_window import FakeWindows

    # 下の右端に、前に見て明るかった項目がある。まだ見ていない項目も下と右にある
    st = _grid(modal=True)
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    run.dim_history = {"ListUI/Slot#15": False, "ListUI/Slot": True}
    # 見当をつけている（前に見て明るかった）項目が画面の外にあれば、目的によらず、まっすぐそちらへ向かう（探す操作は混ぜない）
    for intent in ("progress", "explore"):
        run.intent = intent
        assert {op.group for op in s._search_candidates()} == {"return"}
    assert any("前に見て明るかった項目" in n for n in run.notes)
    # 見当がなければ、まだ見ていない項目を探す
    run.dim_history = {}
    assert {op.group for op in s._search_candidates()} == {"search"}
    # 前のランで押して先に進んだ項目も、見当にする
    run.experience.ops["modal:ListUI"] = {"click ListUI/Slot#15": {"tries": 1, "progress": 1, "rewind": 0}}
    assert {op.group for op in s._search_candidates()} == {"return"}


def test_buttons_that_look_like_a_locked_one_are_skipped():
    import random

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun, FuzzSession
    from yomawari.input.pointer import Pointer
    from yomawari.sensors.window import ScreenRect, WindowInfo

    from .test_window import FakeWindows

    st = _grid(modal=True)
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    # 見えている 4 個はどれも同じような暗さ（ロック中）。まだどれも押していないので、見た目では決められない
    s._values = {"ListUI/Slot": {"ListUI/Slot": (0.2, 0.3), "ListUI/Slot#1": (0.21, 0.31),
                                 "ListUI/Slot#4": (0.19, 0.3), "ListUI/Slot#5": (0.2, 0.29)}}
    assert s._locked_alike() == set()
    # 1つ押して反応がなかった → 同じ見た目のものも押せないとみなす。見えているものがどれも押せないので、探すほうを強める
    run.silent.add("ListUI/Slot")
    assert s._locked_alike() == {"ListUI/Slot#1", "ListUI/Slot#4", "ListUI/Slot#5"}
    assert any("同じ見た目のもの（3 個）も押せない" in n for n in run.notes)
    ops = {op.label: op.weight for op in s._search_candidates()}
    assert ops["wheel_down 3"] == cfg["weights"]["click"] * cfg["search_factor"] * cfg["explore_factor"]
    # スクロールして、反応しなかったボタンが画面の外に出ても、覚えた明るさと比べる。明るいもの（開示済み）は除く
    run.locked_look["ListUI/Slot"] = (0.2, 0.3)
    s._values = {"ListUI/Slot": {"ListUI/Slot#8": (0.2, 0.3), "ListUI/Slot#9": (0.7, 0.9), "ListUI/Slot#12": (0.21, 0.3)}}
    assert s._locked_alike() == {"ListUI/Slot#8", "ListUI/Slot#12"}
