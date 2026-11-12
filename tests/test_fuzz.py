"""ファジング（fuzz/）と S 系・C-04 の検出器。

ゲームの代わりに、同じプロセス内の SimGame がクリックを受けてイベントを返す。
OS の入力とウィンドウは、tests/test_window.py と同じ作りのフェイクに差し替える。
"""

import asyncio
import random

import pytest

from yomawari.config import FUZZ_DEFAULTS, GUARD_ROLE_DEFAULTS, Config, ConfigError
from yomawari.detect.engine import Engine
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import Op, irregular_candidates, normal_candidates, state_key
from yomawari.input.pointer import Pointer
from yomawari.profile import GameSpec, build_spec
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState, Target

from .conftest import scaled_rules
from .test_window import FakeWindows

# 描画領域は (0, 0) から 1000x500 ポイント、Retina 2倍
BOUNDS = ScreenRect(0, 0, 1000, 500)
TITLE = {
    "NewGameButton": [0.1, 0.5, 0.3, 0.6],
    "SettingsButton": [0.1, 0.65, 0.3, 0.75],
    "QuitButton": [0.1, 0.8, 0.3, 0.9],
}
RETURN = {"ReturnButton": [0.8, 0.85, 0.95, 0.95]}
# 回想一覧のような画面。押しても反応しないスロットが縦に並び、6つ目より下は描画領域の外。
# 下へスクロール（上へドラッグ・ホイール下・下キー）すると先が出る。閉じるのは Esc だけ
LIST_TITLE = {"ListButton": [0.1, 0.5, 0.3, 0.6]}
LIST_SLOTS = 12
CFG = {
    **FUZZ_DEFAULTS,
    "op_interval_sec": 0.01,
    "settle_sec": 0.02,
    "stuck_modal_sec": 0.2,
    "external_wait_sec": 0.05,
    "exit_min_interval_sec": 3600,
    "hover_wait_sec": 0,
}


def _inside(r, nx, ny):
    return r[0] <= nx <= r[2] and r[1] <= ny <= r[3]


class SimGame:
    """タイトル → ニューゲームで本文、設定はモーダル。bugs で不具合を仕込む。

    stuck_after_rapid: 本文中に5回以上の連打を受けると、以後の本文送りに反応しない
    dead_settings: 設定ボタンは1回目だけ設定を開き、2回目からは押しても何も起きない
    """

    def __init__(self, emit, title=TITLE, bugs=(), list_modal=True, list_at_boot=False):
        self.emit = emit
        self.list_at_boot = list_at_boot
        self.list_modal = list_modal
        self.title = dict(title)
        self.bugs = set(bugs)
        self.screen = "title"
        self.n = 0
        self.done = False
        self.stuck = False
        self.clicked = []
        self.offset = 0.0
        self.scrolled = 0
        self.max_offset = 0.0
        self.list_closed = 0
        self.settings_opened = 0

    def boot(self):
        self.emit("boot", version="sim")
        self.emit("resolution", w=2000, h=1000, mode="Windowed")
        if self.list_at_boot:
            # ゲームが一覧を開く（本文の途中で選ばせる画面など）。ハーネスのボタン操作では開かない
            self.screen = "list"
            self._show_list()
            return
        self._show_title()

    def _show_title(self):
        self.screen = "title"
        self.emit("menu", name="TitleUI", modal=False, buttons=[{"name": k, "rect": r} for k, r in self.title.items()])

    def _show_list(self):
        slots = [{"name": "Slot", "rect": [0.2, 0.1 + 0.2 * i - self.offset, 0.8, 0.25 + 0.2 * i - self.offset]}
                 for i in range(LIST_SLOTS)]
        self.emit("menu", name="ListUI", modal=self.list_modal, buttons=slots)

    def scroll_list(self, step):
        if self.screen != "list":
            return
        self.scrolled += 1
        self.offset = min(max(self.offset + 0.2 * step, 0), 0.2 * (LIST_SLOTS - 4))
        self.max_offset = max(self.max_offset, self.offset)
        self._show_list()

    def key(self, name):
        if self.screen != "list":
            return
        if name == "esc":
            self.screen = "title"
            self.list_closed += 1
            self.emit("menu_close", name="ListUI")
        elif name in ("down", "up"):
            self.scroll_list(1 if name == "down" else -1)

    def wheel(self, amount):
        # pyautogui と同じく、負の値がホイールを下へ（下の項目が出てくる）
        self.scroll_list(1 if amount < 0 else -1)

    def drag(self, dy):
        self.scroll_list(1 if dy < 0 else -1)

    def click(self, nx, ny, button, clicks):
        if button != "left":
            return
        if self.screen == "list":
            return
        if self.screen == "reading":
            if clicks >= 5 and "stuck_after_rapid" in self.bugs:
                self.stuck = True
            if not self.stuck:
                self._advance()
            return
        buttons = RETURN if self.screen == "settings" else self.title
        hit = next((k for k, r in buttons.items() if _inside(r, nx, ny)), None)
        if hit is None:
            return
        self.clicked.append(hit)
        if hit == "NewGameButton":
            self.emit("menu_close", name="TitleUI")
            self.screen = "reading"
            self.emit("scene", name="main/s1")
            self._advance()
        elif hit == "ListButton":
            self.screen = "list"
            self.offset = 0.0
            self._show_list()
        elif hit == "SettingsButton" and not ("dead_settings" in self.bugs and self.settings_opened):
            self.settings_opened += 1
            self.screen = "settings"
            self.emit("menu", name="SettingsUI", modal=True, buttons=[{"name": k, "rect": r} for k, r in RETURN.items()])
        elif hit == "ReturnButton":
            self.emit("menu_close", name="SettingsUI")
            self.screen = "title"

    def _advance(self):
        if self.n and not self.done:
            self.done = True
            self.emit("line_done", line_id=f"main/s1:{self.n}.0")
            return
        self.n += 1
        self.done = False
        self.emit("line", line_id=f"main/s1:{self.n}.0", text=f"line {self.n}")


