"""続きから遊ぶ・セーブ済みのスロット（fuzz/resume.py、DESIGN.md §9「続きから遊ぶ」）。"""

import dataclasses
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
from yomawari.fuzz.ops import Op, normal_candidates
from yomawari.fuzz.resume import load_save_hints, newest_slots, older_than, stamp_key
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
    assert f"click {_label(4)}" not in w  # 古いセーブは押さない
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
    # run 2026-11-06_1012: 本編の途中で前の章のセーブをロードし、いまの章から戻って 12 分やり直した。
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
    # run 2026-11-06_1418: 「8.2026 1F06 14:51:52」（日付が崩れた）より「9. 2026-1-06 12:58:58」を選び、前の章に戻った
    texts = {"a": "6. 2026-0-06 12:58:22", "b": "8.2026 1F06 14:51:52", "c": "9. 2026-1-06 12:58:58", "d": "7. Empty"}
    assert newest_slots(texts, time.mktime((2026, 11, 6, 15, 0, 0, 0, 0, -1))) == {"b"}
    assert newest_slots({"a": "1. 2026-11-05 23:00:00", "b": "2. 2026-11-06 01:00:00"}) == {"b"}
    assert newest_slots({"d": "7. Empty"}) == set()


SAVE_FACTOR = FUZZ_DEFAULTS["save_first_factor"]
RCLICK = [{"name": n, "rect": [0.4, 0.3 + 0.06 * i, 0.5, 0.35 + 0.06 * i]}
          for i, n in enumerate(["ToSave", "ToLoad", "ToTitle", "ToExit", "CloseButton"])]
RCLICK_TEXT = {"QuickMenuUI/ToSave": "Save", "QuickMenuUI/ToLoad": "Load", "QuickMenuUI/ToTitle": "Title",
               "QuickMenuUI/ToExit": "Exit", "QuickMenuUI/CloseButton": "back"}


def _save_weights(s, st):
    cands = normal_candidates(st, s.run.guard, s.cfg["weights"], False)
    return {op.label: op.weight for op in s._apply_save_first(s._apply_resume(cands, 0.0))}


def test_saves_before_trying_exit_button():
    # run 2026-11-06_1646: 22 分セーブしないまま終了ボタンを試し、起動し直したらロードするセーブがなく、はじめから遊び直した
    s, st, run = _session(launch=0)
    now = 1000.0
    assert not s._saved_for_exit(now) and run.save_first_until == now + FUZZ_DEFAULTS["save_first_sec"]
    # 右クリックメニュー: 「Save」を先に押す
    _show(st, "QuickMenuUI", RCLICK, RCLICK_TEXT)
    w = _save_weights(s, st)
    assert w["click QuickMenuUI/ToSave"] == CLICK * SAVE_FACTOR
    assert w["click QuickMenuUI/ToLoad"] == CLICK
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
    # ゲームが流れの中で自分でするセーブ（思い出を読む前の EpisodeReturn など）は数えない
    run.flow.active = object()
    s.on_event(Event("game", "save", {"slot": "EpisodeReturn", "phase": "start"}, 0))
    assert run.saved_t is None
    assert not s._saved_for_exit(0.0) and run.save_first_until is None  # 流れの途中は待つ
    run.flow.active = None
    assert not s._saved_for_exit(0.0)
    assert s._saved_for_exit(run.save_first_until + 1)  # セーブできないまま時間がたったら、終了を試す
    assert any("セーブできなかった" in n for n in run.notes)


def test_save_first_does_not_count_time_inside_flow():
    # run 2026-11-10_0957: 流れの外にいた一瞬にセーブしようとし始め、そのあとずっと流れの途中だった。流れの操作が先に
    # 選ばれて一度もセーブしないまま時間切れになり、流れの途中で終了を試して、約 2 時間前のセーブに戻った
    s, st, run = _session(launch=0)
    limit = FUZZ_DEFAULTS["save_first_sec"]
    assert not s._saved_for_exit(0.0) and run.save_first_until == limit
    run.flow.active = object()
    assert not s._saved_for_exit(10.0)
    assert not s._saved_for_exit(limit + 100.0)  # 流れの途中は時間切れにしない
    run.flow.active = None
    assert not s._saved_for_exit(limit + 101.0)  # 流れの途中だった 0〜limit+100 秒は数えない
    assert not s._saved_for_exit(limit + 100.0 + limit - 1)
    assert s._saved_for_exit(limit + 100.0 + limit)  # 流れの外で limit 秒たったら、あきらめて終了を試す
    assert any("セーブできなかった" in n for n in run.notes)


