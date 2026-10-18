from __future__ import annotations

import plistlib
import shutil
import sys
from pathlib import Path

import pytest

from yomawari.detect.engine import Rule, load_rules
from yomawari.platform import GameIdentity
from yomawari.platform.mac import MacPlatform

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "rules.yaml"
MOCK_GAME = Path(__file__).resolve().parent / "mock_game.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_app(dest: Path, exe_name: str = "MockGame") -> Path:
    """mock_game.py を起動する .app バンドルを作る。"""
    app = dest / f"{exe_name}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as fp:
        plistlib.dump(
            {
                "CFBundleExecutable": exe_name,
                "CFBundleIdentifier": f"com.YomawariTest.{exe_name}",
                "CFBundleName": exe_name,
            },
            fp,
        )
    exe = macos / exe_name
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{MOCK_GAME}" "$@"\n')
    exe.chmod(0o755)
    return app


class FakePlatform(MacPlatform):
    """保存場所・PlayerPrefs・クラッシュフォルダを一時ディレクトリに差し替えた macOS 実装。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.save = root / "save"
        self.crashes = root / "crashes"
        self.crashes.mkdir(parents=True, exist_ok=True)
        self.prefs: dict[str, bytes] = {}

    def default_save_dir(self, ident: GameIdentity) -> Path | None:
        return self.save

    def default_prefs_domain(self, ident: GameIdentity) -> str | None:
        return f"unity.{ident.company}.{ident.product}"

    def crash_dir(self) -> Path:
        return self.crashes

    def prefs_export(self, domain: str, dest: Path) -> bool:
        if domain not in self.prefs:
            return False
        dest.write_bytes(self.prefs[domain])
        return True

    def prefs_replace(self, domain: str, src: Path | None) -> None:
        self.prefs.pop(domain, None)
        if src is not None:
            self.prefs[domain] = src.read_bytes()

    def open_file(self, path: Path) -> None:
        pass


def scaled_rules(factor: float) -> dict[str, Rule]:
    """リポジトリの rules.yaml の時間系パラメータ（*_sec）だけを factor 倍する。

    E2E を短時間で回すための時間の縮尺で、判定の条件そのものは変えない。
    """
    rules = load_rules(RULES_PATH)
    for r in rules.values():
        r.params = {k: (v * factor if k.endswith("_sec") else v) for k, v in r.params.items()}
    return rules


@pytest.fixture
def mock_app(tmp_path: Path) -> Path:
    return make_app(tmp_path / "builds")


@pytest.fixture
def fake_platform(tmp_path: Path) -> FakePlatform:
    return FakePlatform(tmp_path / "os")


@pytest.fixture
def sample_log() -> Path:
    return FIXTURES / "player_sample.log"


@pytest.fixture
def repo_rules() -> dict[str, Rule]:
    return load_rules(RULES_PATH)


__all__ = ["make_app", "FakePlatform", "scaled_rules", "shutil"]