class SimDriver:
    """Pointer は入力をワーカースレッドで呼ぶ。本物と同じく、ゲームのイベントはイベントループ側で届くようにする。"""

    def __init__(self, game):
        self.game = game
        self.loop = None

    def move(self, x, y): ...
    def click(self, x, y, button="left", clicks=1):
        self.loop.call_soon_threadsafe(self.game.click, x / BOUNDS.w, y / BOUNDS.h, button, clicks)
    def scroll(self, x, y, amount):
        self.loop.call_soon_threadsafe(self.game.wheel, amount)
    def drag(self, x0, y0, x1, y1):
        self.loop.call_soon_threadsafe(self.game.drag, (y1 - y0) / BOUNDS.h)
    def key(self, name):
        self.loop.call_soon_threadsafe(self.game.key, name)
    def hotkey(self, *names): ...


def run_sim(mode, seconds, bugs=(), title=TITLE, guard=None, cfg=CFG, seed="1", book=None, list_modal=True,
            list_at_boot=False):
    rules = scaled_rules(0.05)  # S-01: 0.25秒、S-03: 0.5秒
    engine = Engine(rules)
    state = GameState()
    issues, events, exits = [], [], []
    holder = {}

    def publish(ev):
        events.append(ev)
        state.feed(ev)
        issues.extend(engine.feed(ev))
        if "session" in holder:
            holder["session"].on_event(ev)

    game = SimGame(lambda kind, **d: publish(Event("game", kind, d, 0)), title=title, bugs=bugs, list_modal=list_modal, list_at_boot=list_at_boot)
    run = FuzzRun(
        mode=mode, decider=RandomDecider(random.Random(seed), cfg["no_response_factor"]), guard=Guard(guard or {}, book=book),
        rng=random.Random(seed), cfg=cfg, response_timeout=float(rules["S-01"].param("timeout_sec")),
    )
    driver = SimDriver(game)
    pointer = Pointer(FakeWindows(WindowInfo(1, BOUNDS, True), front=1), driver, 1, state, activate_wait=0)
    holder["session"] = FuzzSession(run, state, pointer, publish, 0, exits.append)

    async def main():
        driver.loop = asyncio.get_running_loop()
        publish(Event("harness", "launch", {"pid": 1}, 0))
        game.boot()
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                publish(Event("game", "heartbeat", {}, 0))
                issues.extend(engine.tick(asyncio.get_running_loop().time(), 0))
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        loop = asyncio.create_task(holder["session"].loop(stop))
        await asyncio.sleep(seconds)
        stop.set()
        await asyncio.gather(hb, loop)

    asyncio.run(main())
    return game, run, issues, events, exits


def test_random_mode_reaches_reading_and_advances():
    game, run, issues, events, _ = run_sim("random", 1.5)
    assert game.n >= 5, game.clicked
    assert not issues, [i.detail for i in issues]
    assert run.stats["normal"] > 0 and run.stats["irregular"] == 0
    assert any(e.kind == "op_result" and e.data["kind"] == "advance" and e.data["responded"] for e in events)
    # 本文送りで新しい行に届いたことを、進行につながった操作として数えている（fuzz/experience.py）
    adv = run.experience.ops["reading"]["advance"]
    assert adv["tries"] > 0 and 0 < adv["progress"] <= adv["tries"]
    assert run.experience.ops["menus:TitleUI"]["click TitleUI/NewGameButton"]["progress"] == 1


def test_fuzz_mode_stuck_after_rapid_click_is_s01_or_s02():
    cfg = {**CFG, "trigger_prob": 1.0, "irregular_prob": 0.3}
    game, run, issues, _, _ = run_sim("fuzz", 3.0, bugs={"stuck_after_rapid"}, cfg=cfg)
    assert game.stuck
    assert run.stats["irregular"] > 0
    ids = {i.rule_id for i in issues}
    assert ids & {"S-01", "S-02"}, ids


def test_dead_button_is_s05():
    title = {k: v for k, v in TITLE.items() if k != "NewGameButton"}
    game, _, issues, _, _ = run_sim("random", 1.5, bugs={"dead_settings"}, title=title,
                                    guard={"expect_exit": ["TitleUI/QuitButton"]})
    s05 = [i for i in issues if i.rule_id == "S-05"]
    assert s05 and "TitleUI/SettingsButton" in s05[0].detail
    assert "QuitButton" not in game.clicked  # exit_min_interval_sec の間は押さない


def test_expect_exit_is_pressed_and_announced():
    cfg = {**CFG, "exit_min_interval_sec": 0, "save_before_exit_sec": 0}  # セーブしてから試すのは test_resume で見る
    title = {"QuitButton": TITLE["QuitButton"]}
    game, run, _, events, exits = run_sim("random", 0.5, title=title, cfg=cfg,
                                          guard={"expect_exit": ["TitleUI/QuitButton"]})
    assert game.clicked == ["QuitButton"]
    assert exits == [True]
    assert [e.kind for e in events if e.kind == "exit_expected"] == ["exit_expected"]
    assert run.stats["expect_exit"] == 1


def test_deny_is_never_pressed():
    title = {"SettingsButton": TITLE["SettingsButton"], "QuitButton": TITLE["QuitButton"]}
    game, *_ = run_sim("fuzz", 1.0, title=title, guard={"deny": ["TitleUI/QuitButton"]},
                       cfg={**CFG, "irregular_prob": 0.5})
    assert "QuitButton" not in game.clicked


# --- 候補と Decider ---


def _title_state():
    s = GameState()
    s.feed(Event("game", "menu", {"name": "TitleUI", "modal": False,
                                  "buttons": [{"name": k, "rect": r} for k, r in TITLE.items()]}, 0))
    return s


