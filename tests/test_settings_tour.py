"""設定を変えに行く（fuzz/settings_tour.py）と F-03（設定が次の起動に残らない）。"""

import asyncio
import random

from yomawari.detect.engine import Engine
from yomawari.events import Event
from yomawari.fuzz.experience import Experience
from yomawari.fuzz.ops import Op
from yomawari.fuzz.settings_tour import SettingsTour, VolumeHold

from .test_fuzz import _LANG, _PAUSE, _SETTINGS, _locale_session

_SOUND = {"name": "SettingsUI", "modal": True, "buttons": [
    {"name": "Option_a", "rect": [0.1, 0.5, 0.3, 0.6], "type": "other"},
    {"name": "Option_b", "rect": [0.5, 0.5, 0.7, 0.6], "type": "other"},
    {"name": "Mute", "rect": [0.1, 0.65, 0.3, 0.75], "type": "other"},
    {"name": "BackButton", "rect": [0.1, 0.8, 0.3, 0.9]}]}


def _press(st, s, label, **values):
    """ボタンを押すと、settings の値が values に変わる。"""
    target = {t.label: t for t in st.targets()}[label]

    def click(*a, **k):
        st.feed(Event("game", "settings", {**(st.settings or {}), **values}, 0))

    s.pointer.driver.click = click
    asyncio.run(s._normal(Op("click", target), "modal:SettingsUI", False))


def _weights(st, s):
    return {op.target.label: op.weight for op in s._apply_tour([Op("click", t) for t in st.targets()])}


def test_setting_buttons_and_routes_are_learned_per_entry(tmp_path):
    st, run, s = _locale_session("ja-JP", _PAUSE, _SOUND)
    run.experience = Experience(tmp_path / "exp.json")
    run.opened_by.update({"PauseUI": "MainUI/MenuButton", "SettingsUI": "PauseUI/SettingsButton"})
    _press(st, s, "SettingsUI/Mute", master_volume=0)
    # 同じ画面を、別の入り口（タイトル画面）から開いて押した
    run.opened_by = {"SettingsUI": "TitleUI/SettingsButton"}
    _press(st, s, "SettingsUI/Option_a", reveal_speed=1)
    exp = run.experience
    assert exp.setting_buttons == {"SettingsUI/Mute": {"master_volume": 0}, "SettingsUI/Option_a": {"reveal_speed": 1}}
    assert exp.setting_routes == {"SettingsUI": [["TitleUI/SettingsButton"],
                                                  ["MainUI/MenuButton", "PauseUI/SettingsButton"]]}
    exp.save()
    again = Experience(tmp_path / "exp.json")
    assert again.setting_buttons == exp.setting_buttons and again.setting_routes == exp.setting_routes
    # 押して変わった設定は、このランで変わった回数に数える
    assert run.tour.changes == {"master_volume": 1, "reveal_speed": 1}


def test_tour_picks_least_changed_setting_and_boosts_buttons_then_route():
    st, run, s = _locale_session("ja-JP", _PAUSE)
    run.tour = SettingsTour(interval=100, timeout=50, factor=20)
    exp = run.experience
    exp.setting_buttons = {"SettingsUI/Option_a": {"reveal_speed": 1}, "SettingsUI/Option_b": {"reveal_speed": 0.5},
                           "SettingsUI/Mute": {"master_volume": 0}}
    exp.setting_routes = {"SettingsUI": [["PauseUI/SettingsButton"]]}
    run.tour.changes["reveal_speed"] = 3
    run.tour.step(0.0, exp.setting_buttons, random.Random(1))
    assert run.tour.target is None  # 間隔がたつまでは行かない
    run.tour.step(100.0, exp.setting_buttons, random.Random(1))
    assert run.tour.target == "master_volume"  # このランで変わった回数がいちばん少ない設定
    # 設定の画面が出ていない: その画面を開くボタンを優先する
    w = _weights(st, s)
    assert w["PauseUI/SettingsButton"] == 20.0 and w["PauseUI/CloseButton"] == 1.0
    # 設定の画面が出ている: その設定を変えるボタンだけを優先する
    st.feed(Event("game", "menu", _SOUND, 0))
    w = _weights(st, s)
    assert w["SettingsUI/Mute"] == 20.0 and w["SettingsUI/Option_a"] == 1.0 and w["SettingsUI/BackButton"] == 1.0
    # 変えたら終わり。次は間隔がたってから
    run.tour.changed({"master_volume"}, "PauseUI/SettingsButton", 120.0)
    assert run.tour.target is None and run.tour.next_t == 220.0
    assert set(_weights(st, s).values()) == {1.0}


