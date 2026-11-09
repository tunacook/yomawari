"""ランの記録からレポートを作り直す（post/rebuild.py、yomawari report）。"""

import json

from yomawari.events import Issue
from yomawari.post.rebuild import SUMMARY_FILE, load_summary, rebuild_report, save_summary
from yomawari.post.report import RunSummary

from .conftest import RULES_PATH


def _event(kind, wall, t=0.0, source="harness", launch=0, data=None, **fields):
    return {"source": source, "kind": kind, "data": data or fields, "launch": launch, "t": t, "wall": wall}


def _write_events(run_dir, extra=()):
    run_dir.mkdir(exist_ok=True)
    issue = Issue("L-01", "major", "ログ", "Error: x", "L-01:x", 0, 1.0, "2026-01-01T00:00:01+09:00").to_json()
    evs = [_event("launch", "2026-01-01T00:00:00+09:00", cmd=["/path/game.app"]),
           _event("issue", "2026-01-01T00:00:01+09:00", data=issue),
           _event("launch", "2026-01-01T00:30:00+09:00", t=1800.0, launch=1, cmd=["/path/game.app"]),
           *extra,
           _event("heartbeat", "2026-01-01T01:00:00+09:00", t=3600.0, source="game", launch=1, fps=60.0)]
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs) + "\n", encoding="utf-8")


def _summary(**kw):
    base = dict(run_id="r", build_name="dev", build_path="/path/game.app", started="2026-01-01T00:00:00+09:00",
                ended="2026-01-01T01:00:00+09:00", duration_sec=3600.0, mode="random", seed="7", launches=2, restarts=0,
                interrupted=False, rules_path=str(RULES_PATH), harness_notes=["記録"], silent_buttons={"A/B": 3},
                stall_rows=[("`s:1`", "手がかり")], planned_hours=1.0)
    return RunSummary(**{**base, **kw})


def test_summary_round_trip(tmp_path):
    s = _summary()
    save_summary(s, tmp_path)
    assert load_summary(tmp_path) == s
    assert not (tmp_path / "run.tmp").exists()


def test_load_summary_tolerates_missing_broken_and_unknown(tmp_path):
    assert load_summary(tmp_path) is None
    (tmp_path / SUMMARY_FILE).write_text("{", encoding="utf-8")
    assert load_summary(tmp_path) is None
    save_summary(_summary(), tmp_path)
    doc = json.loads((tmp_path / SUMMARY_FILE).read_text(encoding="utf-8"))
    (tmp_path / SUMMARY_FILE).write_text(json.dumps({**doc, "later_field": 1}), encoding="utf-8")
    assert load_summary(tmp_path) is not None  # 新しい版のフィールドがあっても読める


def test_rebuild_finished_run_keeps_saved_values_and_previous_report(tmp_path):
    _write_events(tmp_path)
    save_summary(_summary(), tmp_path)
    (tmp_path / "report.md").write_text("前のレポート", encoding="utf-8")
    text = rebuild_report(tmp_path, RULES_PATH).read_text(encoding="utf-8")
    assert "Error: x" in text and "`A/B`" in text and "記録" in text
    assert "作り直した" in text and "終了処理まで進まなかった" not in text
    assert (tmp_path / "report.prev.md").read_text(encoding="utf-8") == "前のレポート"


def test_rebuild_run_without_summary_says_what_is_missing(tmp_path):
    _write_events(tmp_path)
    text = rebuild_report(tmp_path, RULES_PATH).read_text(encoding="utf-8")
    assert "Error: x" in text
    assert "ハーネスが終了処理まで進まなかった" in text and "記録から作り直した" in text
    assert "/path/game.app" in text and "不明" in text


def test_rebuild_killed_run_uses_summary_from_the_start(tmp_path):
    _write_events(tmp_path)
    save_summary(_summary(ended="", duration_sec=0.0, launches=0, harness_notes=[]), tmp_path)
    text = rebuild_report(tmp_path, RULES_PATH).read_text(encoding="utf-8")
    assert "ハーネスが終了処理まで進まなかった" in text
    assert load_summary(tmp_path).ended == ""  # run.json は書き換えない


def test_rebuild_with_bad_rules_path_still_writes_report(tmp_path):
    _write_events(tmp_path)
    text = rebuild_report(tmp_path, tmp_path / "missing.yaml").read_text(encoding="utf-8")
    assert "パフォーマンスの節を作れなかった" in text and "Error: x" in text


def test_report_command(tmp_path):
    from typer.testing import CliRunner

    from yomawari.cli import app

    cfg = tmp_path / "yomawari.toml"
    cfg.write_text(f'runs_dir = "{tmp_path / "runs"}"\nrules = "{RULES_PATH}"\n', encoding="utf-8")
    run = tmp_path / "runs" / "2026-01-01_0000_dev"
    run.mkdir(parents=True)
    _write_events(run)
    res = CliRunner().invoke(app, ["--config", str(cfg), "report", run.name])
    assert res.exit_code == 0, res.output
    assert "Error: x" in (run / "report.md").read_text(encoding="utf-8")
    assert CliRunner().invoke(app, ["--config", str(cfg), "report", "no-such-run"]).exit_code == 2
    assert CliRunner().invoke(app, ["--config", str(cfg), "report", "../runs"]).exit_code == 2
