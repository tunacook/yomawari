"""モックゲームを実際に起動して、ラン全体（起動・監視・再起動・復元・レポート）を確かめる。

時間は rules.yaml の *_sec を 1/20 に縮めて回す（判定条件は変えない）。
"""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yomawari import cli
from yomawari.config import RUN_DEFAULTS, SENSOR_DEFAULTS, BuildConfig
from yomawari.platform.mac import MacPlatform
from yomawari.watchdog import Harness, RunOptions

from .conftest import RULES_PATH, scaled_rules

SCALE = 0.05  # C-03: 1.5秒、H-01: 0.5秒、C-02 待ち: 0.25秒
SENSORS = {**SENSOR_DEFAULTS, "log_poll_sec": 0.02, "process_poll_sec": 0.05, "crash_poll_sec": 0.05, "tick_sec": 0.05}
RUN = {**RUN_DEFAULTS, "quit_timeout_sec": 2.0, "max_restarts": 1}


def run_harness(tmp_path, mock_app, fake_platform, args, seconds, run_cfg=RUN):
    fake_platform.save.mkdir(parents=True)
    (fake_platform.save / "slot1.dat").write_text("original")
    build = BuildConfig("mock", mock_app, args=["--mock-hb", "0.05", *args])
    exe = MacPlatform().resolve_executable(mock_app)
    h = Harness(build, exe, fake_platform, scaled_rules(SCALE), RULES_PATH, tmp_path / "runs", run_cfg, SENSORS)
    return asyncio.run(h.run(RunOptions(hours=seconds / 3600, mode="aging", seed="1")))


def events(result):
    return [json.loads(l) for l in (result.run_dir / "events.jsonl").read_text().splitlines()]


def assert_restored(fake_platform, tmp_path):
    assert (fake_platform.save / "slot1.dat").read_text() == "original"
    assert not (tmp_path / "runs" / "restore_pending.json").exists()


def test_normal_run_has_no_issues(tmp_path, mock_app, fake_platform):
    r = run_harness(tmp_path, mock_app, fake_platform, [], 1.5)
    assert r.issues == [] and r.exit_code == 0
    evs = events(r)
    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "launch" and "boot" in kinds and kinds.count("heartbeat") > 5
    assert evs[-1]["kind"] == "proc_exit" and evs[-1]["data"]["by_harness"] is True
    assert "-yomawari" in evs[0]["data"]["cmd"]
    report = r.report_path.read_text()
    assert "## Critical\n\nなし" in report and "起動回数 / 再起動回数 | 1 / 0" in report
    assert_restored(fake_platform, tmp_path)


def test_crash_is_c01_and_restarts(tmp_path, mock_app, fake_platform):
    r = run_harness(tmp_path, mock_app, fake_platform, ["--mock-mode", "crash", "--mock-after", "0.3"], 4)
    c01 = [i for i in r.issues if i.rule_id == "C-01"]
    assert len(c01) == 2  # 最初の起動と再起動後（max_restarts=1 で打ち切り）
    assert c01[0].evidence["exit_code"] == 3
    assert r.exit_code == 1
    assert (r.run_dir / "player.log").exists() and (r.run_dir / "player.1.log").exists()
    report = r.report_path.read_text()
    assert "[C-01]" in report and "発生回数: 2" in report and "打ち切り" in report
    assert_restored(fake_platform, tmp_path)


def test_crash_dump_is_c02(tmp_path, mock_app, fake_platform):
    # ゲームが落ちる前後にダンプが書かれた状況を作る
    async def plant():
        await asyncio.sleep(0.4)
        (fake_platform.crashes / "MockGame-2026-10-18-010203.ips").write_text("{}")
        (fake_platform.crashes / "OtherApp-2026-10-18-010203.ips").write_text("{}")

    fake_platform.save.mkdir(parents=True)
    build = BuildConfig("mock", mock_app, args=["--mock-hb", "0.05", "--mock-mode", "crash", "--mock-after", "0.3"])
    exe = MacPlatform().resolve_executable(mock_app)
    h = Harness(build, exe, fake_platform, scaled_rules(SCALE), RULES_PATH, tmp_path / "runs",
                {**RUN, "max_restarts": 0}, SENSORS)

    async def both():
        t = asyncio.create_task(plant())
        res = await h.run(RunOptions(hours=3 / 3600, mode="aging", seed="1"))
        await t
        return res

    r = asyncio.run(both())
    c02 = [i for i in r.issues if i.rule_id == "C-02"]
    assert len(c02) == 1 and c02[0].evidence["dump"].endswith("MockGame-2026-10-18-010203.ips")


def test_hang_is_h01(tmp_path, mock_app, fake_platform):
    r = run_harness(tmp_path, mock_app, fake_platform, ["--mock-mode", "hang", "--mock-after", "0.3"], 3)
    assert "H-01" in [i.rule_id for i in r.issues]
    assert "C-01" not in [i.rule_id for i in r.issues]  # ハーネスが止めたので異常終了ではない
    assert r.exit_code == 1
    assert_restored(fake_platform, tmp_path)


