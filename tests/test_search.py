"""画面の外を探す（fuzz/search.py）。"""

from yomawari.events import Event
from yomawari.fuzz.search import blind_candidates, leads, search_candidates, stuck_rows
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
    assert found == {("down", 8), ("right", 8)}
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


def test_nothing_to_search_when_all_seen():
    st = _grid()
    seen = {"ListUI/Slot"} | {f"ListUI/Slot#{i}" for i in range(1, 16)}
    assert leads(st, seen) == []


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
