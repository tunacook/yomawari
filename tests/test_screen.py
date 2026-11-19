"""ランの画面の前提（--screen。yomawari/screen.py）。"""

import pytest

from yomawari.screen import Screen, ScreenError, parse_screen


def test_parse_screen():
    assert parse_screen("1920x1080") == Screen(1920, 1080)
    assert parse_screen(" 1280 X 720 ") == Screen(1280, 720)
    assert parse_screen("") is None
    for bad in ("1920", "1920x", "fullscreen", "0x720"):
        with pytest.raises(ScreenError):
            parse_screen(bad)


def test_screen_args_replace_the_build_args():
    args = ["-screen-fullscreen", "1", "-screen-width", "1280", "-popupwindow", "-force-metal",
            "-screen-height", "720", "-window-mode", "exclusive"]
    assert Screen(1920, 1080).args(args) == ["-force-metal", "-screen-fullscreen", "0",
                                             "-screen-width", "1920", "-screen-height", "1080"]


def test_fullscreen():
    assert parse_screen("", fullscreen=True) == Screen(None, None, True)
    assert parse_screen("1920x1080", fullscreen=True) == Screen(1920, 1080, True)
    assert str(Screen(None, None, True)) == "フルスクリーン" and str(Screen(1920, 1080, True)) == "1920x1080 のフルスクリーン"
    # 大きさを決めなければ、画面モードの指定だけを付ける
    assert Screen(None, None, True).args(["-screen-width", "1280", "-force-metal"]) == ["-force-metal", "-screen-fullscreen", "1"]
    assert Screen(1920, 1080, True).args([]) == ["-screen-fullscreen", "1", "-screen-width", "1920", "-screen-height", "1080"]
    # 大きさを決めなければ、画面モードだけを比べる
    assert Screen(None, None, True).matches(3024, 1964, "FullScreenWindow")
    assert not Screen(None, None, True).matches(1280, 720, "Windowed")
    assert not Screen(1920, 1080, True).matches(3024, 1964, "FullScreenWindow")


def test_screen_matches_the_resolution_event():
    s = Screen(1920, 1080)
    assert s.matches(1920, 1080, "Windowed")
    assert not s.matches(1280, 720, "Windowed")
    assert not s.matches(1920, 1080, "FullScreenWindow")


def test_screen_buttons_are_never_pressed():
    from yomawari.config import GUARD_ROLE_DEFAULTS
    from yomawari.events import Event
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.ops import irregular_candidates
    from yomawari.fuzz.roles import RoleBook
    from yomawari.state import GameState

    st = GameState()
    st.feed(Event("game", "menu", {"name": "SettingsUI", "buttons": [
        {"name": "Res", "rect": [0.1, 0.1, 0.2, 0.2]}, {"name": "Option_a", "rect": [0.1, 0.3, 0.2, 0.4]}]}, 0))
    book = RoleBook({})
    book.confirm("SettingsUI/Res", "system_setting", "解像度が変わった")

    def pressed(roles):
        return {op.target.name for op in irregular_candidates(st, Guard({}, roles, book), 3) if op.target}

    # ふだんはイレギュラー操作で押す。--screen では押さない
    assert "Res" in pressed(GUARD_ROLE_DEFAULTS)
    assert "Res" not in pressed(Screen.guard_roles(GUARD_ROLE_DEFAULTS))
    assert Guard({}, Screen.guard_roles(GUARD_ROLE_DEFAULTS), book).setting_labels() == set()