def test_candidates_follow_guard():
    s = _title_state()
    g = Guard({"irregular_only": ["TitleUI/NewGameButton"], "expect_exit": ["TitleUI/QuitButton"]})
    normal = {op.label: op for op in normal_candidates(s, g, FUZZ_DEFAULTS["weights"], allow_exit=False)}
    assert set(normal) == {"click TitleUI/SettingsButton"}
    normal = {op.label for op in normal_candidates(s, g, FUZZ_DEFAULTS["weights"], allow_exit=True)}
    assert "expect_exit TitleUI/QuitButton" in normal
    irregular = {op.label for op in irregular_candidates(s, g, 10)}
    assert "click TitleUI/NewGameButton" in irregular
    assert "double_click TitleUI/SettingsButton" in irregular
    assert not any("QuitButton" in l for l in irregular)


def test_reading_state_advances_and_busy_waits():
    s = GameState()
    s.feed(Event("game", "line", {"line_id": "a:1.0"}, 0))
    labels = {op.label for op in normal_candidates(s, Guard({}), FUZZ_DEFAULTS["weights"], False)}
    assert "advance" in labels and state_key(s) == "reading"
    s.feed(Event("game", "transition", {"phase": "start", "name": "fade"}, 0))
    assert normal_candidates(s, Guard({}), FUZZ_DEFAULTS["weights"], False) == []


def test_same_name_buttons_are_distinguished():
    s = GameState()
    slots = [{"name": "Slot", "rect": [0.1 * i, 0.1, 0.1 * i + 0.05, 0.2]} for i in range(3)]
    s.feed(Event("game", "menu", {"name": "SaveLoadUI", "modal": True, "buttons": slots}, 0))
    labels = [t.label for t in s.targets()]
    assert labels == ["SaveLoadUI/Slot", "SaveLoadUI/Slot#1", "SaveLoadUI/Slot#2"]
    assert s.find("SaveLoadUI/Slot", 2).rect[0] == pytest.approx(0.2)


def test_decider_prefers_untried_and_avoids_repeat():
    t = lambda n: Target("button", "M", n, (0, 0, 1, 1))  # noqa: E731
    ops = [Op("click", t("A"), novelty=True), Op("click", t("B"), novelty=True)]
    d = RandomDecider(random.Random(0))
    picks = [d.choose("k", ops).label for _ in range(20)]
    assert all(a != b for a, b in zip(picks, picks[1:]))  # 同じ状態で同じ操作を続けない
    assert {"click M/A", "click M/B"} == set(picks)


# --- 時間で判定する検出器 ---


def test_s03_transition_stuck():
    engine = Engine(scaled_rules(1.0))
    engine.feed(Event("game", "transition", {"phase": "start", "name": "fade"}, 0, t=100.0))
    assert not engine.tick(105.0, 0)
    issues = engine.tick(110.5, 0)
    assert [i.rule_id for i in issues] == ["S-03"] and issues[0].action == "restart"
    engine.feed(Event("game", "transition", {"phase": "start", "name": "fade"}, 0, t=200.0))
    engine.feed(Event("game", "transition", {"phase": "end", "name": "fade"}, 0, t=201.0))
    assert not engine.tick(215.0, 0)


def test_c04_exit_timeout():
    engine = Engine(scaled_rules(1.0))
    engine.feed(Event("harness", "exit_expected", {"target": "TitleUI/QuitButton"}, 0, t=100.0))
    issues = engine.tick(110.5, 0)
    assert [i.rule_id for i in issues] == ["C-04"]
    engine.feed(Event("harness", "exit_expected", {"target": "TitleUI/QuitButton"}, 0, t=200.0))
    engine.feed(Event("proc", "proc_exit", {"code": 0, "by_harness": True}, 0, t=201.0))
    assert not engine.tick(215.0, 0)


def test_s01_needs_live_heartbeat():
    engine = Engine(scaled_rules(1.0))
    base = {"kind": "advance", "label": "advance", "verify": True, "responded": False, "interfered": False,
            "recent": ["rapid_click 10", "advance"], "state": {}}
    assert engine.feed(Event("harness", "op_result", {**base, "heartbeat_age": 30.0}, 0)) == []  # H-01 の担当
    issues = engine.feed(Event("harness", "op_result", {**base, "heartbeat_age": 0.5}, 0))
    assert [i.rule_id for i in issues] == ["S-01"] and "rapid_click 10" in issues[0].detail
    # 待っているあいだにスクリプトのコマンドが進んでいれば、ゲームは止まっていない
    assert engine.feed(Event("harness", "op_result", {**base, "heartbeat_age": 0.5, "commands_ran": True}, 0)) == []


def _verify_press(feed_command: bool):
    """応答確認のボタンを押す。押したあと画面は変わらず、feed_command ならスクリプトのコマンドだけが進む。"""
    from .test_looks import CFG, _Mouse

    st = GameState()
    st.feed(Event("game", "resolution", {"w": 2000, "h": 1000}, 0))
    st.feed(Event("game", "menu", {"name": "Menu", "modal": True,
                                   "buttons": [{"name": "Button", "rect": [0.1, 0.1, 0.3, 0.25]}]}, 0))
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=CFG, response_timeout=0.2)
    events = []
    s = FuzzSession(run, st, Pointer(windows, _Mouse(), 1, st, activate_wait=0), events.append, 0, lambda _: None)

    def click(*a, **k):
        if feed_command:
            st.feed(Event("game", "command", {"name": "wait", "line_id": "main/x:1.0"}, 0))

    s.pointer.driver.click = click
    asyncio.run(s._normal(Op("click", st.targets()[0]), "modal:Menu", True))
    return [e.data for e in events if e.kind == "op_result"][0]


