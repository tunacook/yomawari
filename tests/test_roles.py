"""ボタンの役割の推定と確定（fuzz/roles.py、DESIGN.md §9「役割の推定と判定」）。"""

import asyncio
import json
import random
import time
from pathlib import Path

import pytest

from yomawari.config import FUZZ_DEFAULTS, RUN_DEFAULTS, SENSOR_DEFAULTS, BuildConfig
from yomawari.detect.engine import Engine
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import Op, normal_candidates
from yomawari.fuzz.roles import RoleBook, RoleHintError, RoleHints, load_role_hints
from yomawari.input.pointer import Pointer
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState, Target
from yomawari.watchdog import Harness

from .conftest import RULES_PATH, scaled_rules
from .test_fuzz import CFG, TITLE, run_sim
from .test_window import FakeWindows, RecordingDriver

HINTS = load_role_hints(RULES_PATH)
WEIGHTS = FUZZ_DEFAULTS["weights"]


def _t(name, text=None, menu="TitleUI", role=None):
    return Target("button", menu, name, (0.1, 0.1, 0.2, 0.2), text, role=role)


# --- 推定 ---


@pytest.mark.parametrize("name,text,role", [
    ("ToExit", None, "exit"),                      # タイトル画面の自作ボタン
    ("ToExitGame", None, "exit"),                  # 右クリックメニューの自作ボタン
    ("TitleWebSiteButton", None, "external"),      # "title" だけでは to_title にしない
    ("ToTitleMenu", None, "to_title"),
    ("ResolutionDropdown", None, "system_setting"),
    ("Button (3)", "はじめから", "new_game"),        # 名前で分からなくても文字で
    ("QuitButton", "タイトルへ戻る", "to_title"),     # 文字を名前より先に見る
    ("SubtitleToggle", "Subtitle", None),          # 英語は単語として照らし合わせる
    ("Btn", "Steam Share", None),                  # 複数の役割に当たるものは推定しない
    ("Slot", None, None),
])
def test_estimate_from_text_and_name(name, text, role):
    guess = HINTS.estimate(_t(name, text))
    assert (guess[0] if guess else None) == role


