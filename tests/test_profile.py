"""ゲームのプロファイル（profile.py）と、役割・locked・type の扱い。"""

from pathlib import Path

import pytest

from yomawari.config import GUARD_ROLE_DEFAULTS, Config, ConfigError
from yomawari.detect.engine import Engine
from yomawari.events import Event
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.ops import irregular_candidates, normal_candidates
from yomawari.profile import apply_rules, build_spec, load_spec, read_profile
from yomawari.state import GameState

from .conftest import scaled_rules

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = {"advance": 20, "right_click": 1, "wheel_up": 1, "click": 5, "move": 2, "leave": 1}


def _state(buttons, name="TitleUI", modal=False):
    st = GameState()
    st.feed(Event("game", "menu", {"name": name, "modal": modal, "buttons": buttons}, 0))
    return st


def test_examples_profiles_are_valid():
    for p in sorted((ROOT / "examples" / "profiles").glob("*.toml")):
        build_spec(read_profile(p), {}, p)


def test_merge_order_and_arrays(tmp_path):
    profile = {
        "guard": {"roles": {"to_title": "normal"}, "buttons": {"deny": ["TitleUI/Shop"]}},
        "screens": {"ListUI": {"weight": 0.5, "no_response_ok": ["Slot"]}},
    }
    local = {
        "guard": {"buttons": {"deny": ["TitleUI/Share"]}},
        "screens": {"ListUI": {"weight": 0.2}},
        "fuzz": {"op_interval_sec": 1},  # プロファイルの対象外の節は無視する
    }
    spec = build_spec(profile, local)
    assert spec.guard_buttons["deny"] == ["TitleUI/Shop", "TitleUI/Share"]  # 配列は足し合わせる
    assert spec.screen_weights == {"ListUI": 0.2}  # yomawari.toml が上書きする
    assert spec.guard_roles == {**GUARD_ROLE_DEFAULTS, "to_title": "normal"}
    assert spec.no_response_ok == {"ListUI": ["Slot"]}


@pytest.mark.parametrize("profile", [
    {"guard": {"roles": {"quit": "expect_exit"}}},
    {"guard": {"roles": {"exit": "never"}}},
    {"guard": {"buttons": {"never": ["A/B"]}}},
    {"screens": {"ListUI": {"scroll": ["drag"]}}},
    {"screens": {"ListUI": {"weight": -1}}},
])
def test_invalid_profile_is_rejected(profile):
    with pytest.raises(ConfigError):
        build_spec(profile, {})


def test_unknown_section_and_profile_path(tmp_path):
    p = tmp_path / "yomawari.profile.toml"
    p.write_text("[fuzz]\nop_interval_sec = 1\n")
    with pytest.raises(ConfigError):
        read_profile(p)
    p.write_text('[screens.ListUI]\nweight = 0.5\n')
    cfg_path = tmp_path / "yomawari.toml"
    cfg_path.write_text('[builds.dev]\npath = "/x/Game.app"\nprofile = "yomawari.profile.toml"\n')
    cfg = Config(cfg_path)
    b = cfg.get_build("dev")
    assert b.profile == tmp_path / "yomawari.profile.toml"  # yomawari.toml からの相対パス
    assert load_spec(cfg, b).screen_weights == {"ListUI": 0.5}


def test_rules_override_and_no_response_ok():
    rules = scaled_rules(1.0)
    spec = build_spec({
        "rules": {"S-05": {"consecutive": 5}},
        "screens": {"ListUI": {"no_response_ok": ["Slot"]}},
    }, {})
    out = apply_rules(rules, spec)
    assert out["S-05"].param("consecutive") == 5
    assert rules["S-05"].param("consecutive") == 3  # 元の rules は変えない
    engine = Engine(out)
    miss = {"kind": "click", "responded": False, "interfered": False, "heartbeat_age": 0.5}
    for _ in range(6):
        assert engine.feed(Event("harness", "op_result", {**miss, "target": "ListUI/Slot#3"}, 0)) == []
    with pytest.raises(ConfigError):
        apply_rules(rules, build_spec({"rules": {"Z-99": {"x": 1}}}, {}))
    with pytest.raises(ConfigError):
        apply_rules(rules, build_spec({"rules": {"S-05": {"nope": 1}}}, {}))


def test_role_decides_guard_kind_and_name_wins():
    st = _state([
        {"name": "Quit", "rect": [0.1, 0.1, 0.2, 0.2], "role": "exit"},
        {"name": "Web", "rect": [0.1, 0.3, 0.2, 0.4], "role": "external"},
        {"name": "ToTitle", "rect": [0.1, 0.5, 0.2, 0.6], "role": "to_title"},
        {"name": "Start", "rect": [0.1, 0.7, 0.2, 0.8], "role": "new_game"},
    ])
    guard = Guard({"deny": ["TitleUI/Web"]})
    kinds = {t.name: guard.kind_of(t) for t in st.targets()}
    assert kinds == {"Quit": "expect_exit", "Web": "deny", "ToTitle": "irregular_only", "Start": None}
    labels = {op.label for op in normal_candidates(st, guard, WEIGHTS, allow_exit=True)}
    assert labels == {"expect_exit TitleUI/Quit", "click TitleUI/Start"}
    roles = {**GUARD_ROLE_DEFAULTS, "to_title": "normal"}
    labels = {op.label for op in normal_candidates(st, Guard({}, roles), WEIGHTS, allow_exit=False)}
    assert "click TitleUI/ToTitle" in labels


def test_locked_buttons_are_irregular_only():
    st = _state([
        {"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2], "locked": True},
        {"name": "Slot", "rect": [0.3, 0.1, 0.4, 0.2]},
    ], name="ListUI", modal=True)
    ops = normal_candidates(st, Guard({}), WEIGHTS, allow_exit=False)
    assert [op.target.label for op in ops if op.target] == ["ListUI/Slot#1"]
    irr = {op.label for op in irregular_candidates(st, Guard({}), 10)}
    assert "click ListUI/Slot" in irr and "double_click ListUI/Slot#1" in irr


def test_all_locked_modal_still_moves_and_leaves():
    st = _state([{"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2], "locked": True}], name="ListUI", modal=True)
    groups = {op.group for op in normal_candidates(st, Guard({}), WEIGHTS, allow_exit=False)}
    assert groups == {"move", "leave"}


def test_screen_weight_scales_buttons():
    st = _state([{"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2]}], name="ListUI")
    ops = normal_candidates(st, Guard({}), WEIGHTS, allow_exit=False, screen_weights={"ListUI": 0.5})
    assert [op.weight for op in ops] == [2.5]


def test_s05_ignores_types():
    engine = Engine(scaled_rules(1.0))
    miss = {"kind": "click", "responded": False, "interfered": False, "heartbeat_age": 0.5,
            "target": "SettingsUI/Lang", "type": "dropdown"}
    for _ in range(5):
        assert engine.feed(Event("harness", "op_result", miss, 0)) == []
    out = []
    for _ in range(3):
        out += engine.feed(Event("harness", "op_result", {**miss, "type": "button"}, 0))
    assert [i.rule_id for i in out] == ["S-05"]
