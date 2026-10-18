from pathlib import Path

import pytest

from yomawari.config import Config, ConfigError, normalize_path_input


@pytest.mark.parametrize(
    "text,expected",
    [
        ("'/Users/me/Builds/My Game.app' ", "/Users/me/Builds/My Game.app"),
        ('"/Users/me/Builds/My Game.app"', "/Users/me/Builds/My Game.app"),
        (r"/Users/me/Builds/My\ Game.app", "/Users/me/Builds/My Game.app"),
        ("/Users/me/Builds/Game.app/", "/Users/me/Builds/Game.app"),
    ],
)
def test_normalize_path_input(text, expected):
    assert normalize_path_input(text) == Path(expected)


def test_normalize_expands_home():
    assert normalize_path_input("~/Builds/A.app") == Path.home() / "Builds" / "A.app"


def test_roundtrip_keeps_comments_and_sets_default(tmp_path):
    p = tmp_path / "yomawari.toml"
    p.write_text('# 手書きのコメント\n[run]\nhours = 2  # 短め\n')
    cfg = Config(p)
    cfg.set_build("mac_dev", Path("/b/Rachel.app"), ["-screen-width", "1280"])
    cfg.set_build("mac_rel", Path("/b/Rachel2.app"))
    cfg.save()
    text = p.read_text()
    assert "# 手書きのコメント" in text and "# 短め" in text
    assert text.index("default_build") < text.index("[run]")
    cfg2 = Config(p)
    assert cfg2.default_build == "mac_dev"
    assert cfg2.get_build("mac_dev").args == ["-screen-width", "1280"]
    assert cfg2.run["hours"] == 2
    assert cfg2.run["max_restarts"] == 20  # 既定値


def test_resolve_order(tmp_path):
    p = tmp_path / "yomawari.toml"
    p.write_text('default_build = "a"\n[builds.a]\npath = "/x/A.app"\n[builds.b]\npath = "/x/B.app"\n')
    cfg = Config(p)
    assert cfg.resolve_build(None, None).name == "a"
    assert cfg.resolve_build("b", None).name == "b"
    tmp = cfg.resolve_build("b", Path("/y/Other Game.app"))
    assert tmp.path == Path("/y/Other Game.app") and not tmp.registered
    with pytest.raises(ConfigError):
        cfg.resolve_build("zzz", None)
    assert Config(tmp_path / "none.toml").resolve_build(None, None) is None


def test_relative_paths_are_from_config_dir(tmp_path):
    p = tmp_path / "conf" / "yomawari.toml"
    p.parent.mkdir()
    p.write_text('runs_dir = "out"\n[builds.a]\npath = "/x/A.app"\nsave_dir = "s"\n')
    cfg = Config(p)
    assert cfg.runs_dir == p.parent / "out"
    assert cfg.rules_path == p.parent / "rules.yaml"
    assert cfg.get_build("a").save_dir == p.parent / "s"