def test_exit_without_save_waits_for_flow_to_end():
    # セーブできないまま終了を試すことにしても、流れの途中では試さない。セーブしたら、流れの途中でも今までどおり試せる
    s, st, run = _session(launch=0)
    assert not s._saved_for_exit(0.0)
    assert s._saved_for_exit(run.save_first_until + 1)
    run.flow.active = object()
    assert not s._saved_for_exit(run.saved_t + 1)
    run.flow.active = None
    assert s._saved_for_exit(run.saved_t + 2)
    s.on_event(Event("game", "save", {"slot": "GameSave001", "phase": "start"}, 0))
    run.flow.active = object()
    assert s._saved_for_exit(time.monotonic())


def test_stamp_key_treats_impossible_date_as_unreadable():
    # run 2026-11-06_2157: 「8.2026-14-06 22:00:43」と読めた。月が 14 では、ほかのどれより新しくなってしまう
    assert stamp_key("8.2026-14-06 22:00:43") == (0, 0, 0, 22, 0, 43)


def test_relaunch_looks_for_own_save_on_other_pages():
    # run 2026-11-06_2157: 22:44 に 16 番（2 ページ目）へセーブしたのに、起動し直したあと 1 ページ目の 22:00 のセーブを
    # ロードして 44 分前に戻った。自分が最後にセーブした時刻より古いセーブしか見えなければ、ページを送って探す
    saved_at = time.mktime((2026, 11, 6, 22, 44, 9, 0, 0, -1))
    s, st, run = _session(launch=1)
    run.saved_wall = saved_at
    page1 = {_label(i): f"{i + 1}. Empty" for i in range(9)}
    page1[_label(4)] = "5.2026-11-06 22:00:47"
    page1[_label(7)] = "8.2026-14-06 22:00:43"
    _show(st, "SaveLoadUI", LOAD, page1)
    w = _weights(s, st, 0.0)
    assert w["click SaveLoadUI/NextPageButton"] == CLICK * BOOST
    assert f"click {_label(4)}" not in w and f"click {_label(7)}" not in w  # 古いセーブは押さない
    assert any("22:44:09" in n and "ページを送って探した" in n for n in run.notes)
    # 2 ページ目に自分のセーブがある: それを先に押す
    page2 = {_label(i): f"{i + 10}. Empty" for i in range(9)}
    page2[_label(1)] = "11.2026-11-06 22:10:27"
    page2[_label(6)] = "16.2026-11-06 22:44:09"
    _show(st, "SaveLoadUI", LOAD, page2)
    w = _weights(s, st, 0.0)
    assert w[f"click {_label(6)}"] == CLICK * BOOST and f"click {_label(1)}" not in w
    assert w["click SaveLoadUI/NextPageButton"] == CLICK
    # ページを送り尽くしても見つからなければ、見えているいちばん新しいものでよい
    _show(st, "SaveLoadUI", LOAD, page1)
    run.resume_pages = FUZZ_DEFAULTS["resume_page_limit"]
    w = _weights(s, st, 0.0)
    assert w[f"click {_label(4)}"] == CLICK * BOOST
    # このランでセーブしていなければ、今までどおり見えているいちばん新しいもの
    s, st, run = _session(launch=1)
    _show(st, "SaveLoadUI", LOAD, page1)
    assert _weights(s, st, 0.0)[f"click {_label(4)}"] == CLICK * BOOST


MIDNIGHT_PAGE1 = {"a": "1.202611F091 23:29:00", "b": "6. 2026-11-10 00:49:24", "c": "31.2026-1510 01:00:48"}