def test_s01_not_raised_while_script_commands_run():
    # run 2026-11-09_2259: 最後の本文のあと、背景の切り替えと待ちだけが続く演出の途中で本文送りが効かず、S-01 として再起動した
    engine = Engine(scaled_rules(1.0))
    res = _verify_press(feed_command=True)
    assert not res["responded"] and res["commands_ran"]
    assert engine.feed(Event("harness", "op_result", {**res, "heartbeat_age": 0.5}, 0)) == []
    res = _verify_press(feed_command=False)
    assert not res["responded"] and not res["commands_ran"]
    assert [i.rule_id for i in engine.feed(Event("harness", "op_result", {**res, "heartbeat_age": 0.5}, 0))] == ["S-01"]


def test_s01_rechecks_with_another_op_before_raising():
    # run 2026-11-12_0959: 応答確認に選んだ操作が、前に反応した画面と同じ名前の画面でも、いまは何も起こさないもの
    # （すでに選ばれているタブ、端までスクロールした一覧のスクロールバー、空きのスロット）で、S-01 の 4 件がすべて誤検知だった。
    # 反応がなければ、前に反応した別の操作でもう一度確かめ、どれにも反応がないときだけ S-01 にする
    from .test_looks import CFG, _Mouse

    st = GameState()
    st.feed(Event("game", "resolution", {"w": 2000, "h": 1000}, 0))
    st.feed(Event("game", "menu", {"name": "Menu", "modal": True, "buttons": [
        {"name": "Tab", "rect": [0.1, 0.1, 0.3, 0.25]}, {"name": "Close", "rect": [0.5, 0.1, 0.7, 0.25]},
        {"name": "Slot", "rect": [0.1, 0.5, 0.3, 0.65]}]}, 0))
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=CFG, response_timeout=0.2, response_checks=2)
    events = []
    s = FuzzSession(run, st, Pointer(windows, _Mouse(), 1, st, activate_wait=0), events.append, 0, lambda _: None)
    s.pointer.driver.click = lambda *a, **k: None  # 押しても画面は変わらない
    key = "modal:Menu"
    tab, close, slot = st.targets()
    run.responsive |= {(key, f"click {t.label}") for t in (tab, close, slot)}
    s._dim = {slot.label}  # 空きと見込んでいるスロットは、前に反応していても使わない
    engine = Engine(scaled_rules(1.0))

    def press(t):
        asyncio.run(s._normal(Op("click", t), key, True))
        res = [e.data for e in events if e.kind == "op_result"][-1]
        return res, engine.feed(Event("harness", "op_result", {**res, "heartbeat_age": 0.5}, 0))

    cands = normal_candidates(st, Guard({}), CFG["weights"], False)
    assert {op.label for op in s._verify_candidates(cands, key)} == {f"click {tab.label}", f"click {close.label}"}
    run.recent.append("rapid_click 10")
    res, issues = press(tab)
    assert res["recheck"] and issues == [] and s._verify_next
    assert (key, f"click {tab.label}") not in run.responsive  # 反応がなかった操作は、この状態では応答確認に使わない
    assert [op.label for op in s._verify_candidates(cands, key)] == [f"click {close.label}"]
    res, issues = press(close)
    assert not res["recheck"] and res["verify_tried"] == [f"click {tab.label}"]
    assert [i.rule_id for i in issues] == ["S-01"]
    assert "「rapid_click 10」のあと" in issues[0].detail and f"click {tab.label} にも反応がありませんでした" in issues[0].detail


# --- 設定 ---


def test_guard_config(tmp_path):
    p = tmp_path / "yomawari.toml"
    p.write_text('[guard.buttons]\nexpect_exit = ["TitleUI/QuitButton"]\n')
    spec = build_spec({}, Config(p).doc.unwrap())
    assert spec.guard_buttons["expect_exit"] == ["TitleUI/QuitButton"]
    assert spec.guard_buttons["deny"] == []
    p.write_text('[guard.buttons]\nnever = ["A/B"]\n')
    with pytest.raises(ConfigError):
        build_spec({}, Config(p).doc.unwrap())
    p.write_text('[fuzz.weights]\nadvance = 5\n')
    assert Config(p).fuzz["weights"] == {**FUZZ_DEFAULTS["weights"], "advance": 5}


def test_s02_counts_advances_until_progress():
    engine = Engine(scaled_rules(1.0))
    adv = {"kind": "advance", "label": "advance", "responded": False, "interfered": False,
           "heartbeat_age": 0.5, "recent": [], "state": {"line_id": "a:1.0"}}
    for _ in range(9):
        assert engine.feed(Event("harness", "op_result", adv, 0)) == []
    engine.feed(Event("game", "choices", {"items": []}, 0))  # 選択肢が消えた知らせは進んだことにしない
    assert [i.rule_id for i in engine.feed(Event("harness", "op_result", adv, 0))] == ["S-02"]
    engine.feed(Event("game", "line", {"line_id": "a:2.0"}, 0))  # 進んだら数え直す
    out = []
    # 進めなくなった原因の操作（回想一覧を Esc で閉じた）は、本文送りが続くと「直前の操作」から押し出される。前の操作として載せる
    first = {**adv, "recent": ["click AlbumUI/Slot#17", "wheel_down 3", "key esc", "advance"]}
    out += engine.feed(Event("harness", "op_result", first, 0))
    for _ in range(9):
        out += engine.feed(Event("harness", "op_result", {**adv, "recent": ["advance"] * 10}, 0))
    assert [i.rule_id for i in out] == ["S-02"]
    assert "10 回" in out[0].detail and "`wheel_down 3` → `key esc`" in out[0].detail


# --- ハーネス全体（モックゲーム） ---


class _WindowedPlatform:
    """FakePlatform に、モックゲームのウィンドウがあるふりをさせる。"""

    def __init__(self, base):
        self.base = base
        self.win = FakeWindows(WindowInfo(0, ScreenRect(0, 0, 1280, 748), True))

    def __getattr__(self, name):
        return getattr(self.base, name)

    def window_info(self, pid):
        return self.win.window_info(pid)

    def frontmost_pid(self):
        return self.win.front

    def activate(self, pid):
        self.win.activate(pid)