def test_bad_role_hints_are_rejected(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("role_hints:\n  quit:\n    text: ['終了']\n")
    with pytest.raises(RoleHintError):
        load_role_hints(p)
    p.write_text("role_hints:\n  exit:\n    text: '終了'\n")
    with pytest.raises(RoleHintError):
        load_role_hints(p)


# --- RoleBook ---


def test_book_order_and_cancel():
    book = RoleBook(HINTS)
    quit_ = _t("QuitButton")
    assert book.role_of(quit_) == "exit" and book.is_estimate(quit_)
    assert book.role_of(_t("QuitButton", role="new_game")) == "new_game"  # エンジンの標準部品は推定より先
    book.cancel(quit_.key, "終わらなかった")
    assert book.role_of(quit_) is None and not book.is_estimate(quit_)  # 取り消した推定は使わず、推定し直さない
    assert book.confirm(quit_.key, "to_title", "タイトル画面が再び出た")
    assert book.role_of(quit_) == "to_title" and not book.is_estimate(quit_)
    assert not book.confirm(quit_.key, "to_title", "タイトル画面が再び出た")  # 同じ確定は2回書かない
    assert len(book.new) == 3 and "推定した" in book.new[0] and "取り消した" in book.new[1]


def test_book_persists_and_keeps_other_keys(tmp_path):
    path = tmp_path / "_experience" / "Rachel.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"screens": {"x": 1}}))
    book = RoleBook(HINTS, path, build="1.0/abc")
    book.role_of(_t("ToExit"))
    book.confirm("TitleUI/ToExit", "exit", "終了コード 0 で終わった")
    book.save()
    doc = json.loads(path.read_text())
    assert doc["screens"] == {"x": 1}
    assert doc["roles"]["TitleUI/ToExit"] == {
        "role": "exit", "status": "confirmed", "evidence": "終了コード 0 で終わった", "build": "1.0/abc"}
    again = RoleBook(RoleHints(), path)  # キーワードがなくても、覚えた役割を使う
    assert again.role_of(_t("ToExit")) == "exit" and again.new == []


def test_estimated_roles_decide_guard_kind():
    st = GameState()
    st.feed(Event("game", "menu", {"name": "TitleUI", "buttons": [
        {"name": "ToExit", "rect": [0.1, 0.1, 0.2, 0.2]},
        {"name": "TitleWebSiteButton", "rect": [0.1, 0.3, 0.2, 0.4]},
        {"name": "Shop", "rect": [0.1, 0.5, 0.2, 0.6]},
        {"name": "Start", "rect": [0.1, 0.7, 0.2, 0.8]},
    ]}, 0))
    st.set_read_text({"TitleUI/Shop": "ショップ", "TitleUI/Start": "はじめから"})  # 画面から読んだ文字
    ops = normal_candidates(st, Guard({}, book=RoleBook(HINTS)), WEIGHTS, allow_exit=True)
    kinds = {op.target.name: op.kind for op in ops if op.target}
    assert kinds == {"ToExit": "expect_exit", "TitleWebSiteButton": "external", "Start": "click"}  # deny は押さない


# --- 観測（状態） ---


def test_state_counts_title_returns_and_resolution_changes():
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("resolution", w=1920, h=1080, mode="Windowed")
    # パッケージの text は使わず、画面から読んだ文字を付ける。出し直された menu にも引き継ぐ
    feed("menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2], "text": "パッケージの文字"}])
    assert st.menus["TitleUI"].buttons[0].text is None
    st.set_read_text({"TitleUI/Start": "はじめから"})
    feed("menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert st.title_menu == "TitleUI" and st.menus["TitleUI"].buttons[0].text == "はじめから"
    feed("menu_close", name="TitleUI")
    feed("menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert st.title_returns == 0  # 本編を挟まずに戻った（設定から戻ったなど）のは数えない
    feed("menu_close", name="TitleUI")
    feed("line", line_id="s1:1")
    feed("menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert st.title_returns == 1
    feed("resolution", w=1920, h=1080, mode="Windowed")
    assert st.resolution_changes == 0
    feed("resolution", w=1280, h=720, mode="Windowed")
    assert st.resolution_changes == 1


def _session(front=1):
    st = GameState()
    book = RoleBook(HINTS)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.3), guard=Guard({}, book=book),
                  rng=random.Random(1), cfg=CFG, response_timeout=0.25)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=front)
    events = []
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), events.append, 0, lambda _: None)
    return s, st, book, windows, events


def _feed(s, st, kind, **d):
    ev = Event("game", kind, d, 0)
    st.feed(ev)
    s.on_event(ev)