def test_newest_slot_across_midnight_with_unreadable_date():
    # run 2026-11-09_2259: 日付が崩れた前日 23:29 のセーブを、時刻の大小で当日 00:49・01:00 のセーブより新しいとして
    # 3 回ロードし、本編のかなり前に戻った。今から何秒前かで比べる
    now = time.mktime((2026, 11, 10, 1, 1, 16, 0, 0, -1))
    assert newest_slots(MIDNIGHT_PAGE1, now) == {"c"}
    assert newest_slots({"a": MIDNIGHT_PAGE1["a"], "b": MIDNIGHT_PAGE1["b"]}, now) == {"b"}


def test_older_than_across_midnight_with_unreadable_date():
    saved = time.mktime((2026, 11, 10, 1, 1, 7, 0, 0, -1))
    assert older_than((0, 0, 0, 23, 29, 0), saved, 60.0)  # 前日 23:29 は 1 時間 32 分前
    assert not older_than((0, 0, 0, 1, 0, 48), saved, 60.0)  # 19 秒前（slack のうち）
    assert not older_than((0, 0, 0, 1, 1, 30), saved, 60.0)  # 少しあと
    assert older_than((0, 0, 0, 0, 49, 24), saved, 60.0)


def test_relaunch_after_midnight_looks_for_own_save(monkeypatch):
    # 01:01:07 に 4 ページ目へセーブして起動し直した。1 ページ目には前日 23:29（日付が崩れた）と当日 00:49 のセーブしか
    # 見えない: どちらも押さず、ページを送って探す
    monkeypatch.setattr(time, "time", lambda: time.mktime((2026, 11, 10, 1, 1, 18, 0, 0, -1)))
    s, st, run = _session(launch=1)
    run.saved_wall = time.mktime((2026, 11, 10, 1, 1, 7, 0, 0, -1))
    _show(st, "SaveLoadUI", LOAD, _page({_label(0): MIDNIGHT_PAGE1["a"], _label(5): MIDNIGHT_PAGE1["b"]}))
    w = _weights(s, st, 0.0)
    assert w["click SaveLoadUI/NextPageButton"] == CLICK * BOOST
    assert f"click {_label(0)}" not in w and f"click {_label(5)}" not in w
    # 探し尽くしたら、見えているうちで新しい当日 00:49 を選ぶ（前日 23:29 ではない）
    run.resume_pages = FUZZ_DEFAULTS["resume_page_limit"]
    w = _weights(s, st, 0.0)
    assert w[f"click {_label(5)}"] == CLICK * BOOST and f"click {_label(0)}" not in w


def test_relaunch_forgets_last_choice_on_title():
    # run 2026-11-09_2259: 前の起動のタイトルで「続きから」を押したので、次の起動では「直前と同じ操作」として
    # 「続きから」が候補から外れ、「はじめから」を押した。起動し直したら直前の操作を忘れる
    s, st, run = _session(launch=1)
    _show(st, "TitleUI", TITLE, TITLE_TEXT)
    cands = s._apply_resume(normal_candidates(st, run.guard, s.cfg["weights"], False), 0.0)
    cont = next(op for op in cands if op.label == "click TitleUI/ContinueButton")
    # 同じ起動の中では、今までどおり直前と同じ操作を続けて選ばない
    run.decider._last["title"] = cont.label
    assert run.decider.choose("title", cands).label != cont.label
    run.decider._last["title"] = cont.label
    FuzzSession(run, st, s.pointer, lambda _: None, 2, lambda _: None)
    assert not run.decider._last
    # 忘れたあとは「続きから」が候補に残り、resume_factor の重みでほぼ選ばれる
    picks = [run.decider.choose("title", cands).label for _ in range(1)]
    assert picks == [cont.label]


def _page(saved):
    texts = {_label(i): f"{i + 1}. Empty" for i in range(9)}
    texts.update(saved)
    return texts


def test_resume_never_presses_an_older_save_even_with_list_weight():
    # 一覧の探索の重みが付いていても、いちばん新しいセーブ以外は候補に残さない
    s, st, run = _session(launch=1)
    _show(st, "SaveLoadUI", LOAD, _page({_label(1): "2. 2026-11-06 10:00:00", _label(2): "3. 2026-11-06 11:00:00"}))
    cands = normal_candidates(st, s.run.guard, s.cfg["weights"], False)
    cands = [op.scaled("list", 20.0) if op.target is not None and op.target.label == _label(1) else op for op in cands]
    labels = {op.label for op in s._apply_resume(cands, 0.0)}
    assert f"click {_label(1)}" not in labels and f"click {_label(2)}" in labels


