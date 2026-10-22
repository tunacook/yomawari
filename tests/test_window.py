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


def test_inconsistent_resolution_is_none():
    # 描画領域が外枠より高くなる（resolution が古い、別のウィンドウを見ている等）
    assert client_area(ScreenRect(0, 0, 1280, 300), 2560, 1440) is None
    assert client_area(ScreenRect(0, 0, 0, 0), 2560, 1440) is None


class FakeWindows:
    def __init__(self, info, front=None):
        self.info = info
        self.front = front
        self.activated = []

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