def test_title_return_and_resolution_change_confirm_last_press():
    s, st, book, _, _ = _session()
    _feed(s, st, "resolution", w=1920, h=1080, mode="Windowed")
    _feed(s, st, "menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]}])
    _feed(s, st, "menu_close", name="TitleUI")
    _feed(s, st, "line", line_id="s1:1")
    s.run.last_press = ("QuickMenuUI/Back", time.monotonic())
    _feed(s, st, "menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert book.entries["QuickMenuUI/Back"].role == "to_title"
    s.run.last_press = ("SettingsUI/Item 1", time.monotonic() - 60)  # role_window_sec より前
    _feed(s, st, "resolution", w=1280, h=720, mode="Windowed")
    assert "SettingsUI/Item 1" not in book.entries
    s.run.last_press = ("SettingsUI/Item 2", time.monotonic())
    _feed(s, st, "resolution", w=1920, h=1080, mode="Windowed")
    assert book.entries["SettingsUI/Item 2"].role == "system_setting"


def test_front_change_after_silent_click_confirms_external():
    s, _, book, windows, events = _session(front=2)  # ブラウザが前面に出ている
    asyncio.run(s._check_front("TitleUI/Banner"))
    assert book.entries["TitleUI/Banner"].role == "external"
    assert windows.activated == [1] and not s.run.guard.external_available("TitleUI/Banner")
    assert [e.kind for e in events if e.kind != "intent"] == ["external"]


def test_front_change_does_not_make_known_button_external():
    # run 2026-11-06_1830: 前に開いた Web サイトのブラウザがあとから前面に出ていたときに、ロード画面の「戻る」を押して
    # external と確定し、1回のランで1回しか押さなくなった。前にゲームの中で反応したボタンは external にしない
    s, _, book, windows, events = _session(front=2)
    s.run.answered.add("SaveLoadUI/ReturnButton")
    asyncio.run(s._check_front("SaveLoadUI/ReturnButton"))
    assert "SaveLoadUI/ReturnButton" not in book.entries
    assert windows.activated == [1] and s.run.guard.external_available("SaveLoadUI/ReturnButton")
    assert any("ほかのアプリが前面に出ていた" in n for n in s.run.notes)


def test_wrong_external_is_revoked_when_front_does_not_change():
    # 前のランで誤って external と確定したボタンは、押しても前面のアプリが変わらなければ、ふつうのボタンに戻す
    s, st, book, windows, events = _session(front=1)
    s.run.cfg = {**s.run.cfg, "external_wait_sec": 0}
    s.pointer.driver = RecordingDriver()
    book.confirm("SaveLoadUI/ReturnButton", "external", "前面のアプリがゲーム以外に切り替わった")
    _feed(s, st, "resolution", w=1000, h=500, mode="Windowed")
    _feed(s, st, "menu", name="SaveLoadUI", modal=True, buttons=[{"name": "ReturnButton", "rect": [0.45, 0.85, 0.55, 0.9]}])
    op = next(o for o in normal_candidates(st, s.run.guard, s.cfg["weights"], False) if o.kind == "external")
    asyncio.run(s._external(op))
    assert book.entries["SaveLoadUI/ReturnButton"].status == "cancelled"
    assert book.role_of(st.find("SaveLoadUI/ReturnButton")) is None
    assert any("取り消した" in n for n in book.new)


# --- ループ: 推定だけの exit ---


def test_estimated_exit_is_cancelled_when_game_keeps_running():
    cfg = {**CFG, "exit_min_interval_sec": 0, "role_window_sec": 0.1, "save_before_exit_sec": 0}
    book = RoleBook(HINTS)
    game, _, issues, events, exits = run_sim("random", 0.6, title={"QuitButton": TITLE["QuitButton"]},
                                               cfg=cfg, book=book)
    assert game.clicked[0] == "QuitButton"
    assert exits[:2] == [True, False]  # 押すあいだだけ終了を期待する
    assert not [e for e in events if e.kind == "exit_expected"]  # C-04 の対象にしない
    assert not [i for i in issues if i.rule_id == "C-04"]
    assert book.entries["TitleUI/QuitButton"].status == "cancelled"
    assert any("取り消した" in n for n in book.new)


# --- C-01 とウォッチドッグ ---


def test_c01_skips_clean_exit_right_after_press():
    engine = Engine(scaled_rules(1.0))
    ok = {"code": 0, "by_harness": False, "after_op": "TitleUI/ToExit"}
    assert engine.feed(Event("proc", "proc_exit", ok, 0)) == []
    assert [i.rule_id for i in engine.feed(Event("proc", "proc_exit", {**ok, "code": 1}, 0))] == ["C-01"]
    assert [i.rule_id for i in engine.feed(Event("proc", "proc_exit", {**ok, "after_op": None}, 0))] == ["C-01"]


def _harness(tmp_path, book):
    h = Harness(BuildConfig("x", Path("/x")), Path("/x"), None, scaled_rules(1.0), RULES_PATH, tmp_path,
                RUN_DEFAULTS, SENSOR_DEFAULTS, roles=book)
    h._restart_req = asyncio.Event()
    h._bus = type("Bus", (), {"publish": lambda self, ev: None})()
    h._on_event(Event("game", "boot", {"version": "1.0", "build_guid": "abc"}, 0))
    return h


def test_harness_confirms_exit_it_expected(tmp_path):
    # 終わると見込んで押した（推定を確かめた）ボタンで終わったら確定する
    book = RoleBook(HINTS)
    h = _harness(tmp_path, book)
    h._on_event(Event("proc", "proc_exit", {"code": 0, "by_harness": True, "after_op": "QuickMenuUI/ToExit"}, 0))
    assert book.entries["QuickMenuUI/ToExit"].status == "confirmed"
    assert book.entries["QuickMenuUI/ToExit"].build == "1.0/abc"
    assert h.issues == []


def test_unexpected_exit_after_press_is_only_estimated_and_relaunches(tmp_path):
    # 見込んでいない終了は、人がゲームを閉じたのかもしれない（run 1346 で、閉じる直前に押した
    # 「この思い出を読む」が exit と確定され、以後のランで一度も押されなくなった）
    book = RoleBook(HINTS)
    h = _harness(tmp_path, book)
    h._on_event(Event("proc", "proc_exit", {"code": 0, "by_harness": False, "after_op": "Summary/Read"}, 0))
    e = book.entries["Summary/Read"]
    assert (e.role, e.status) == ("exit", "estimated")
    assert h._exit_after_op and h._restart_req.is_set() and h.issues == []


def test_unexpected_exit_after_button_that_survived_before_is_not_exit(tmp_path):
    book = RoleBook(HINTS)
    h = _harness(tmp_path, book)
    h._fuzz = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.3), guard=Guard({}, book=book),
                      rng=random.Random(1), cfg=CFG, response_timeout=0.25)
    h._fuzz.survived.add("Summary/Read")
    h._on_event(Event("proc", "proc_exit", {"code": 0, "by_harness": False, "after_op": "Summary/Read"}, 0))
    assert "Summary/Read" not in book.entries
    assert any("終了ボタンとはみなさなかった" in n for n in h.notes)
    assert h._exit_after_op and h.issues == []