def test_tour_prefers_buttons_that_change_the_value():
    st, run, s = _locale_session("ja-JP", _SOUND)
    run.tour = SettingsTour(interval=1, timeout=50, factor=20, next_t=0.0)
    exp = run.experience
    # いまの reveal_speed は 0.5。Option_b を押しても変わらないので、Option_a を先に押す
    exp.setting_buttons = {"SettingsUI/Option_a": {"reveal_speed": 1}, "SettingsUI/Option_b": {"reveal_speed": 0.5}}
    run.tour.step(0.0, exp.setting_buttons, random.Random(1))
    w = _weights(st, s)
    assert w["SettingsUI/Option_a"] == 20.0 and w["SettingsUI/Option_b"] == 1.0


def test_tour_pauses_and_gives_up():
    st, run, s = _locale_session("en-US", _LANG)
    run.tour = SettingsTour(interval=1, timeout=10, factor=20, next_t=0.0)
    run.experience.setting_buttons = {"SettingsUI/Option_a": {"reveal_speed": 1}}
    # 言語を戻しているあいだは変えに行かない
    s._step_tour(5.0)
    assert run.tour.target is None
    st.feed(Event("game", "settings", {**_SETTINGS, "locale": "ja-JP"}, 0))
    s._step_tour(5.0)
    assert run.tour.target == "reveal_speed"
    s._step_tour(16.0)
    assert run.tour.target is None and run.tour.gave_up == {"reveal_speed": 1}
    assert any("reveal_speed を変えに行ったが" in n for n in run.notes)


def test_tour_disabled_by_default():
    st, run, s = _locale_session("ja-JP", _SOUND)
    run.experience.setting_buttons = {"SettingsUI/Mute": {"master_volume": 0}}
    for t in (0.0, 1e6):
        s._step_tour(t)
    assert run.tour.target is None


def test_settings_notes():
    tour = SettingsTour(interval=1, timeout=1, factor=20)
    tour.observe(_SETTINGS)
    tour.target, tour.tours = "master_volume", 2
    tour.changed({"master_volume"}, "TitleUI/SettingsButton", 0.0)
    tour.gave_up["reveal_speed"] = 1
    notes = tour.notes({"SettingsUI/Mute": {"master_volume": 0}, "SettingsUI/Option_a": {"reveal_speed": 1}})
    assert notes[0].startswith("設定を変えに 2 回行った（変えられた 1 回、時間のうちに変えられなかった reveal_speed 1 回）")
    assert "`TitleUI/SettingsButton` 1 回" in notes[0]
    assert notes[1] == "このランで変わった設定: master_volume 1 回"
    assert "reveal_speed、" in notes[2] and "auto_delay（変えるボタンを見つけていない）" in notes[2]
    assert "locale（変えるボタンを見つけていない）" in notes[2]


# --- F-03 ---

def _f03(repo_rules):
    return Engine({"F-03": repo_rules["F-03"]})


def _ev(kind, launch, t, source="game", **data):
    return Event(source, kind, data, launch=launch, t=t, wall=f"w{t}")


def _settings(launch, t, **values):
    return _ev("settings", launch, t, **{**_SETTINGS, **values})


def _change_and_exit(launch, close=True, code=-15, by_harness=True, restart=False):
    evs = [
        _ev("launch", launch, launch * 100, source="harness"),
        _settings(launch, launch * 100 + 1),
        _ev("menu", launch, launch * 100 + 2, name="SettingsUI", modal=True, buttons=[]),
        _settings(launch, launch * 100 + 3, master_volume=0),
        _ev("op_result", launch, launch * 100 + 3, source="harness", target="SettingsUI/Mute",
            settings_changed=True, screen_changed=False),
    ]
    if close:
        evs.append(_ev("menu_close", launch, launch * 100 + 4, name="SettingsUI"))
    if restart:
        evs.append(_ev("issue", launch, launch * 100 + 5, source="harness", rule_id="H-01", action="restart"))
    evs.append(_ev("proc_exit", launch, launch * 100 + 6, source="proc", code=code, by_harness=by_harness))
    return evs


def _feed(engine, evs):
    out = []
    for e in evs:
        out += engine.feed(e)
    return out


