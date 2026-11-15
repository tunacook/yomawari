"""schedule install / uninstall（launchd）と、古いランの削除（keep_runs_days）。"""

import json
import plistlib
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yomawari import cli
from yomawari.platform.mac import LAUNCHD_LABEL, launchd_plist
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


# --- launchd ---

def test_launchd_plist():
    doc = plistlib.loads(launchd_plist(["/x/yomawari", "run"], 1, 5, Path("/w"), Path("/w/runs/_schedule/l.log"), "/x:/usr/bin"))
    assert doc["Label"] == LAUNCHD_LABEL
    assert doc["ProgramArguments"] == ["/x/yomawari", "run"]
    assert doc["StartCalendarInterval"] == {"Hour": 1, "Minute": 5}
    assert doc["WorkingDirectory"] == "/w" and doc["StandardErrorPath"] == "/w/runs/_schedule/l.log"
    assert doc["EnvironmentVariables"]["PATH"] == "/x:/usr/bin"


@pytest.mark.parametrize("text,want", [("01:00", (1, 0)), ("23:59", (23, 59)), (" 7:30", (7, 30))])
def test_parse_at(text, want):
    assert cli.parse_at(text) == want


@pytest.mark.parametrize("text", ["24:00", "1:5", "0100", "aa:bb", "12:60"])
def test_parse_at_rejects(text):
    with pytest.raises(ValueError):
        cli.parse_at(text)


class FakePlat:
    def __init__(self):
        self.installed = None

    def schedule_install(self, argv, hour, minute, workdir, log):
        self.installed = (argv, hour, minute, workdir, log)
        return Path("/fake/agent.plist")

    def schedule_uninstall(self):
        return Path("/fake/agent.plist") if self.installed else None

    def schedule_info(self):
        return None


def test_schedule_install_command(tmp_path, monkeypatch):
    plat = FakePlat()
    monkeypatch.setattr(cli, "get_platform", lambda: plat)
    conf = tmp_path / "y.toml"
    conf.write_text('default_build = "dev"\n[builds.dev]\npath = "/x/Game.app"\n')
    res = CliRunner().invoke(cli.app, ["--config", str(conf), "schedule", "install", "--at", "02:30", "--hours", "6"])
    assert res.exit_code == 0, res.output
    argv, hour, minute, workdir, log = plat.installed
    assert (hour, minute, workdir) == (2, 30, tmp_path.resolve())
    assert argv[1:] == ["--config", str(conf.resolve()), "run", "--build", "dev", "--hours", "6"]
    assert Path(argv[0]).is_absolute()
    assert log == tmp_path.resolve() / "runs" / "_schedule" / "launchd.log"


def test_schedule_install_errors(tmp_path, monkeypatch):
    plat = FakePlat()
    monkeypatch.setattr(cli, "get_platform", lambda: plat)
    conf = tmp_path / "y.toml"
    conf.write_text('[builds.dev]\npath = "/x/Game.app"\n')
    run = CliRunner().invoke
    assert run(cli.app, ["--config", str(conf), "schedule", "install", "--at", "25:00"]).exit_code == 2
    assert run(cli.app, ["--config", str(conf), "schedule", "install", "--build", "nope"]).exit_code == 2
    # ビルド未登録で標準入力が端末でない（launchd など）なら、対話登録せずに終わる
    assert run(cli.app, ["--config", str(conf), "schedule", "install"]).exit_code == 2
    assert run(cli.app, ["--config", str(conf), "schedule", "install", "-b", "dev", "-p", "nope"]).exit_code == 2
    assert plat.installed is None
