"""見張り役（supervisor.py）: ハーネス本体が終了処理まで進まなかったときの後始末。"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from yomawari.supervisor import Supervisor, running_game

RULES = Path(__file__).resolve().parent.parent / "rules.yaml"

# 偽のハーネス本体: ランのディレクトリを知らせ、セーブを退避し、「ゲーム」を起動してから、how のとおりに終わる
CHILD = textwrap.dedent('''
    import json, os, signal, subprocess, sys, time
    from pathlib import Path
    from yomawari.savestate import SaveState, SaveTarget
    from yomawari.supervisor import write_child_state
    runs, save, how = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    run = runs / "2026-01-01_0100_dev"
    run.mkdir(parents=True)
    write_child_state(run_dir=str(run), pid=os.getpid())
    SaveState(runs, None).backup(run, SaveTarget(save, None), None)
    (save / "slot1.dat").write_text("changed during run")
    game = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (runs / "game.pid").write_text(str(game.pid))
    from datetime import datetime
    ev = {"source": "harness", "kind": "launch", "data": {"pid": game.pid, "cmd": ["/not/the/game"]}, "launch": 0,
          "t": 0, "wall": datetime.now().astimezone().isoformat()}
    (run / "events.jsonl").write_text(json.dumps(ev) + "\\n")
    if how == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    elif how == "hang":
        time.sleep(60)
    elif how == "error":
        write_child_state(error="Traceback (most recent call last):\\nRuntimeError: こわれた")
        sys.exit(2)
    elif how == "ok":
        game.kill()
        (run / "run.json").write_text(json.dumps({"run_id": run.name, "build_name": "dev", "build_path": "x",
            "started": "s", "ended": "e", "duration_sec": 1, "mode": "random", "seed": "1", "launches": 1,
            "restarts": 0, "interrupted": False, "rules_path": "rules.yaml"}))
        (run / "report.md").write_text("ok")
        SaveState(runs, None).restore()
        sys.exit(1)
''')


def _run(tmp_path, how, hang_sec=60.0):
    runs, save = tmp_path / "runs", tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("original")
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    sup = Supervisor([sys.executable, str(script), str(runs), str(save), how], runs, RULES, hang_sec, 1.0, poll_sec=0.1)
    code, report = sup.run()
    return sup, code, report, runs, save


def _gone(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_harness_killed(tmp_path):
    sup, code, report, runs, save = _run(tmp_path, "kill")
    assert code == 2 and report is not None
    assert _gone(int((runs / "game.pid").read_text()))
    assert (save / "slot1.dat").read_text() == "original"
    assert not (runs / "restore_pending.json").exists()
    text = report.read_text()
    assert "見張り役（ウォッチドッグ）: ハーネス本体がシグナル（SIGKILL）で終わった" in text
    assert "残っていたゲーム" in text and "セーブを復元" in text
    assert not list((runs / "_watchdog").iterdir())


def test_harness_hung(tmp_path):
    _, code, report, runs, save = _run(tmp_path, "hang", hang_sec=1.0)
    assert code == 2 and "固まったとみなして止めた" in report.read_text()
    assert (save / "slot1.dat").read_text() == "original"
    assert _gone(int((runs / "game.pid").read_text()))


def test_harness_error_is_written(tmp_path):
    _, code, report, runs, _ = _run(tmp_path, "error")
    assert code == 2
    assert "ハーネス本体の例外: RuntimeError: こわれた" in report.read_text()
    assert "こわれた" in (report.parent / "harness_error.txt").read_text()


def test_harness_finished_normally(tmp_path):
    _, code, report, runs, save = _run(tmp_path, "ok")
    assert (code, report) == (1, None)
    assert (runs / "2026-01-01_0100_dev" / "report.md").read_text() == "ok"


def test_running_game(tmp_path):
    p = tmp_path / "events.jsonl"
    rows = [{"source": "harness", "kind": "launch", "data": {"pid": 10, "cmd": ["/g"]}, "launch": 0, "wall": "w0"},
            {"source": "proc", "kind": "proc_exit", "data": {}, "launch": 0},
            {"source": "harness", "kind": "launch", "data": {"pid": 11, "cmd": ["/g"]}, "launch": 1, "wall": "w1"}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert running_game(p) == (11, "/g", "w1")
    p.write_text("\n".join(json.dumps(r) for r in rows[:2]))
    assert running_game(p) is None
    assert running_game(tmp_path / "none") is None


def test_cli_child_killed_watchdog_cleans_up(tmp_path, mock_app):
    """yomawari run のハーネス本体を kill -9 しても、見張り役がゲームを止め、セーブを戻し、レポートを作る。"""
    from tests.test_e2e import write_config

    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("original")
    cfg = write_config(tmp_path, f'[run]\nopen_report = false\n[builds.mock]\npath = "{mock_app}"\nsave_dir = "{save}"\n'
                                 'prefs_domain = "test.yomawari.pytest.watchdog"\n')
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
    state = json.loads(next((tmp_path / "runs" / "_watchdog").glob("*.json")).read_text())
    os.kill(state["pid"], signal.SIGKILL)
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 2, out.decode()
    assert (save / "slot1.dat").read_text() == "original"
    assert not (tmp_path / "runs" / "restore_pending.json").exists()
    report = (log.parent / "report.md").read_text()
    assert "SIGKILL" in report and "残っていたゲーム" in report
    assert "見張り役が後始末をしました" in out.decode()


def test_stop_game_leaves_reused_pid_alone(tmp_path):
    """pid が別のプロセスに使い回されていたら（実行ファイルも起動時刻も違う）、触らない。"""
    from yomawari.supervisor import stop_game

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert stop_game(other.pid, "/not/the/game", "2020-01-01T00:00:00+09:00", 1.0) is None
        assert other.poll() is None
    finally:
        other.kill()
