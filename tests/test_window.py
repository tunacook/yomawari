import random

import pytest

from yomawari.input.pointer import Pointer, PointerError
from yomawari.sensors.window import ScreenRect, WindowInfo, client_area
from yomawari.events import Event
from yomawari.state import GameState


def test_windowed_retina_has_titlebar_on_top():
    # 1280x720 ポイントの描画領域 + 28pt のタイトルバー、Retina（2倍）で 2560x1440 ピクセル
    area = client_area(ScreenRect(100, 50, 1280, 748), 2560, 1440)
    assert area.scale == 2
    assert area.rect == ScreenRect(100, 78, 1280, 720)
    assert area.to_screen(0, 0) == (100, 78)
    assert area.to_screen(1, 1) == (1380, 798)
    assert area.center_of((0.1, 0.5, 0.3, 0.6)) == (100 + 0.2 * 1280, 78 + 0.55 * 720)


def test_borderless_fullscreen_matches_bounds():
    area = client_area(ScreenRect(0, 0, 1512, 982), 3024, 1964)
    assert area.rect == ScreenRect(0, 0, 1512, 982)


def test_fullscreen_is_stretched_over_the_window():
    # ノッチのある画面: ウィンドウはノッチの下（1512x949）なのに、ゲームは画面全体の解像度（3024x1964）を出す。
    # ウィンドウモードの求め方では描画領域が外枠より高くなって押せない。フルスクリーンでは外枠全体に引き伸ばす
    assert client_area(ScreenRect(0, 33, 1512, 949), 3024, 1964) is None
    area = client_area(ScreenRect(0, 33, 1512, 949), 3024, 1964, fullscreen=True)
    assert area.rect == ScreenRect(0, 33, 1512, 949) and area.scale == 2
    # 外部モニタ（3360x1890 ポイント）で 1920x1080: 撮った画面の START の文字（y 146〜239）が、ボタン（y 0.0531〜0.1486）に合った
    area = client_area(ScreenRect(0, 0, 3360, 1890), 1920, 1080, fullscreen=True)
    assert area.to_screen(0, 0.0531)[1] < 146 and area.to_screen(0, 0.1486)[1] > 239


def test_inconsistent_resolution_is_none():
    # 描画領域が外枠より高くなる（resolution が古い、別のウィンドウを見ている等）
    assert client_area(ScreenRect(0, 0, 1280, 300), 2560, 1440) is None
    assert client_area(ScreenRect(0, 0, 0, 0), 2560, 1440) is None


class FakeWindows:
    def __init__(self, info, front=None, shot=None):
        self.info = info
        self.front = front
        self.activated = []
        self.shot = shot  # capture が返す Shot（None なら撮れない）
        self.texts = []  # read_text が返す、画面から読んだ文字 [(文字, 確からしさ, 矩形)]

    def read_text(self, shot):
        return list(self.texts)

    def capture(self, rect):
        return self.shot

    def window_info(self, pid):
        return self.info

    def frontmost_pid(self):
        return self.front

    def activate(self, pid):
        self.activated.append(pid)
        self.front = pid


class RecordingDriver:
    def __init__(self):
        self.clicks = []

    def move(self, x, y): ...
    def click(self, x, y, button="left", clicks=1):
        self.clicks.append((x, y, button, clicks))
    def scroll(self, x, y, amount): ...
    def key(self, name): ...
    def hotkey(self, *names): ...


def title_state():
    s = GameState()
    s.feed(Event("game", "resolution", {"w": 2560, "h": 1440, "mode": "Windowed"}))
    s.feed(Event("game", "menu", {"name": "TitleUI", "modal": False,
                                   "buttons": [{"name": "NewGameButton", "rect": [0.1, 0.5, 0.3, 0.6]}]}))
    return s


def test_pointer_activates_and_clicks_center():
    s = title_state()
    win = FakeWindows(WindowInfo(42, ScreenRect(100, 50, 1280, 748), True), front=999)
    drv = RecordingDriver()
    p = Pointer(win, drv, 42, s, activate_wait=0)
    pos = p.click(s.find("TitleUI/NewGameButton"))
    assert win.activated == [42]
    assert drv.clicks == [(pos[0], pos[1], "left", 1)]
    assert pos == (100 + 0.2 * 1280, 78 + 0.55 * 720)


def test_pointer_refuses_when_target_gone_or_window_hidden():
    s = title_state()
    target = s.find("TitleUI/NewGameButton")
    drv = RecordingDriver()
    s.feed(Event("game", "menu_close", {"name": "TitleUI"}))
    p = Pointer(FakeWindows(WindowInfo(42, ScreenRect(0, 0, 1280, 748), True), front=42), drv, 42, s)
    with pytest.raises(PointerError, match="消えました"):
        p.click(target)

    s = title_state()
    p = Pointer(FakeWindows(WindowInfo(42, ScreenRect(0, 0, 1280, 748), False), front=42), drv, 42, s)
    with pytest.raises(PointerError, match="画面に出ていません"):
        p.click(s.find("TitleUI/NewGameButton"))
    assert drv.clicks == []


