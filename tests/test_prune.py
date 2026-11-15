"""古いランの削除（keep_runs_days）。"""

import json
from datetime import datetime
from pathlib import Path

from typer.testing import CliRunner

from yomawari import cli
from yomawari.runs import prune_runs

NOW = datetime(2026, 11, 15, 12, 0)


def make_run(runs: Path, name: str) -> Path:
    d = runs / name
    (d / "backup").mkdir(parents=True)
    (d / "events.jsonl").write_text("")
    return d


# --- 古いランの削除 ---

def test_prune_removes_old_runs_only(tmp_path):
    runs = tmp_path / "runs"
    for n in ("2026-10-01_0100_dev", "2026-10-01_0100_dev_2", "2026-11-05_0100_dev", "2026-11-14_0100_dev"):
        make_run(runs, n)
    (runs / "_experience").mkdir()
    (runs / "coverage.json").write_text("{}")
    (runs / "2026-01-01_misc").mkdir()  # ランの形でない名前は触らない
    res = prune_runs(runs, 14, NOW)
    assert res.removed == ["2026-10-01_0100_dev", "2026-10-01_0100_dev_2"]
    assert sorted(p.name for p in runs.iterdir()) == [
        "2026-01-01_misc", "2026-11-05_0100_dev", "2026-11-14_0100_dev", "_experience", "coverage.json"]


def test_prune_keeps_newest_per_build_and_pending_restore(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "2026-09-01_0100_dev")
    pending = make_run(runs, "2026-09-02_0100_dev")
    make_run(runs, "2026-09-03_0100_dev")
    make_run(runs, "2026-09-01_0100_mac_release")
    (runs / "restore_pending.json").write_text(json.dumps({"run_dir": str(pending), "backup_dir": str(pending / "backup")}))
    res = prune_runs(runs, 14, NOW)
    assert res.removed == ["2026-09-01_0100_dev"]
    assert [n for n, _ in res.kept] == ["2026-09-01_0100_mac_release", "2026-09-02_0100_dev", "2026-09-03_0100_dev"]
    assert "未復元" in dict(res.kept)["2026-09-02_0100_dev"]


def test_prune_dry_run_and_disabled(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "2026-09-01_0100_dev")
    make_run(runs, "2026-11-01_0100_dev")
    assert prune_runs(runs, 14, NOW, dry_run=True).removed == ["2026-09-01_0100_dev"]
    assert prune_runs(runs, 0, NOW).removed == []
    assert (runs / "2026-09-01_0100_dev").exists()
    assert prune_runs(tmp_path / "none", 14, NOW).removed == []


def test_prune_command(tmp_path):
    conf = tmp_path / "y.toml"
    conf.write_text("[run]\nkeep_runs_days = 30\n")
    runs = tmp_path / "runs"
    make_run(runs, "2020-01-01_0100_dev")
    make_run(runs, "2026-01-01_0100_dev")
    res = CliRunner().invoke(cli.app, ["--config", str(conf), "prune", "--dry-run"])
    assert res.exit_code == 0 and "2020-01-01_0100_dev" in res.output
    assert (runs / "2020-01-01_0100_dev").exists()
    res = CliRunner().invoke(cli.app, ["--config", str(conf), "prune"])
    assert res.exit_code == 0 and not (runs / "2020-01-01_0100_dev").exists()