def test_f03_setting_reverted_after_relaunch(repo_rules):
    e = _f03(repo_rules)
    issues = _feed(e, _change_and_exit(0) + [_ev("launch", 1, 100, source="harness"), _settings(1, 101)])
    assert [(i.rule_id, i.severity, i.evidence["setting"], i.evidence["before"], i.evidence["after"])
            for i in issues] == [("F-03", "major", "master_volume", 0, 1)]


def test_f03_setting_kept(repo_rules):
    e = _f03(repo_rules)
    assert _feed(e, _change_and_exit(0) + [_ev("launch", 1, 100, source="harness"),
                                           _settings(1, 101, master_volume=0.0)]) == []


def test_f03_settings_screen_open_at_exit_accepts_either(repo_rules):
    # 設定の画面を開いたまま終わった: 閉じたときの値（変える前）でも、最後の値でもよい
    for after in (1, 0):
        e = _f03(repo_rules)
        evs = _change_and_exit(0, close=False) + [_ev("launch", 1, 100, source="harness"),
                                                   _settings(1, 101, master_volume=after)]
        assert _feed(e, evs) == []


def test_f03_skips_after_crash_or_restart(repo_rules):
    for kw in ({"code": 134, "by_harness": False}, {"restart": True}):
        e = _f03(repo_rules)
        evs = _change_and_exit(0, **kw) + [_ev("launch", 1, 100, source="harness"), _settings(1, 101)]
        assert _feed(e, evs) == []


def test_f03_compares_only_first_settings_of_launch(repo_rules):
    e = _f03(repo_rules)
    evs = _change_and_exit(0) + [_ev("launch", 1, 100, source="harness"), _settings(1, 101, master_volume=0),
                                 _settings(1, 102, master_volume=1)]
    assert _feed(e, evs) == []


def test_response_without_setting_change_is_not_counted_as_no_response():
    st, run, s = _locale_session("ja-JP", _PAUSE)
    target = {t.label: t for t in st.targets()}["PauseUI/SettingsButton"]

    def click(*a, **k):
        st.feed(Event("game", "menu", _SOUND, 0))  # 設定の画面が開いた（設定の値は変わらない）

    s.pointer.driver.click = click
    asyncio.run(s._normal(Op("click", target), "modal:PauseUI", False))
    assert run.stats["no_response"] == 0 and not run.tour.changes


def test_route_starts_with_right_click_and_tour_presses_it(tmp_path):
    # 右クリックで開くメニューの奥の設定画面: 開き方の最初の一歩に right_click を覚え、変えに行くときに押しにいく
    st, run, s = _locale_session("ja-JP")
    run.experience = Experience(tmp_path / "exp.json")

    async def do(op, _show=_PAUSE):
        st.feed(Event("game", "menu", _show, 0))
        return True

    s._do = do
    asyncio.run(s._normal(Op("right_click"), "reading", False))
    assert run.opened_via == {"PauseUI": "right_click"} and run.opened_by == {}  # 流れ・一覧の寄り道は変えない

    async def do2(op):
        st.feed(Event("game", "menu", _SOUND, 0))
        return True

    s._do = do2
    target = {t.label: t for t in st.targets()}["PauseUI/SettingsButton"]
    asyncio.run(s._normal(Op("click", target), "modal:PauseUI", False))
    del s._do  # 元の押し方に戻す（_press は driver.click を差し替える）
    _press(st, s, "SettingsUI/Mute", master_volume=0)
    assert run.experience.setting_routes == {"SettingsUI": [["right_click", "PauseUI/SettingsButton"]]}

    # 画面がどれも出ていない: 右クリック（ボタンでない操作）を優先する
    st2, _, s2 = _locale_session("ja-JP", run=run)
    run.tour = SettingsTour(interval=1, timeout=50, factor=20, next_t=0.0)
    run.tour.step(0.0, run.experience.setting_buttons, random.Random(1))
    w = {op.label: op.weight for op in s2._apply_tour([Op("right_click", weight=1.0), Op("advance", weight=20.0)])}
    assert w == {"right_click": 20.0, "advance": 20.0}


def test_move_keys_are_not_openers():
    st, run, s = _locale_session("ja-JP")

    async def do(op):
        st.feed(Event("game", "menu", _PAUSE, 0))
        return True

    s._do = do
    asyncio.run(s._normal(Op("key", arg="down", group="move"), "reading", False))
    assert run.opened_via == {}