def test_pointer_needs_resolution():
    s = GameState()
    p = Pointer(FakeWindows(WindowInfo(1, ScreenRect(0, 0, 10, 10), True), front=1), RecordingDriver(), 1, s)
    with pytest.raises(PointerError, match="resolution"):
        p.click_at(0.5, 0.5)


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS のみ")
def test_mac_window_info_prefers_onscreen_window(monkeypatch):
    # Rachel の実機: 本体 640x388 のほかに、画面外の 500x500 とメニューバー大の隠れたウィンドウがある
    import Quartz

    from yomawari.platform.mac import MacPlatform

    def win(x, y, w, h, onscreen):
        d = {Quartz.kCGWindowOwnerPID: 7, Quartz.kCGWindowLayer: 0,
             Quartz.kCGWindowBounds: {"X": x, "Y": y, "Width": w, "Height": h}}
        if onscreen is not None:
            d[Quartz.kCGWindowIsOnscreen] = onscreen
        return d

    infos = [win(0, 0, 3360, 30, None), win(0, 1390, 500, 500, None), win(552, 414, 640, 388, True)]
    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", lambda *a: infos)
    info = MacPlatform().window_info(7)
    assert info.bounds == ScreenRect(552, 414, 640, 388) and info.on_screen


def test_parse_lsappinfo_pid():
    from yomawari.platform.mac import parse_lsappinfo_pid

    assert parse_lsappinfo_pid('"pid"=91521\n') == 91521
    # macOS 27 の形（実機の出力）
    out = ("[ NULL ]  [ NULL ]  \n    bundleID=[ NULL ] \n    bundle path=[ NULL ] \n    executable path=[ NULL ] \n"
           "    pid = 3880 !cgsConnection !signalled type=[ NULL ]  flavor=[ NULL ]  Version=[ NULL ]  Arch=!!none \n\n")
    assert parse_lsappinfo_pid(out) == 3880
    assert parse_lsappinfo_pid("") is None
    assert parse_lsappinfo_pid("parentpid = 12\n") is None


def test_parse_lsappinfo_app():
    from yomawari.platform.mac import parse_lsappinfo_app

    # macOS 27 の形（実機の出力）
    out = ('"メモ" ASN:0x0-0x56a269d: (in front) \n    bundleID="com.apple.Notes"\n    bundle path=[ NULL ] \n'
           "    executable path=[ NULL ] \n    pid = 36854 !cgsConnection !signalled type=[ NULL ]  \n")
    assert parse_lsappinfo_app(out) == "メモ（com.apple.Notes）"
    # macOS 26 の形
    assert parse_lsappinfo_app('"LSDisplayName"="メモ"\n"CFBundleIdentifier"="com.apple.Notes"\n"pid"=1\n') \
        == "メモ（com.apple.Notes）"
    # 名前がない
    out = '[ NULL ]  ASN:0x0-0x1: (in front) \n    bundleID="com.apple.Notes"\n'
    assert parse_lsappinfo_app(out) == "com.apple.Notes"
    assert parse_lsappinfo_app("") is None


def test_unknown_frontmost_app_is_not_reported_as_cannot_activate():
    # 前面のアプリを OS から取得できないときは、押さず、「前面にできない」とも言わない（ゲームは前面にあるかもしれない）
    import pytest

    from yomawari.input.pointer import InputBlocked, Pointer
    from yomawari.state import GameState

    class NoFront(FakeWindows):
        def activate(self, pid):
            self.activated.append(pid)  # 前面にしても、前面のアプリは分からないまま

    p = Pointer(NoFront(WindowInfo(1, ScreenRect(0, 0, 100, 100), True), front=None), None, 1, GameState(), activate_wait=0)
    with pytest.raises(InputBlocked, match="取得できません"):
        p.ensure_front()


def test_locked_screen_blocks_input_and_is_noted():
    # 画面がロックされていると、OS が入力を受け付けない。前面にしようとせず InputBlocked にし、続いた時間を記録に書く
    import pytest

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun
    from yomawari.input.pointer import InputBlocked, Pointer
    from yomawari.state import GameState

    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=2)
    windows.screen_locked = lambda: True
    p = Pointer(windows, None, 1, GameState(), activate_wait=0)
    with pytest.raises(InputBlocked, match="ロック"):
        p.ensure_front()
    assert windows.activated == []
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1)), guard=Guard({}),
                  rng=random.Random(1), cfg=dict(FUZZ_DEFAULTS), response_timeout=5)
    assert run.block("画面がロックされています", 1000.0)
    assert not run.block("画面がロックされています", 1005.0)  # 続いているあいだは1回と数える
    assert run.unblock(1000.0 + 3 * 3600) == ("画面がロックされています", 3 * 3600)
    run.block("ゲームを前面にできません", 20000.0)
    run.unblock(20003.0)  # すぐ戻ったもの（外部を開く操作のあとなど）は載せない
    notes = run.blocked_notes(30000.0)
    assert len(notes) == 1 and "計 3 時間 0 分" in notes[0] and "1 回" in notes[0]