def test_harness_random_mode_clicks_mock_game(tmp_path, mock_app, fake_platform):
    from yomawari.config import RUN_DEFAULTS, SENSOR_DEFAULTS, BuildConfig
    from yomawari.platform.mac import MacPlatform
    from yomawari.watchdog import Harness, RunOptions

    from .conftest import RULES_PATH
    from .test_window import RecordingDriver

    fake_platform.save.mkdir(parents=True)
    (fake_platform.save / "slot1.dat").write_text("original")
    build = BuildConfig("mock", mock_app, args=["--mock-hb", "0.05", "--mock-mode", "menus", "--mock-after", "60"])
    exe = MacPlatform().resolve_executable(mock_app)
    drivers = []

    def factory():
        drivers.append(RecordingDriver())
        return drivers[-1]

    sensors = {**SENSOR_DEFAULTS, "log_poll_sec": 0.02, "process_poll_sec": 0.05, "tick_sec": 0.05}
    h = Harness(
        build, exe, _WindowedPlatform(fake_platform), scaled_rules(0.05), RULES_PATH, tmp_path / "runs",
        {**RUN_DEFAULTS, "quit_timeout_sec": 2.0, "max_restarts": 0}, sensors,
        fuzz_cfg=CFG, spec=GameSpec(guard_buttons={"deny": ["TitleUI/NewGameButton"]}), driver_factory=factory,
    )
    r = asyncio.run(h.run(RunOptions(hours=3 / 3600, mode="random", seed="1")))

    # 2560x1440 の描画領域が、1280x748 の外枠の下 720pt に収まる（タイトルバー 28pt）
    assert drivers[0].clicks and all(28 <= y <= 748 for _, y, _, _ in drivers[0].clicks)
    # モックはクリックに反応しない。一度も反応しなかったボタンは S-05 にせず、レポートに一覧で載せる。deny は押さない
    assert not [i for i in r.issues if i.rule_id == "S-05"], [i.detail for i in r.issues]
    report = r.report_path.read_text()
    assert "## 一度も反応しなかったボタン" in report and "`TitleUI/SettingsButton`" in report
    assert "NewGameButton" not in report
    assert "操作: 通常" in report
    assert (fake_platform.save / "slot1.dat").read_text() == "original"


def test_example_toml_matches_fuzz_defaults():
    from .conftest import ROOT

    c = Config(ROOT / "yomawari.example.toml")
    assert c.fuzz == FUZZ_DEFAULTS
    spec = build_spec({}, c.doc.unwrap())
    assert spec.guard_buttons == {"expect_exit": [], "external": [], "irregular_only": [], "deny": []}
    assert spec.guard_roles == GUARD_ROLE_DEFAULTS


def test_choice_label_uses_text():
    s = GameState()
    items = [{"id": "f3a9", "text": "話す", "rect": [0.3, 0.4, 0.7, 0.5]},
             {"id": "0c1d", "text": "話さない", "rect": [0.3, 0.6, 0.7, 0.7]}]
    s.feed(Event("game", "choices", {"items": items}, 0))
    assert [t.label for t in s.targets()] == ["choices/話す", "choices/話さない"]
    assert s.find("choices/0c1d").text == "話さない"


def test_decider_puts_no_response_ops_later():
    t = lambda n: Target("button", "M", n, (0, 0, 1, 1))  # noqa: E731
    dead, alive = Op("click", t("Locked"), novelty=True), Op("click", t("Open"), novelty=True)
    d = RandomDecider(random.Random(0), 0.2)
    d.feedback("k", dead, responded=False)
    d.feedback("k", dead, responded=False)
    assert d.weight("k", dead) == pytest.approx(d.weight("k", alive) * 0.04)
    d.feedback("k", dead, responded=True)  # 反応があれば戻す
    assert d.weight("k", dead) == d.weight("k", alive)


def test_list_screen_scrolls_and_leaves():
    # 押しても反応しないスロットばかりのモーダルでも、スクロールして先を出し、Esc で抜ける。見た目に差がないので、
    # 押して確かめるより先に一覧を最後まで見て回り、見終えてから抜ける
    game, run, issues, events, _ = run_sim("random", 8.0, title=LIST_TITLE)
    assert game.max_offset >= 1.4 - 1e-9  # 台帳を埋めるために、いちばん下のスロットが見えるまで動かした
    assert {"ListUI/Slot"} | {f"ListUI/Slot#{i}" for i in range(1, LIST_SLOTS)} <= set(run.ledger.items)
    assert game.list_closed > 0
    results = [e.data for e in events if e.kind == "op_result"]
    assert any(r["kind"] in ("drag", "wheel_down", "key") and r["responded"] for r in results)
    # 台帳を埋めている間（最後の項目が見えるまで）は、目標と逆の向きへ動かす操作（ドラッグなど）を混ぜない
    def last_visible(e):
        if not (e.source == "game" and e.kind == "menu" and e.data["name"] == "ListUI"):
            return False
        r = e.data["buttons"][-1]["rect"]
        return 0 <= (r[1] + r[3]) / 2 <= 1
    end = next(i for i, e in enumerate(events) if last_visible(e))
    before = [e.data["label"] for e in events[:end] if e.kind == "op"]
    assert not {"drag down", "wheel_up 3", "key up"} & set(before), before
    # 描画領域の外のスロットは押さない
    clicks = [e.data["pos"] for e in events if e.kind == "op" and e.data["kind"] == "click"]
    assert clicks and all(0 <= x <= BOUNDS.w and 0 <= y <= BOUNDS.h for x, y in clicks)


