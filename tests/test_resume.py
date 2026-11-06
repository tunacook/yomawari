"""続きから遊ぶ・セーブ済みのスロット（fuzz/resume.py、DESIGN.md §9「続きから遊ぶ」）。"""

import random
import time
from pathlib import Path

import pytest

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.looks import load_inactive_words
from yomawari.fuzz.ops import normal_candidates
from yomawari.fuzz.resume import load_save_hints, newest_slots, stamp_key
from yomawari.fuzz.roles import RoleBook, load_role_hints
from yomawari.input.pointer import Pointer
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState

from .test_window import FakeWindows

RULES = Path(__file__).resolve().parent.parent / "rules.yaml"
CLICK = FUZZ_DEFAULTS["weights"]["click"]
BOOST, LOW = FUZZ_DEFAULTS["resume_factor"], FUZZ_DEFAULTS["resume_new_game_factor"]

# Rachel のタイトルとロード画面（run 2026-11-06_1012 で読んだ文字）
TITLE = [{"name": n, "rect": [0.3, 0.3 + 0.08 * i, 0.5, 0.36 + 0.08 * i]}
         for i, n in enumerate(["NewGameButton", "ContinueButton", "SettingsButton"])]
TITLE_TEXT = {"TitleUI/NewGameButton": "START", "TitleUI/ContinueButton": "CONTINUE", "TitleUI/SettingsButton": "SETTINGS"}
SLOTS = [{"name": "SaveLoadSlot (Clone)", "rect": [0.35 + 0.15 * (i % 3), 0.3 + 0.1 * (i // 3),
                                                  0.48 + 0.15 * (i % 3), 0.38 + 0.1 * (i // 3)]} for i in range(9)]
LOAD = SLOTS + [{"name": "NextPageButton", "rect": [0.8, 0.5, 0.85, 0.55]},
                {"name": "PreviousPageButton", "rect": [0.15, 0.5, 0.2, 0.55]},
                {"name": "ReturnButton", "rect": [0.45, 0.85, 0.55, 0.9]}]


def _label(i):
    return "SaveLoadUI/SaveLoadSlot (Clone)" + (f"#{i}" if i else "")


def _session(launch, cont=False):
    st = GameState()
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2),
                  guard=Guard({}, None, RoleBook(load_role_hints(RULES))), rng=random.Random(1), cfg=cfg,
                  response_timeout=0.1, inactive_words=load_inactive_words(RULES), save_hints=load_save_hints(RULES),
                  continue_previous=cont)
    run.plays = 1 if launch else 0
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    events = []
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), events.append, launch, lambda _: None)
    return s, st, run


def _feed(st, kind, **d):
    st.feed(Event("game", kind, d, 0))


def _show(st, name, buttons, texts):
    for m in list(st.menus):
        _feed(st, "menu_close", name=m)
    _feed(st, "menu", name=name, modal=True, buttons=buttons)
    st.set_read_text(texts)


def _weights(s, st, now):
    cands = normal_candidates(st, s.run.guard, s.cfg["weights"], False)
    return {op.label: op.weight for op in s._apply_resume(cands, now)}


def test_stamp_key_reads_date_and_time():
    assert stamp_key("16. 2026-11-06 10:46:59") == (2026, 11, 6, 10, 46, 59)
    # 日付が OCR で崩れたら時刻だけ
    assert stamp_key("16. 2026511-06 10:46:59") == (0, 0, 0, 10, 46, 59)
    assert stamp_key("1. Empty") is None


def test_relaunch_continues_from_newest_save():
    # run 2026-11-06_1012: 終了ボタンを確かめて起動し直したあと、ロード画面の 1 ページ目がどれも空で、はじめから始めた
    s, st, run = _session(launch=1)
    now = 0.0
    assert run.resume_until is not None
    _show(st, "TitleUI", TITLE, TITLE_TEXT)
    w = _weights(s, st, now)
    assert w["click TitleUI/ContinueButton"] == CLICK * BOOST
    assert w["click TitleUI/NewGameButton"] == pytest.approx(CLICK * LOW)
    assert w["click TitleUI/SettingsButton"] == CLICK
    # 1 ページ目はどれも空: ページを送る
    _show(st, "SaveLoadUI", LOAD, {_label(i): f"{i + 1}. Empty" for i in range(9)})
    w = _weights(s, st, now)
    assert w["click SaveLoadUI/NextPageButton"] == CLICK * BOOST
    assert w["click SaveLoadUI/PreviousPageButton"] == CLICK
    # 2 ページ目にセーブがある: いちばん新しいものを先に押す
    texts = {_label(i): f"{i + 10}. Empty" for i in range(9)}
    texts[_label(4)] = "14. 2026-11-06 10:20:00"
    texts[_label(6)] = "16. 2026-11-06 10:46:59"
    _show(st, "SaveLoadUI", LOAD, texts)
    w = _weights(s, st, now)
    assert w[f"click {_label(6)}"] == CLICK * BOOST
    assert w[f"click {_label(4)}"] == CLICK
    assert w["click SaveLoadUI/NextPageButton"] == CLICK
    # ロードしたら終える
    s.on_event(Event("game", "load", {"slot": "GameSave016", "phase": "start"}, 1))
    assert run.resume_until is None
    assert any("セーブ（GameSave016）をロードした" in n for n in run.notes)


def test_first_launch_and_timeout_do_not_resume():
    s, st, run = _session(launch=0)
    assert run.resume_until is None
    s, st, run = _session(launch=1)
    _show(st, "TitleUI", TITLE, TITLE_TEXT)
    w = _weights(s, st, run.resume_until + 1)
    assert w["click TitleUI/NewGameButton"] == CLICK  # セーブが見つからないまま時間がたったら、はじめからも選ぶ
    assert run.resume_until is None
    assert any("ロードも本文の始まりもなかった" in n for n in run.notes)


def test_continue_option_resumes_on_first_launch():
    # --continue: 前のランが終わりまで届かなかったとき、はじめから遊び直さずにその先を見る
    s, st, run = _session(launch=0, cont=True)
    assert run.resume_until is not None
    _show(st, "TitleUI", TITLE, TITLE_TEXT)
    w = _weights(s, st, 0.0)
    assert w["click TitleUI/ContinueButton"] == CLICK * BOOST
    assert w["click TitleUI/NewGameButton"] == pytest.approx(CLICK * LOW)
    s.on_event(Event("game", "load", {"slot": "GameSave016", "phase": "start"}, 0))
    assert run.resume_until is None
    assert any("前のランの続きから遊ぶ（--continue）ので" in n for n in run.notes)


def test_saved_slots_are_not_pressed_without_purpose():
    # run 2026-11-06_1012: 本編の途中で A_009 のセーブをロードし、B_004 から戻って 12 分やり直した。
    # 通常のプレイでは、自分のセーブのロードは続きから遊ぶときだけにする
    s, st, run = _session(launch=0)
    texts = {_label(i): f"{i + 1}. Empty" for i in range(9)}
    texts[_label(6)] = "7. 2026-11-06 10:46:59"
    _show(st, "SaveLoadUI", LOAD, texts)
    for intent in ("progress", "explore"):
        run.intent = intent
        w = _weights(s, st, 0.0)
        assert f"click {_label(6)}" not in w and w[f"click {_label(5)}"] > 0
    # ロードとの行き来を試す目的（stuck）では押す
    s.cfg["saved_slot_factor"] = 1.0
    assert _weights(s, st, 0.0)[f"click {_label(6)}"] == CLICK


def test_newest_slot_compares_time_only_when_some_date_is_unreadable():
    # run 2026-11-06_1418: 「8.2026 1F06 14:51:52」（日付が崩れた）より「9. 2026-1-06 12:58:58」を選び、A_004 に戻った
    texts = {"a": "6. 2026-0-06 12:58:22", "b": "8.2026 1F06 14:51:52", "c": "9. 2026-1-06 12:58:58", "d": "7. Empty"}
    assert newest_slots(texts) == {"b"}
    assert newest_slots({"a": "1. 2026-11-05 23:00:00", "b": "2. 2026-11-06 01:00:00"}) == {"b"}
    assert newest_slots({"d": "7. Empty"}) == set()


SAVE_FACTOR = FUZZ_DEFAULTS["save_first_factor"]
RCLICK = [{"name": n, "rect": [0.4, 0.3 + 0.06 * i, 0.5, 0.35 + 0.06 * i]}
          for i, n in enumerate(["ToSave", "ToLoad", "ToTitle", "ToExit", "CloseButton"])]
RCLICK_TEXT = {"RClickMenuUI/ToSave": "Save", "RClickMenuUI/ToLoad": "Load", "RClickMenuUI/ToTitle": "Title",
               "RClickMenuUI/ToExit": "Exit", "RClickMenuUI/CloseButton": "back"}


def _save_weights(s, st):
    cands = normal_candidates(st, s.run.guard, s.cfg["weights"], False)
    return {op.label: op.weight for op in s._apply_save_first(s._apply_resume(cands, 0.0))}


def test_saves_before_trying_exit_button():
    # run 2026-11-06_1646: 22 分セーブしないまま終了ボタンを試し、起動し直したらロードするセーブがなく、はじめから遊び直した
    s, st, run = _session(launch=0)
    now = 1000.0
    assert not s._saved_for_exit(now) and run.save_first_until == now + FUZZ_DEFAULTS["save_first_sec"]
    # 右クリックメニュー: 「Save」を先に押す
    _show(st, "RClickMenuUI", RCLICK, RCLICK_TEXT)
    w = _save_weights(s, st)
    assert w["click RClickMenuUI/ToSave"] == CLICK * SAVE_FACTOR
    assert w["click RClickMenuUI/ToLoad"] == CLICK
    # セーブ画面: セーブのタブと空きのスロットを先に押す。セーブ済みのスロットは押さない（上書きしない）
    buttons = SLOTS[:3] + [{"name": "SaveToggle", "rect": [0.2, 0.2, 0.3, 0.25]},
                           {"name": "ReturnButton", "rect": [0.45, 0.85, 0.55, 0.9]}]
    texts = {_label(0): "1. Empty", _label(1): "2. 2026-11-06 16:01:40", _label(2): "3.Empty",
             "SaveLoadUI/SaveToggle": "SAVE", "SaveLoadUI/ReturnButton": "RETURN"}
    _show(st, "SaveLoadUI", buttons, texts)
    w = _save_weights(s, st)
    assert w["click SaveLoadUI/SaveToggle"] == CLICK * SAVE_FACTOR
    assert w[f"click {_label(0)}"] == CLICK * SAVE_FACTOR and w[f"click {_label(2)}"] == CLICK * SAVE_FACTOR
    assert f"click {_label(1)}" not in w and w["click SaveLoadUI/ReturnButton"] == CLICK
    # セーブしたら終了を試す
    s.on_event(Event("game", "save", {"slot": "GameSave001", "phase": "start"}, 0))
    assert run.save_first_until is None and any("終了ボタンを試す前にセーブした（GameSave001" in n for n in run.notes)
    assert s._saved_for_exit(time.monotonic())


def test_save_first_gives_up_and_skips_inside_flow_saves():
    s, st, run = _session(launch=0)
    # ゲームが流れの中で自分でするセーブ（思い出を読む前の MemoryReturn など）は数えない
    run.flow.active = object()
    s.on_event(Event("game", "save", {"slot": "MemoryReturn", "phase": "start"}, 0))
    assert run.saved_t is None
    assert not s._saved_for_exit(0.0) and run.save_first_until is None  # 流れの途中は待つ
    run.flow.active = None
    assert not s._saved_for_exit(0.0)
    assert s._saved_for_exit(run.save_first_until + 1)  # セーブできないまま時間がたったら、終了を試す
    assert any("セーブできなかった" in n for n in run.notes)