def test_resume_pages_instead_of_filling_list_ledger():
    # run 2026-11-10_0957: 起動し直したあと、見えているセーブは自分の最後のセーブより古く、ページを送ると決めたのに、
    # 一覧の探索（台帳を埋める）が空のスロットを目標にして 97% で選ばれ、ページを一度も送らないまま「はじめから」を押した
    s, st, run = _session(launch=1)
    run.saved_wall = time.mktime((2026, 11, 10, 12, 27, 17, 0, 0, -1))
    _show(st, "SaveLoadUI", LOAD, _page({_label(4): "2026-115101 10:14:39"}))
    cands = normal_candidates(st, s.run.guard, s.cfg["weights"], False)
    empty = next(op for op in cands if op.target is not None and op.target.label == _label(1))
    cands.append(dataclasses.replace(empty, by="list", group="list", goal=_label(1)).scaled("list", 20.0))
    cands.append(Op("drag", arg="down", weight=CLICK, by="blind", group="search"))
    out = s._apply_resume(cands, 0.0)
    assert not [op for op in out if op.by in ("list", "blind")]
    w = {op.label: op.weight for op in out}
    assert w["click SaveLoadUI/NextPageButton"] == CLICK * BOOST
    assert w["click SaveLoadUI/NextPageButton"] > w[f"click {_label(1)}"]
    # ページを送らないとき（見えているセーブが新しい）は、一覧の探索をそのまま残す
    s, st, run = _session(launch=1)
    _show(st, "SaveLoadUI", LOAD, _page({_label(4): "5. 2026-11-10 10:14:39"}))
    cands = normal_candidates(st, s.run.guard, s.cfg["weights"], False)
    cands.append(Op("drag", arg="down", weight=CLICK, by="list", group="list", goal=_label(8)))
    assert [op for op in s._apply_resume(cands, 0.0) if op.by == "list"]


def test_resume_accepts_visible_save_when_there_is_no_next_page():
    # 探すページがなければ、見えているいちばん新しいセーブでよい（はじめから遊び直すより近い）
    s, st, run = _session(launch=1)
    run.saved_wall = time.mktime((2026, 11, 6, 22, 44, 9, 0, 0, -1))
    no_next = [b for b in LOAD if b["name"] != "NextPageButton"]
    _show(st, "SaveLoadUI", no_next, _page({_label(4): "5. 2026-11-06 22:00:47"}))
    assert _weights(s, st, 0.0)[f"click {_label(4)}"] == CLICK * BOOST


def test_unread_slots_are_not_pressed_after_saved_ones_were_seen():
    # 実機: ページを送った直後、文字を読む前のスロットを押して、古いセーブをロードした
    s, st, run = _session(launch=0)
    _show(st, "SaveLoadUI", LOAD, _page({_label(6): "7. 2026-11-06 10:46:59"}))
    _weights(s, st, 0.0)
    _show(st, "SaveLoadUI", LOAD, {})  # 次のページ。文字はまだ読めていない
    w = _weights(s, st, 0.0)
    assert not [k for k in w if "SaveLoadSlot" in k]
    assert w["click SaveLoadUI/NextPageButton"] > 0
    # 読めたら、空のスロットは押せる
    _show(st, "SaveLoadUI", LOAD, _page({}))
    assert _weights(s, st, 0.0)[f"click {_label(5)}"] > 0


def test_unread_slots_are_pressable_before_any_saved_slot_was_seen():
    s, st, run = _session(launch=0)
    _show(st, "SaveLoadUI", LOAD, {})
    assert _weights(s, st, 0.0)[f"click {_label(5)}"] > 0


def test_enter_is_not_used_while_saved_slots_are_visible():
    # 実機: セーブ画面でフォーカスのあるスロットを Enter で押し、古いセーブをロードした
    s, st, run = _session(launch=0)
    _show(st, "SaveLoadUI", LOAD, _page({_label(6): "7. 2026-11-06 10:46:59"}))
    assert "key enter" not in _weights(s, st, 0.0)
    _show(st, "SaveLoadUI", LOAD, _page({}))
    assert "key enter" in _weights(s, st, 0.0)  # 空のスロットだけなら使う