def _list_state(offset=0.0):
    st = GameState()
    st.feed(Event("game", "menu", {"name": "ListUI", "modal": True, "buttons": [
        {"name": "Slot", "rect": [0.2, 0.1 + 0.2 * i - offset, 0.8, 0.25 + 0.2 * i - offset]} for i in range(LIST_SLOTS)
    ]}, 0))
    return st


def test_offscreen_buttons_are_not_targets_and_scrolling_changes_signature():
    st = _list_state()
    assert [t.label for t in st.targets()] == ["ListUI/Slot"] + [f"ListUI/Slot#{i}" for i in range(1, 5)]
    scrolled = _list_state(0.4)
    assert [t.label for t in scrolled.targets()][0] == "ListUI/Slot#2"  # 番号はスクロールしても変わらない
    assert st.signature() != scrolled.signature()
    assert st.top_modal().area == (0.2, 0.1, 0.8, 1.0)  # 描画領域で切り取る


def test_modal_candidates_have_move_and_leave_groups():
    ops = normal_candidates(_list_state(), Guard({}), FUZZ_DEFAULTS["weights"], allow_exit=False)
    groups = {op.group for op in ops}
    assert groups == {"button", "move", "leave"}
    labels = {op.label for op in ops}
    assert {"drag up", "key down", "key enter", "key esc", "right_click", "wheel_down 3"} <= labels
    assert all(op.point is not None for op in ops if op.kind == "drag")


def test_decider_does_not_bury_leave_under_many_buttons():
    st = GameState()
    st.feed(Event("game", "menu", {"name": "ListUI", "modal": True, "buttons": [
        {"name": "Slot", "rect": [0.0, i / 60, 0.1, (i + 0.5) / 60]} for i in range(50)
    ]}, 0))
    ops = normal_candidates(st, Guard({}), FUZZ_DEFAULTS["weights"], allow_exit=False)
    d = RandomDecider(random.Random(0), 0.2)
    picked = []
    for _ in range(30):
        op = d.choose("modal:ListUI", ops)
        d.feedback("modal:ListUI", op, responded=False)
        picked.append(op.group)
    # click 5 : move 2 : leave 1 なので、30回のうち抜ける操作は数回は出る（ボタン1つあたりで分けると 1/50 未満）
    assert picked.count("leave") >= 2, picked
    assert picked.count("move") >= 4, picked


def test_list_screen_is_searched_to_the_end():
    # 見えているスロットが反応しないので、まだ見ていないスロットのある下へ、端まで動かして探す
    game, run, issues, events, _ = run_sim("random", 6.0, title=LIST_TITLE)
    _assert_searched(game, run, events)


def test_non_modal_list_is_searched_to_the_end():
    # モーダルでない一覧（Rachel の回想一覧）には、ランダムに動かす操作（move）は出ない。台帳を埋める操作だけで端まで行く
    game, run, issues, events, _ = run_sim("random", 6.0, title=LIST_TITLE, list_modal=False)
    _assert_searched(game, run, events)
    # 端まで見るまでは、見ていないスロットのある下へだけ動かす（見終わってスロットがどれも反応しなければ、ほかの向きも試す）
    ops = [e.data["label"] for e in events if e.kind == "op"]
    moves = [o for o in ops if o.split()[0] in ("drag", "wheel_down", "wheel_up", "key")]
    assert moves[:3] and all(o in ("drag up", "wheel_down 3", "key down") for o in moves[:3])
    # ハーネスが ListButton で開いた寄り道の画面なので、台帳を埋めたら目標を持たず、ほかの向きへ動かして探し続けない
    assert set(moves) <= {"drag up", "wheel_down 3", "key down"}
    assert any("寄り道" in n and "TitleUI/ListButton" in n for n in run.notes)


def test_list_opened_by_game_is_probed_item_by_item():
    # ゲームが開いた一覧は、押せるものを見つけないと進めない。台帳を埋めても押せそうなものがなければ、未確認の項目を
    # 1つずつ目標にして押して確かめ、どれも押せないと分かったら記録する。矩形の分かる一覧なので、向きを決めずに
    # すべての向きへ動かす探し方（blind）はしない
    game, run, issues, events, _ = run_sim("random", 20.0, list_modal=False, list_at_boot=True)
    labels = {"ListUI/Slot"} | {f"ListUI/Slot#{i}" for i in range(1, LIST_SLOTS)}
    assert labels <= set(run.ledger.items)
    pressed = {e.data["target"] for e in events if e.kind == "op_result" and e.data["kind"] == "click"}
    assert labels <= pressed  # 画面の外の項目も、目標にして押して確かめた
    assert all(run.ledger.items[x].status == "no" for x in labels)
    assert not run.blind_tries
    assert any("ゲームが開いた画面" in n and "見つからなかった" in n for n in run.notes)


def _assert_searched(game, run, events):
    # いちばん下のスロットの中心が描画領域に入るのは、0.2 ずつ動かして 1.4 のとき
    assert game.max_offset >= 1.4 - 1e-9
    assert {f"ListUI/Slot#{i}" for i in range(1, LIST_SLOTS)} <= set(run.ledger.items)
    assert any(e.kind == "op" and e.data["kind"] in ("wheel_down", "drag", "key") for e in events)


