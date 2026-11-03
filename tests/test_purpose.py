"""ランの目的（yomawari/purpose.py）。"""

from pathlib import Path

import pytest
import tomlkit

from yomawari.config import FUZZ_DEFAULTS, RUN_DEFAULTS, Config
from yomawari.events import Issue
from yomawari.post.report import RunSummary, render
from yomawari.purpose import DEFAULT_PURPOSE, PURPOSE_DEFAULTS, PurposeError, purposes, resolve


def test_default_is_stuck_and_every_builtin_resolves():
    assert RUN_DEFAULTS["purpose"] == DEFAULT_PURPOSE == "stuck"
    for name in PURPOSE_DEFAULTS:
        p = resolve(name)
        assert p.description and p.mode in ("fuzz", "random", "aging")
        cfg = p.apply(FUZZ_DEFAULTS)
        assert set(FUZZ_DEFAULTS) <= set(cfg)  # 知らない項目で上書きしていない（綴りの誤りがない）
        assert set(cfg) == set(FUZZ_DEFAULTS)
    # 進行不能を探すときは、抜け出す操作を試さずに記録する
    assert resolve("stuck").apply(FUZZ_DEFAULTS)["recover_ops"] == 0


def test_apply_merges_weights_per_item():
    cfg = resolve("buttons").apply(FUZZ_DEFAULTS)
    assert cfg["weights"]["click"] == 10
    assert cfg["weights"]["advance"] == FUZZ_DEFAULTS["weights"]["advance"]
    assert cfg["learn"] is False
    assert FUZZ_DEFAULTS["weights"]["click"] == 5  # 元の既定値は変えない


def test_toml_overrides_and_adds_purposes(tmp_path: Path):
    path = tmp_path / "yomawari.toml"
    path.write_text(
        """
[run]
purpose = "saveload"

[purpose.stuck]
focus = ["S-02"]
[purpose.stuck.fuzz]
recover_ops = 8

[purpose.buttons.fuzz]
weights = { leave = 3 }

[purpose.saveload]
description = "セーブとロードを繰り返す"
mode = "random"
fuzz = { intent_explore = 1, weights = { right_click = 5 } }
""",
        encoding="utf-8",
    )
    cfg = Config(path)
    found = purposes(cfg.purposes)
    assert found["stuck"].focus == ["S-02"]
    assert found["stuck"].fuzz["recover_ops"] == 8 and found["stuck"].fuzz["learn"] is True  # ほかの項目は残る
    assert found["buttons"].fuzz["weights"] == {"click": 10, "leave": 3}
    saveload = resolve(cfg.run["purpose"], cfg.purposes)
    assert saveload.mode == "random" and saveload.apply(cfg.fuzz)["weights"]["right_click"] == 5


def test_unknown_purpose_and_mode_are_errors():
    with pytest.raises(PurposeError, match="見つかりません"):
        resolve("nothing")
    with pytest.raises(PurposeError, match="mode が不正"):
        purposes(tomlkit.parse('[x]\nmode = "walk"\n').unwrap())


def _issue(rule: str, title: str) -> Issue:
    return Issue(rule_id=rule, severity="major", title=title, detail="d", t=0.0, wall="w", launch=0, key=rule)


def test_report_shows_purpose_and_puts_focus_first(tmp_path: Path):
    summary = RunSummary(
        run_id="r", build_name="b", build_path="p", started="s", ended="e", duration_sec=1, mode="random",
        seed="1", launches=1, restarts=0, interrupted=False, rules_path="rules.yaml",
        purpose="stuck", purpose_description="進行不能を探す", focus=["S-02", "S-03"],
    )
    text = render(summary, [_issue("S-05", "反応しない"), _issue("S-02", "進まない")], tmp_path)
    assert "| 目的 | stuck: 進行不能を探す |" in text
    assert "| 重点的に見るルール | S-02, S-03 |" in text
    assert text.index("[S-02] 進まない（重点）") < text.index("[S-05] 反応しない")
