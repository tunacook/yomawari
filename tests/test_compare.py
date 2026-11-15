"""前回のランとの比較（post/compare.py と、レポートの「前回」の列・前回比・前回から出なくなったもの）。"""

import json
from dataclasses import asdict

from yomawari.events import Issue
from yomawari.post.compare import item_titles, previous_run
from yomawari.post.report import RunSummary, render


def _w(sec):
    return f"2026-01-01T00:{int(sec) // 60:02d}:{sec % 60:06.3f}+09:00"


def _issue(rule, key, sec=1.0, title=None):
    return Issue(rule, "major", title or f"{rule} の題", "詳細", key, 0, sec, _w(sec))


def _write(run_dir, issues, last=60.0):
    run_dir.mkdir(parents=True)
    events = [{"source": "harness", "kind": "launch", "data": {}, "launch": 0, "t": 0, "wall": _w(0)}]
    events += [{"source": "harness", "kind": "issue", "data": asdict(i), "launch": 0, "t": i.t, "wall": i.wall} for i in issues]
    events.append({"source": "proc", "kind": "proc_exit", "data": {}, "launch": 0, "t": last, "wall": _w(last)})
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")


def _summary(run_id, build="dev"):
    return RunSummary(run_id=run_id, build_name=build, build_path="/b.app", started="s", ended="e", duration_sec=60.0,
                      mode="random", seed="1", launches=1, restarts=0, interrupted=False, rules_path="rules.yaml")


def test_previous_run(tmp_path):
    for name in ("2026-01-01_0100_dev", "2026-01-02_0100_dev", "2026-01-02_0100_dev_2", "2026-01-03_0100_other",
                 "2026-01-04_0100_dev"):
        _write(tmp_path / name, [])
    (tmp_path / "2026-01-03_0100_dev").mkdir()  # events.jsonl がないラン（始めてすぐ止まった）は飛ばす
    assert previous_run(tmp_path, "2026-01-04_0100_dev", "dev").name == "2026-01-02_0100_dev_2"
    assert previous_run(tmp_path, "2026-01-02_0100_dev", "dev").name == "2026-01-01_0100_dev"
    assert previous_run(tmp_path, "2026-01-01_0100_dev", "dev") is None
    assert previous_run(tmp_path, "2026-01-04_0100_other", "other").name == "2026-01-03_0100_other"
    assert previous_run(tmp_path / "none", "x", "dev") is None


def test_item_titles_use_report_grouping():
    issues = [_issue("S-05", "S-05:MenuUI/Page01"), _issue("S-05", "S-05:MenuUI/Page02"),
              _issue("C-01", "C-01:1", title="異常終了")]
    assert item_titles(issues) == {("button", "MenuUI/Page"): ("`MenuUI/Page` を押しても反応しない", 2),
                                   ("C-01",): ("[C-01] 異常終了", 1)}


def test_report_marks_new_continuing_and_gone(tmp_path):
    prev = [_issue("S-05", "S-05:MenuUI/Page01"), _issue("S-05", "S-05:MenuUI/Page03"),
            _issue("R-01", "R-01", title="メモリが増え続ける")]
    _write(tmp_path / "2026-01-01_0100_dev", prev, last=1800.0)
    now = [_issue("S-05", "S-05:MenuUI/Page02"), _issue("C-01", "C-01:1", title="異常終了")]
    run = tmp_path / "2026-01-02_0100_dev"
    _write(run, now)
    text = render(_summary(run.name), now, run)
    rows = {r.split(" | ")[1]: r for r in text.splitlines() if r.startswith("| ") and r[2:3].isdigit()}
    assert "| **新規** |" in rows["[C-01] 異常終了"]
    assert "| 継続（前回 2 回） |" in rows["[S-05] `MenuUI/Page` を押しても反応しない"]
    assert "| 前回比 | `2026-01-01_0100_dev`（2 項目、00:30:00）と比べて、新規 1・継続 1・出なくなった 1" in text
    gone = text.split("## 前回から出なくなったもの", 1)[1].split("\n## ", 1)[0]
    assert "| [R-01] メモリが増え続ける | 1 |" in gone and "MenuUI" not in gone


def test_report_without_previous_run(tmp_path):
    run = tmp_path / "2026-01-02_0100_dev"
    now = [_issue("C-01", "C-01:1")]
    _write(run, now)
    text = render(_summary(run.name), now, run)
    assert "| 前回比 | 前回のランがない" in text
    assert "| 前回 |" not in text and "## 前回から出なくなったもの" not in text