def test_reading_with_hud_button_still_advances():
    # 回想中のように、本文を読んでいる途中で操作のボタン（「本編へ戻る」）が出ている。ボタンだけでなく本文送りも候補に入れる
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("scene", name="episodes/m1")
    # 最初は画面の外から滑り込んでくる（hidden）。見えるようになってから本文が来る
    feed("menu", name="EpisodePlaying", modal=False,
         buttons=[{"name": "OpenDialog", "rect": [0, -0.18, 0.23, -0.1], "hidden": True}])
    feed("menu", name="EpisodePlaying", modal=False,
         buttons=[{"name": "OpenDialog", "rect": [0, 0.0, 0.23, 0.08]}])
    assert not st.reading_with_buttons()  # まだ本文が来ていない
    feed("line", line_id="episodes/m1:1", text="a")
    # ボタンの位置が動いて出し直されても、開いている間に本文が進んだ画面のまま
    feed("menu", name="EpisodePlaying", modal=False,
         buttons=[{"name": "OpenDialog", "rect": [0, 0.01, 0.23, 0.09]}])
    assert st.reading_with_buttons()
    # 確認ダイアログを開いている間はボタンが見えなくなり、「引き続き読む」で閉じるとまた見える。本文を読んでいる途中のまま
    # （run 2050 では、閉じたあとのボタンを本文のあとに出たものとみなし、本文を送らずにダイアログを開き直していた）
    feed("menu", name="EpisodePlayingDialogUI", modal=True,
         buttons=[{"name": "ReturnScenarioCancelButton", "rect": [0.4, 0.5, 0.6, 0.6]}])
    feed("menu", name="EpisodePlaying", modal=False, buttons=[])
    assert not st.reading_with_buttons()  # ダイアログ（モーダル）が出ている
    feed("menu_close", name="EpisodePlayingDialogUI")
    feed("menu", name="EpisodePlaying", modal=False,
         buttons=[{"name": "OpenDialog", "rect": [0, 0.01, 0.23, 0.09]}])
    assert st.reading_with_buttons()
    ops = normal_candidates(st, Guard({}), FUZZ_DEFAULTS["weights"], allow_exit=False)
    labels = {op.label for op in ops}
    assert {"advance", "click EpisodePlaying/OpenDialog"} <= labels
    # 本文のあとにボタンのある画面が出た（タイトルに戻った、ボタンで一覧を開いた）ときは、本文を読んでいる途中ではない
    feed("menu", name="TitleUI", modal=False, buttons=[{"name": "NewGameButton", "rect": [0.1, 0.5, 0.3, 0.6]}])
    assert not st.reading_with_buttons()
    assert "advance" not in {op.label for op in normal_candidates(st, Guard({}), FUZZ_DEFAULTS["weights"], False)}
    feed("menu_close", name="TitleUI")
    # 選択肢が出ているときも足さない
    feed("choices", items=[{"id": "c1", "text": "はい", "rect": [0.4, 0.4, 0.6, 0.5]}])
    assert not st.reading_with_buttons()
    # モーダル（右クリックメニュー）が出ているときも足さない
    feed("choices_done")
    assert st.reading_with_buttons()
    feed("menu", name="QuickMenuUI", modal=True, buttons=[{"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2]}])
    assert not st.reading_with_buttons()
    feed("menu_close", name="QuickMenuUI")
    assert st.reading_with_buttons()
    # 閉じて開き直した画面は、また本文が進むまで読んでいる途中とはみなさない
    feed("menu_close", name="EpisodePlaying")
    feed("menu", name="EpisodePlaying", modal=False,
         buttons=[{"name": "OpenDialog", "rect": [0, 0.01, 0.23, 0.09]}])
    assert not st.reading_with_buttons()
    feed("line", line_id="episodes/m1:2", text="b")
    assert st.reading_with_buttons()


def test_fully_covered_modal_is_not_top():
    # ゲームが開いた選択画面（モーダルでない）と同時に右クリックメニュー（モーダル）が開き、選択画面の下に隠れた。
    # 右クリックメニューを手前の画面とせず、選択画面のボタンを押す
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("menu", name="AlbumUI", modal=False, buttons=[{"name": "Slot", "rect": [0.1, 0.1, 0.3, 0.3]}])
    feed("menu", name="QuickMenuUI", modal=True, buttons=[
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "ToLoad", "rect": [0.1, 0.3, 0.2, 0.4], "covered": True},
    ])
    assert st.top_modal() is None
    assert [t.label for t in st.targets()] == ["AlbumUI/Slot"]
    # 一部だけ覆われているモーダルは、手前のまま
    feed("menu", name="QuickMenuUI", modal=True, buttons=[
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "ToLoad", "rect": [0.1, 0.3, 0.2, 0.4]},
    ])
    assert st.top_modal().name == "QuickMenuUI"
    # ボタンのないモーダル（暗転の幕）も手前のまま
    feed("menu", name="BlackoutUI", modal=True, buttons=[])
    assert st.top_modal().name == "BlackoutUI"


def test_newer_screen_over_partly_covered_modal_is_front():
    # 思い出から戻るロードのあと、ゲームが一覧をモーダル・その上の要約をモーダルでないと伝えた（モーダルの印の付け忘れ）。
    # 一覧のボタンの一部が覆われていて、あとから出た要約のボタンは覆われていないので、要約を手前とする
    # （run 2026-11-07_1809: 要約が上に出ているのに、一覧を動かす操作を 30 秒以上続けた）
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("menu", name="AlbumUI", modal=True, buttons=[
        {"name": "AlbumSlot", "rect": [0.1, 0.1, 0.3, 0.3]},
        {"name": "AlbumSlot", "rect": [0.4, 0.4, 0.6, 0.6], "covered": True},
        {"name": "AlbumSlot", "rect": [0.1, 1.2, 0.3, 1.4], "hidden": True},
    ])
    feed("menu", name="EpisodeUI", modal=False, buttons=[])
    assert st.top_modal().name == "AlbumUI"  # 要約のボタンがまだ出ていない
    feed("menu", name="EpisodeUI", modal=False, buttons=[
        {"name": "ReadEpisodeButton", "rect": [0.42, 0.76, 0.61, 0.84]},
        {"name": "CloseEpisodeButton", "rect": [0.18, 0.77, 0.24, 0.82]},
    ])
    assert st.top_modal().name == "EpisodeUI"
    assert [t.label for t in st.targets()] == ["EpisodeUI/ReadEpisodeButton", "EpisodeUI/CloseEpisodeButton"]
    # 一覧が出し直されても（動かした・ボタンが変わった）、出た順番は変わらないので要約が手前のまま
    feed("menu", name="AlbumUI", modal=True, buttons=[
        {"name": "AlbumSlot", "rect": [0.1, 0.1, 0.3, 0.3], "covered": True},
        {"name": "AlbumSlot", "rect": [0.4, 0.4, 0.6, 0.6], "covered": True},
        {"name": "AlbumSlot", "rect": [0.7, 0.1, 0.9, 0.3]},
    ])
    assert st.top_modal().name == "EpisodeUI"
    # 要約を閉じれば一覧が手前
    feed("menu_close", name="EpisodeUI")
    assert st.top_modal().name == "AlbumUI"
    # 一覧のボタンがどれも覆われていなければ、あとから出たモーダルでない画面（文字の枠など）があってもモーダルが手前
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("menu", name="AlbumUI", modal=True, buttons=[{"name": "AlbumSlot", "rect": [0.1, 0.1, 0.3, 0.3]}])
    feed("menu", name="HudUI", modal=False, buttons=[{"name": "Skip", "rect": [0.8, 0.9, 0.9, 0.95]}])
    assert st.top_modal().name == "AlbumUI"
    # あとから出た画面のボタンも覆われているなら、それも上に重なっていない
    feed("menu", name="AlbumUI", modal=True, buttons=[
        {"name": "AlbumSlot", "rect": [0.1, 0.1, 0.3, 0.3], "covered": True},
        {"name": "AlbumSlot", "rect": [0.4, 0.4, 0.6, 0.6]},
    ])
    feed("menu", name="HudUI", modal=False, buttons=[
        {"name": "Skip", "rect": [0.8, 0.9, 0.9, 0.95]},
        {"name": "Auto", "rect": [0.7, 0.9, 0.8, 0.95], "covered": True},
    ])
    assert st.top_modal().name == "AlbumUI"


def test_verify_does_not_use_move_ops():
    # 前に反応があっても、方向キー・Enter・ドラッグは応答確認に使わない（フォーカスで結果が変わる）
    cfg = {**CFG, "trigger_prob": 1.0, "irregular_prob": 0.3}
    _, _, _, events, _ = run_sim("fuzz", 2.0, title=LIST_TITLE, cfg=cfg)
    verified = [e.data for e in events if e.kind == "op_result" and e.data["verify"]]
    assert verified
    assert not any(d["kind"] in ("drag", "wheel_down", "wheel_up") or d["label"].startswith("key ") for d in verified)


def test_button_that_progressed_before_is_not_quiet_after_one_miss():
    # run 2026-11-06_1830: 起動直後に「はじめから」を1回押して反応せず（画面が切り替わる途中など）、押しても反応しない
    # ボタンとして後回しにし、設定やロード画面の開け閉めを 4 分続けた。前のランまでに押して先へ進んだボタンは後回しにしない
    from .test_flow import _session as flow_session
    s, st, run, _ = flow_session()
    run.silent |= {"TitleUI/NewGameButton", "TitleUI/SettingsButton"}
    run.experience.ops.setdefault("menus:TitleUI", {})["click TitleUI/NewGameButton"] = {"tries": 42, "progress": 28, "rewind": 0}
    quiet = s._quiet()
    assert "TitleUI/NewGameButton" not in quiet and "TitleUI/SettingsButton" in quiet
    # 続けて何度も反応しなければ、ほかのボタンと同じに後回しにする（run 2026-11-06_2050）
    run.miss_streak["TitleUI/NewGameButton"] = FUZZ_DEFAULTS["answered_miss_limit"]
    assert "TitleUI/NewGameButton" in s._quiet()


def test_op_events_say_where_the_op_came_from():
    # 行ったり来たりしたときに、どの仕組みがその操作を出し、どの層が重みを変え、どの候補と競り合っていたかを
    # events.jsonl からあとで突き止められるようにする（run 2026-11-07_0849 では、drag left / drag right を交互に
    # 約 110 回出した仕組みを特定できなかった）
    from yomawari.fuzz.ops import Op
    op = Op("click", weight=2.0).scaled("flow", 20.0).scaled("intent", 1.0).scaled("looks", 0.5)
    assert op.weight == 20.0 and op.why == (("flow", 20.0), ("looks", 0.5))
    assert op.to_json()["by"] == "normal" and op.to_json()["why"] == [["flow", 20.0], ["looks", 0.5]]
    assert Op("key", arg="esc", irregular=True).to_json()["by"] == "irregular"
    assert "why" not in Op("advance").to_json()

    d = RandomDecider(random.Random(0))
    cands = [Op("drag", arg="left", weight=3.0, group="move"), Op("drag", arg="right", weight=1.0, group="move"),
             Op("advance", weight=4.0)]
    picked = d.choose("modal:ListUI", cands)
    odds = d.last_odds
    assert odds["n"] == 3 and abs(sum(p for _, p in odds["top"]) - 1.0) < 1e-3
    assert dict(odds["top"])[picked.label] == odds["p"]
    # グループの重みは中のいちばん大きい重み（move は 3、advance は 4）。move の中は 3 : 1
    assert dict(odds["top"])["advance"] == round(4 / 7, 4)
    assert dict(odds["top"])["drag left"] == round(3 / 7 * 3 / 4, 4)

    # 実際のランでも、op イベントに by と odds が出る。一覧の目標へ向かう操作は list で、目標の label を goal に残す
    game, run, issues, events, _ = run_sim("random", 6.0, title=LIST_TITLE, list_modal=False)
    ops = [e.data for e in events if e.kind == "op"]
    assert all("by" in o for o in ops)
    assert any(o["by"] == "list" and o.get("goal", "").startswith("ListUI/Slot") for o in ops)
    assert all("odds" in o for o in ops if not o["irregular"] and o["trigger"] is None)
