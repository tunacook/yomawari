"""ファジング（fuzz/）と S 系・C-04 の検出器。

ゲームの代わりに、同じプロセス内の SimGame がクリックを受けてイベントを返す。
OS の入力とウィンドウは、tests/test_window.py と同じ作りのフェイクに差し替える。
"""

import asyncio
import random

import pytest

from yomawari.config import FUZZ_DEFAULTS, Config, ConfigError
from yomawari.detect.engine import Engine
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import Op, irregular_candidates, normal_candidates, state_key
from yomawari.input.pointer import Pointer
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
}


def _inside(r, nx, ny):
    return r[0] <= nx <= r[2] and r[1] <= ny <= r[3]


class SimGame:
    """タイトル → ニューゲームで本文、設定はモーダル。bugs で不具合を仕込む。

    stuck_after_rapid: 本文中に5回以上の連打を受けると、以後の本文送りに反応しない
    dead_settings: 設定ボタンを押しても何も起きない
    """

    def __init__(self, emit, title=TITLE, bugs=()):
        self.emit = emit
        self.title = dict(title)
        self.bugs = set(bugs)
        self.screen = "title"
        self.n = 0
        self.done = False
        self.stuck = False
        self.clicked = []
        self.offset = 0.0
        self.scrolled = 0
        self.list_closed = 0

    def boot(self):
        self.emit("boot", version="sim")
        self.emit("resolution", w=2000, h=1000, mode="Windowed")
        self._show_title()

    def _show_title(self):
        self.screen = "title"
        self.emit("menu", name="TitleUI", modal=False, buttons=[{"name": k, "rect": r} for k, r in self.title.items()])

    def _show_list(self):
        slots = [{"name": "Slot", "rect": [0.2, 0.1 + 0.2 * i - self.offset, 0.8, 0.25 + 0.2 * i - self.offset]}
                 for i in range(LIST_SLOTS)]
        self.emit("menu", name="ListUI", modal=True, buttons=slots)

    def scroll_list(self, step):
        if self.screen != "list":
            return
        self.scrolled += 1
        self.offset = min(max(self.offset + 0.2 * step, 0), 0.2 * (LIST_SLOTS - 4))
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
        self.scroll_list(-1 if amount < 0 else 1)

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
        elif hit == "SettingsButton" and "dead_settings" not in self.bugs:
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


def run_sim(mode, seconds, bugs=(), title=TITLE, guard=None, cfg=CFG, seed="1"):
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

    game = SimGame(lambda kind, **d: publish(Event("game", kind, d, 0)), title=title, bugs=bugs)
    run = FuzzRun(
        mode=mode, decider=RandomDecider(random.Random(seed), cfg["no_response_factor"]), guard=Guard(guard or {}),
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
    cfg = {**CFG, "exit_min_interval_sec": 0}
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


# --- 設定 ---


def test_guard_config(tmp_path):
    p = tmp_path / "yomawari.toml"
    p.write_text('[guard.buttons]\nexpect_exit = ["TitleUI/QuitButton"]\n')
    assert Config(p).guard_buttons["expect_exit"] == ["TitleUI/QuitButton"]
    assert Config(p).guard_buttons["deny"] == []
    p.write_text('[guard.buttons]\nnever = ["A/B"]\n')
    with pytest.raises(ConfigError):
        Config(p).guard_buttons
    p.write_text('[fuzz.weights]\nadvance = 5\n')
    assert Config(p).fuzz["weights"] == {**FUZZ_DEFAULTS["weights"], "advance": 5}


def test_s02_counts_advances_until_progress():
    engine = Engine(scaled_rules(1.0))
    adv = {"kind": "advance", "label": "advance", "responded": False, "interfered": False,
           "heartbeat_age": 0.5, "recent": [], "state": {"line_id": "a:1.0"}}
    for _ in range(19):
        assert engine.feed(Event("harness", "op_result", adv, 0)) == []
    engine.feed(Event("game", "line", {"line_id": "a:2.0"}, 0))  # 進んだら数え直す
    out = []
    for _ in range(20):
        out += engine.feed(Event("harness", "op_result", adv, 0))
    assert [i.rule_id for i in out] == ["S-02"]


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
        fuzz_cfg=CFG, guard_buttons={"deny": ["TitleUI/NewGameButton"]}, driver_factory=factory,
    )
    r = asyncio.run(h.run(RunOptions(hours=3 / 3600, mode="random", seed="1")))

    # 2560x1440 の描画領域が、1280x748 の外枠の下 720pt に収まる（タイトルバー 28pt）
    assert drivers[0].clicks and all(28 <= y <= 748 for _, y, _, _ in drivers[0].clicks)
    # モックはクリックに反応しないので、押したボタンが S-05 になる。deny は押さない
    s05 = [i.detail for i in r.issues if i.rule_id == "S-05"]
    assert any("TitleUI/SettingsButton" in d for d in s05), [i.detail for i in r.issues]
    assert not any("NewGameButton" in d for d in s05)
    assert "操作: 通常" in r.report_path.read_text()
    assert (fake_platform.save / "slot1.dat").read_text() == "original"


def test_example_toml_matches_fuzz_defaults():
    from .conftest import ROOT

    c = Config(ROOT / "yomawari.example.toml")
    assert c.fuzz == FUZZ_DEFAULTS
    assert c.guard_buttons == {"expect_exit": [], "external": [], "irregular_only": [], "deny": []}


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
    # 押しても反応しないスロットばかりのモーダルでも、スクロールして先を出し、Esc で抜ける
    game, run, issues, events, _ = run_sim("random", 2.0, title=LIST_TITLE)
    assert game.scrolled > 0
    assert game.list_closed > 0
    results = [e.data for e in events if e.kind == "op_result"]
    assert any(r["kind"] in ("drag", "wheel_down", "key") and r["responded"] for r in results)
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
