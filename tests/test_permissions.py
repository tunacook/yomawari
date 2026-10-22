import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS のみ")


def test_app_bundle_of():
    from yomawari.platform.mac import app_bundle_of

    assert app_bundle_of("/Applications/iTerm.app/Contents/MacOS/iTerm2") == "/Applications/iTerm.app"
    # ネストした Helper.app は外側のアプリにまとめる
    assert app_bundle_of(
        "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper"
    ) == "/Applications/Visual Studio Code.app"
    assert app_bundle_of("/bin/zsh") is None


def test_permission_help_names_host(monkeypatch):
    from yomawari.platform.mac import MacPlatform

    plat = MacPlatform()
    monkeypatch.setattr(plat, "permission_host", lambda: "/Applications/iTerm.app")
    lines = plat.permission_help(["アクセシビリティ"])
    assert "「iTerm」" in lines[0]
    assert any("アクセシビリティ を開き" in l for l in lines)
    assert not any("画面収録" in l for l in lines)