def test_button_pressed_before_next_press_survived():
    s, st, _, windows, _ = _session()
    s.pointer = Pointer(windows, RecordingDriver(), 1, st, activate_wait=0)
    _feed(s, st, "resolution", w=1000, h=500, mode="Windowed")
    _feed(s, st, "menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]},
                                                  {"name": "Gallery", "rect": [0.3, 0.1, 0.4, 0.2]}])
    for name in ("Start", "Gallery"):
        t = st.find(f"TitleUI/{name}")
        asyncio.run(s._do(Op("click", t, weight=1, group="button")))
    assert s.run.survived == {"TitleUI/Start"}  # 最後に押したものは、まだ終わらなかったか分からない
    # 起動し直したら、前の起動で最後に押したものは数えない
    s2 = FuzzSession(s.run, st, s.pointer, lambda e: None, 1, lambda _: None)
    asyncio.run(s2._do(Op("click", st.find("TitleUI/Start"), weight=1, group="button")))
    assert s.run.survived == {"TitleUI/Start"}


def test_title_menu_that_gets_buttons_later():
    # TitleUI はフェードイン中にボタンなしで出て、あとからボタン付きで出し直す（Rachel）
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("menu", name="TitleUI", buttons=[])
    feed("menu", name="TitleUI", buttons=[{"name": "Start", "rect": [0.1, 0.1, 0.2, 0.2]}])
    feed("menu", name="SaveLoadUI", modal=True, buttons=[{"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert st.title_menu == "TitleUI"


def test_roles_are_per_button_not_per_name():
    book = RoleBook(HINTS)
    book.confirm("AlbumUI/Slot#3", "external", "前面のアプリがゲーム以外に切り替わった")
    slot = lambda n: Target("button", "AlbumUI", "Slot", (0, 0, 0.1, 0.1), nth=n)
    assert book.role_of(slot(3)) == "external"
    assert book.role_of(slot(4)) is None  # 同じ名前のほかのスロットには効かない


def test_exit_interval_grows_with_long_runs():
    # run 2026-11-05_2230: 8 時間のランで 30 分ごとに終わらせ、起動し直すたびに最初から遊び直して先へ進めなかった
    s, *_ = _session()
    cfg = {**CFG, "exit_min_interval_sec": 1800, "exit_interval_ratio": 0.25}
    s.run.cfg = cfg
    s.run.run_sec = 3600
    assert s._exit_interval() == 1800  # 1 時間のランは今までどおり
    s.run.run_sec = 8 * 3600
    assert s._exit_interval() == 2 * 3600
    s.run.cfg = {**cfg, "exit_interval_ratio": 0}
    assert s._exit_interval() == 1800
