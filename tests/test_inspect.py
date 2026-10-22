"""inspect をモックゲームで回す。ウィンドウと入力は偽物に差し替える（実際のマウスは動かさない）。"""

import asyncio

from yomawari.config import RUN_DEFAULTS, SENSOR_DEFAULTS, BuildConfig
from yomawari.inspector import inspect_build
from yomawari.platform.mac import MacPlatform
from yomawari.sensors.window import ScreenRect, WindowInfo

from .test_window import RecordingDriver

SENSORS = {**SENSOR_DEFAULTS, "log_poll_sec": 0.02}


def test_inspect_lists_menus_and_clicks(tmp_path, mock_app, fake_platform):
    fake_platform.window_info = lambda pid: WindowInfo(pid, ScreenRect(100, 50, 1280, 748), True)
    fake_platform.frontmost_pid = lambda: None
    fake_platform.activate = lambda pid: setattr(fake_platform, "frontmost_pid", lambda: pid)
    fake_platform.save.mkdir(parents=True)
    (fake_platform.save / "slot1.dat").write_text("original")

    drv = RecordingDriver()
    changes, clicks = [], []
    build = BuildConfig("mock", mock_app, args=["--mock-mode", "menus", "--mock-after", "0.3", "--mock-hb", "0.05"])
    exe = MacPlatform().resolve_executable(mock_app)
    result = asyncio.run(
        inspect_build(
            exe, build, fake_platform, tmp_path / "runs", 4.0,
            ["SettingsUI/ReturnButton", "Nowhere/Button"], SENSORS, RUN_DEFAULTS["quit_timeout_sec"],
            lambda: drv,
            lambda state, area: changes.append((sorted(state.menus), area)),
            lambda key, pos, error: clicks.append((key, pos, error)),
        )
    )

    assert any(menus == ["TitleUI"] for menus, _ in changes)
    area = next(a for _, a in changes if a is not None)
    assert area.scale == 2 and area.rect == ScreenRect(100, 78, 1280, 720)
    # ReturnButton の中心 (0.875, 0.94) を押す
    assert result.clicked == [("SettingsUI/ReturnButton", (100 + 0.875 * 1280, 78 + 0.94 * 720))]
    assert drv.clicks == [(100 + 0.875 * 1280, 78 + 0.94 * 720, "left", 1)]
    assert result.not_clicked == [("Nowhere/Button", "時間内に画面に出ませんでした")]
    assert (fake_platform.save / "slot1.dat").read_text() == "original"
    assert not (tmp_path / "runs" / "restore_pending.json").exists()