class DragWindows(FakeWindows):
    """visible_area を持ち、タイトルバーのドラッグでウィンドウが動くもの。moves=False なら OS が動かさない。"""

    def __init__(self, info, visible, moves=True):
        super().__init__(info, front=info.pid)
        self.visible = visible
        self.moves = moves

    def visible_area(self, rect):
        return self.visible


class DragDriver(RecordingDriver):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.drags = []

    def drag(self, x0, y0, x1, y1):
        self.drags.append((x0, y0, x1, y1))
        b = self.win.info.bounds
        if self.win.moves and b.y <= y0 <= b.y + 28:
            self.win.info = WindowInfo(self.win.info.pid, ScreenRect(b.x + x1 - x0, b.y + y1 - y0, b.w, b.h), True)


def _resized_state():
    s = GameState()
    s.feed(Event("game", "resolution", {"w": 1600, "h": 900, "mode": "Windowed"}))
    s.feed(Event("game", "menu", {"name": "CardUI", "modal": True,
                                   "buttons": [{"name": "AnswerButton", "rect": [0.3, 0.84, 0.93, 0.93]}]}))
    return s


def test_window_out_of_display_is_dragged_back_by_title_bar():
    # run 2026-11-14_2214: 画面の右下に置いたウィンドウを、ゲームの設定で 1600x900 にした。左上を動かさずに広がり、
    # 下のボタンが画面の外に出て、押しても届かなかった
    visible = ScreenRect(0, 33, 1512, 949)
    win = DragWindows(WindowInfo(7, ScreenRect(792, 557, 800, 478), True), visible)
    drv = DragDriver(win)
    p = Pointer(win, drv, 7, _resized_state(), activate_wait=0, fit_wait=0)
    x, y = p.click(p.state.find("CardUI/AnswerButton"))
    assert len(drv.drags) == 1
    x0, y0, x1, y1 = drv.drags[0]
    assert 557 <= y0 <= 585 and x0 >= 792 + 80  # 閉じるボタンなどを避けて、タイトルバーをつかむ
    assert win.info.bounds == ScreenRect(712, 504, 800, 478)  # 右と下を見える範囲に揃えた
    assert x <= 1512 and y <= 982 and drv.clicks == [(x, y, "left", 1)]
    assert len(p.fits) == 1 and p.fits[0].fitted
    # 収まっていれば、もう動かさない
    p.click(p.state.find("CardUI/AnswerButton"))
    assert len(drv.drags) == 1


def test_point_outside_display_is_not_pressed_when_window_cannot_move():
    visible = ScreenRect(0, 33, 1512, 949)
    win = DragWindows(WindowInfo(7, ScreenRect(792, 557, 800, 478), True), visible, moves=False)
    drv = DragDriver(win)
    p = Pointer(win, drv, 7, _resized_state(), activate_wait=0, fit_wait=0, fit_tries=2)
    for _ in range(4):
        with pytest.raises(PointerError, match="見える範囲の外"):
            p.click(p.state.find("CardUI/AnswerButton"))
    assert drv.clicks == []
    assert len(drv.drags) == 2  # 同じ大きさでは fit_tries 回まで
    assert [f.fitted for f in p.fits] == [False, False]
    # 見える範囲の中の点は押す
    p.click_at(0.1, 0.1)
    assert len(drv.clicks) == 1


def test_fullscreen_presses_under_the_menu_bar_without_dragging():
    # フルスクリーンではメニューバーが隠れていて、画面の上端までゲーム。見える範囲（メニューバーの下）で断らず、動かさない
    s = GameState()
    s.feed(Event("game", "resolution", {"w": 3024, "h": 1964, "mode": "FullScreenWindow"}))
    win = DragWindows(WindowInfo(7, ScreenRect(0, 0, 1512, 982), True), ScreenRect(0, 33, 1512, 949))
    drv = DragDriver(win)
    p = Pointer(win, drv, 7, s, activate_wait=0, fit_wait=0)
    assert p.click_at(0.5, 0.01) == (756, 0.01 * 982)
    assert drv.drags == [] and p.fits == [] and len(drv.clicks) == 1


def test_window_without_visible_area_is_left_as_is():
    win = FakeWindows(WindowInfo(42, ScreenRect(5000, 5000, 1280, 748), True), front=42)
    drv = RecordingDriver()
    p = Pointer(win, drv, 42, title_state(), activate_wait=0)
    p.click(p.state.find("TitleUI/NewGameButton"))
    assert len(drv.clicks) == 1 and p.fits == []