def test_locale_route_with_right_click():
    st, run, s = _locale_session("en-US")
    run.experience.locale_buttons = {"SettingsUI/Option_b": "ja-JP"}
    run.experience.locale_routes = {"SettingsUI": ["right_click", "PauseUI/SettingsButton"]}
    w = {op.label: op.weight for op in s._apply_locale([Op("right_click", weight=1.0), Op("advance", weight=20.0)])}
    assert w == {"right_click": 20.0, "advance": 20.0}


def _labels(cands):
    return {op.target.label for op in cands}


def test_volume_hold_keeps_other_settings_still_while_a03_compares():
    # run 2026-11-15_2240: 音量を 61 回変えたのに、ほかの設定を続けて変えたため A-03 が 1 回しか判定できなかった
    st, run, s = _locale_session("ja-JP", _SOUND)
    run.volume_hold = VolumeHold(before=5, after=6.5)
    run.experience.setting_buttons = {"SettingsUI/Option_a": {"reveal_speed": 1},
                                      "SettingsUI/Mute": {"master_volume": 0}}
    hold = run.volume_hold
    cands = [Op("click", t) for t in st.targets()]
    hold.observe(st.settings, 0.0)
    assert hold.apply(cands, run.experience.setting_buttons, 0.0) == cands
    # 音量が変わった: 比べる区間のあいだは、設定を変えるボタンをどれも押さない。ほかのボタンは押せる
    hold.observe({**st.settings, "master_volume": 0}, 10.0)
    left = _labels(hold.apply(cands, run.experience.setting_buttons, 16.0))
    assert left == {"SettingsUI/Option_b", "SettingsUI/BackButton"}
    assert hold.holding(16.0) and not hold.holding(16.5)
    assert len(hold.apply(cands, run.experience.setting_buttons, 16.5)) == len(cands)
    # ほかの設定が変わった: window_sec のあいだは、音量のボタンだけ押さない（変える前の区間に入るため）
    hold.observe({**st.settings, "master_volume": 0, "reveal_speed": 1}, 20.0)
    assert not hold.holding(20.0)
    assert _labels(hold.apply(cands, run.experience.setting_buttons, 24.9)) == _labels(cands) - {"SettingsUI/Mute"}
    assert len(hold.apply(cands, run.experience.setting_buttons, 25.0)) == len(cands)
    assert hold.notes() == ["音量を変えたあと 1 回、A-03 が比べる 6.5 秒のあいだ、ほかの設定を変えずに待った"]


def test_volume_hold_skips_irregular_ops_and_is_off_by_default():
    st, run, s = _locale_session("ja-JP", _SOUND)
    assert not run.volume_hold.enabled and run.volume_hold.notes() == []
    run.volume_hold.observe(st.settings, 0.0)
    run.volume_hold.observe({**st.settings, "bgm_volume": 0}, 1.0)
    assert not run.volume_hold.holding(1.0)  # A-03 がなければ待たない
    done = []

    async def do(op, **k):
        done.append(op)
        return True

    s._do = do
    st.booted = True
    asyncio.run(s._irregular("random"))
    assert len(done) >= 1  # 待っていなければ、イレギュラーな操作をする
    done.clear()
    run.volume_hold = VolumeHold(before=5, after=6.5, until=float("inf"))
    asyncio.run(s._irregular("random"))
    assert done == []


def test_tour_and_notes_follow_profile_offered():
    # プロファイルで外した設定は、変えに行かず、「変わらなかった設定」にも挙げない
    tour = SettingsTour(interval=1, timeout=50, factor=20, next_t=0.0,
                        offered=frozenset({"reveal_speed", "master_volume", "resolution"}))
    tour.observe({"locale": "ja-JP", "reveal_speed": 0.5, "auto_delay": 0.5, "master_volume": 1, "voice_volume": 1})
    buttons = {"SettingsUI/Option_a": {"reveal_speed": 1}, "SettingsUI/Voice": {"voice_volume": 0}}
    for _ in range(5):
        tour.target = None
        tour.step(1.0, buttons, random.Random(1))
        assert tour.target == "reveal_speed"
    notes = tour.notes(buttons)
    assert "このランで一度も変わらなかった設定: master_volume（変えるボタンを見つけていない）、reveal_speed" in notes
    assert any("offered にあるのに、ゲームが値を出していない設定: resolution" in n for n in notes)
    assert not any("auto_delay" in n or "voice_volume" in n for n in notes)