def test_no_boot_is_c03(tmp_path, mock_app, fake_platform):
    r = run_harness(tmp_path, mock_app, fake_platform, ["--mock-mode", "noboot"], 2.5, {**RUN, "max_restarts": 0})
    assert [i.rule_id for i in r.issues] == ["C-03"]


def test_exception_is_l01(tmp_path, mock_app, fake_platform):
    r = run_harness(tmp_path, mock_app, fake_platform, ["--mock-mode", "exception"], 1.5)
    l01 = [i for i in r.issues if i.rule_id == "L-01"]
    assert len(l01) == 1 and l01[0].severity == "major"
    assert r.exit_code == 0  # Major のみ
    assert "Runner.cs:42" in r.report_path.read_text()


def test_pending_restore_from_previous_run(tmp_path, mock_app, fake_platform):
    """前回が異常終了してマーカーが残っていたら、次のランの最初に復元する。"""
    from yomawari.savestate import SaveState, SaveTarget

    fake_platform.save.mkdir(parents=True)
    (fake_platform.save / "slot1.dat").write_text("original")
    SaveState(tmp_path / "runs", fake_platform).backup(tmp_path / "runs" / "old", SaveTarget(fake_platform.save, None), None)
    (fake_platform.save / "slot1.dat").write_text("broken by crashed harness")
    build = BuildConfig("mock", mock_app, args=["--mock-hb", "0.05"])
    exe = MacPlatform().resolve_executable(mock_app)
    h = Harness(build, exe, fake_platform, scaled_rules(SCALE), RULES_PATH, tmp_path / "runs", RUN, SENSORS)
    r = asyncio.run(h.run(RunOptions(hours=0.5 / 3600, mode="aging", seed="1")))
    assert (fake_platform.save / "slot1.dat").read_text() == "original"
    assert "未復元" in r.report_path.read_text()


# --- CLI ---


def write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "yomawari.toml"
    (tmp_path / "rules.yaml").write_text(RULES_PATH.read_text())
    p.write_text(body)
    return p


def test_cli_run_without_build_and_no_tty_exits_2(tmp_path):
    cfg = write_config(tmp_path, "")
    res = CliRunner().invoke(cli.app, ["--config", str(cfg), "run", "--no-open"])
    assert res.exit_code == 2
    assert "端末ではありません" in res.output


def test_cli_run_and_builds(tmp_path, mock_app, monkeypatch, fake_platform):
    monkeypatch.setattr(cli, "get_platform", lambda: fake_platform)
    cfg = write_config(tmp_path, "[run]\nopen_report = false\n")
    runner = CliRunner()
    res = runner.invoke(cli.app, ["--config", str(cfg), "builds", "add", "mock", str(mock_app), "--no-launch"])
    assert res.exit_code == 0, res.output
    assert 'default_build = "mock"' in cfg.read_text()
    res = runner.invoke(cli.app, ["--config", str(cfg), "builds", "list"])
    assert "mock" in res.output
    res = runner.invoke(cli.app, ["--config", str(cfg), "run", "--hours", str(1 / 3600)])
    assert res.exit_code == 0, res.output
    assert "report.md" in res.output
    res = runner.invoke(cli.app, ["--config", str(cfg), "restore"])
    assert "未復元の退避はありません" in res.output


def test_cli_builds_check_boot(tmp_path, mock_app, monkeypatch, fake_platform):
    """builds check は rules.yaml の C-03 の時間だけ boot を待つ（モックはすぐ boot を出す）。"""
    monkeypatch.setattr(cli, "get_platform", lambda: fake_platform)
    cfg = write_config(tmp_path, f'[builds.mock]\npath = "{mock_app}"\n')
    res = CliRunner().invoke(cli.app, ["--config", str(cfg), "builds", "check", "mock"])
    assert res.exit_code == 0, res.output
    assert "boot を受信しました" in res.output and "0.0.1-mock" in res.output


@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM"])
def test_cli_signal_stops_game_and_restores(tmp_path, mock_app, sig):
    """実際の yomawari コマンドに中断シグナルを送り、ゲーム終了・復元・レポート出力を確かめる。"""
    import signal
    import subprocess
    import sys
    import time

    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("original")
    cfg = write_config(
        tmp_path,
        f'[run]\nopen_report = false\n[builds.mock]\npath = "{mock_app}"\nsave_dir = "{save}"\n'
        f'prefs_domain = "test.yomawari.pytest.{sig.lower()}"\n',
    )
    exe = Path(sys.executable).parent / "yomawari"
    p = subprocess.Popen([str(exe), "--config", str(cfg), "run", "--build", "mock", "--hours", "1"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = None
    for _ in range(100):
        log = next((tmp_path / "runs").glob("*/player.log"), None)
        if log and "boot" in log.read_text():
            break
        time.sleep(0.1)
    (save / "slot1.dat").write_text("changed during run")
    p.send_signal(getattr(signal, sig))
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 0, out.decode()
    assert (save / "slot1.dat").read_text() == "original"
    assert not (tmp_path / "runs" / "restore_pending.json").exists()
    events_path = log.parent / "events.jsonl"
    last = json.loads(events_path.read_text().splitlines()[-1])
    assert last["kind"] == "proc_exit" and last["data"]["by_harness"] is True
    assert "中断の指示" in (log.parent / "report.md").read_text()
