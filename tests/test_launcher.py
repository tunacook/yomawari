import plistlib
from pathlib import Path

from yomawari.config import BuildConfig
from yomawari.launcher import build_command, validate_build
from yomawari.platform.mac import MacPlatform, macho_archs

plat = MacPlatform()


def test_validate_app(mock_app: Path):
    exe, msgs = validate_build(BuildConfig("m", mock_app), plat)
    assert exe == mock_app / "Contents" / "MacOS" / "MockGame"
    assert any(level == "warn" and "スクリプト" in m for level, m in msgs)


def test_validate_missing_path(tmp_path: Path):
    exe, msgs = validate_build(BuildConfig("m", tmp_path / "Nope.app"), plat)
    assert exe is None and msgs[0][0] == "error"


def test_validate_missing_bundle_executable(mock_app: Path):
    with (mock_app / "Contents" / "Info.plist").open("wb") as fp:
        plistlib.dump({"CFBundleName": "X"}, fp)
    exe, msgs = validate_build(BuildConfig("m", mock_app), plat)
    assert exe is None and "CFBundleExecutable" in msgs[-1][1]


def test_validate_not_executable(mock_app: Path):
    (mock_app / "Contents" / "MacOS" / "MockGame").chmod(0o644)
    exe, msgs = validate_build(BuildConfig("m", mock_app), plat)
    assert exe is None and "実行権限" in msgs[-1][1]


def test_validate_non_macho(tmp_path: Path):
    f = tmp_path / "Game.exe"
    f.write_bytes(b"MZ\x90\x00" + b"\x00" * 60)
    f.chmod(0o755)
    exe, msgs = validate_build(BuildConfig("m", f), plat)
    assert exe is None and "Mach-O" in msgs[0][1]


def test_macho_archs_of_system_binary():
    assert "arm64" in macho_archs(Path("/bin/ls"))


def test_build_command_appends_yomawari_args(tmp_path: Path):
    b = BuildConfig("m", tmp_path / "A.app", args=["-screen-width", "1280"])
    cmd = build_command(Path("/x/A"), b, tmp_path / "player.log")
    assert cmd == ["/x/A", "-screen-width", "1280", "-yomawari", "-logFile", str(tmp_path / "player.log")]


def test_guess_identity(mock_app: Path):
    ident = plat.guess_identity(mock_app)
    assert (ident.company, ident.product, ident.bundle_id) == ("YomawariTest", "MockGame", "com.YomawariTest.MockGame")


def test_real_defaults_export_of_missing_domain(tmp_path: Path):
    """存在しないドメインは False になり、ファイルも残らない（本物の defaults を使う）。"""
    dest = tmp_path / "p.plist"
    assert plat.prefs_export("test.yomawari.pytest.missing-domain", dest) is False
    assert not dest.exists()


def test_real_defaults_roundtrip(tmp_path: Path):
    import subprocess

    domain = "test.yomawari.pytest.roundtrip"
    subprocess.run(["defaults", "write", domain, "volume", "-int", "7"], check=True)
    try:
        dest = tmp_path / "p.plist"
        assert plat.prefs_export(domain, dest) is True
        subprocess.run(["defaults", "write", domain, "volume", "-int", "0"], check=True)
        plat.prefs_replace(domain, dest)
        r = subprocess.run(["defaults", "read", domain, "volume"], capture_output=True, text=True)
        assert r.stdout.strip() == "7"
        plat.prefs_replace(domain, None)
        assert subprocess.run(["defaults", "read", domain], capture_output=True).returncode != 0
    finally:
        subprocess.run(["defaults", "delete", domain], capture_output=True)
        # cfprefsd が空の plist ファイルを残すので消しておく
        (Path.home() / "Library" / "Preferences" / f"{domain}.plist").unlink(missing_ok=True)


def test_save_and_prefs_prefer_bundle_id(tmp_path: Path, monkeypatch):
    """ビルドはバンドルID形式。Company/Product 形式（エディタ側）が存在していても選ばない。"""
    import yomawari.platform.mac as mac
    from yomawari.platform import GameIdentity

    monkeypatch.setattr(mac, "HOME", tmp_path)
    (tmp_path / "Library" / "Application Support" / "My Co" / "Game").mkdir(parents=True)
    ident = GameIdentity("My Co", "Game", "com.My-Co.Game")
    assert plat.default_save_dir(ident) == tmp_path / "Library" / "Application Support" / "com.My-Co.Game"
    assert plat.default_prefs_domain(ident) == "com.My-Co.Game"
    # バンドルIDが無いときだけ Company/Product 形式を使う
    no_bundle = GameIdentity("My Co", "Game", None)
    assert plat.default_save_dir(no_bundle) == tmp_path / "Library" / "Application Support" / "My Co" / "Game"
    assert plat.default_prefs_domain(no_bundle) == "unity.My Co.Game"
