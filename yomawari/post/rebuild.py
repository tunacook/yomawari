"""ランの記録からレポートを作り直す（yomawari report。DESIGN.md「レポート構成」）。

ハーネスは、ランの始めと終わりに RunSummary を run.json へ書く。events.jsonl には検出（issue）も入っているので、
この 2 つから report.md を作り直せる。終了処理まで進まなかったラン（run.json が始めのままか、ない）は、
events.jsonl から分かる範囲で埋め、分からなかったものをレポートの「ハーネスの記録」に書く。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..detect.engine import Engine, RuleError, load_rules
from ..detect.perf import load_perf
from ..events import Event, Issue
from .report import RunSummary, write_report
from .stalls import load_events

SUMMARY_FILE = "run.json"
UNKNOWN = "不明"


def save_summary(summary: RunSummary, run_dir: Path) -> None:
    path = run_dir / SUMMARY_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(summary), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_summary(run_dir: Path) -> RunSummary | None:
    """run.json を読む。ない・読めないときは None。"""
    try:
        doc = json.loads((run_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    names = {f.name for f in dataclasses.fields(RunSummary)}
    known = {k: v for k, v in doc.items() if k in names}
    try:
        return RunSummary(**known)
    except TypeError:
        return None


def _issues(events: list[dict]) -> list[Issue]:
    names = {f.name for f in dataclasses.fields(Issue)}
    return [Issue(**{k: v for k, v in e["data"].items() if k in names}) for e in events if e["kind"] == "issue"]


def _perf_lines(events: list[dict], rules_path: Path) -> tuple[list[str], str]:
    """性能の節を、記録したイベントを検出に流し直して作る。(節の行, 作れなかった理由)。"""
    try:
        perf = load_perf(rules_path)
        rules = {k: r for k, r in load_rules(rules_path).items() if k.startswith("R-")}
    except (OSError, RuleError, ValueError) as e:
        return [], f"パフォーマンスの節を作れなかった: {e}"
    if perf is None or not rules:
        return [], ""
    engine = Engine(rules, perf)
    for e in events:
        if e["kind"] != "issue":
            engine.feed(Event(e["source"], e["kind"], e["data"], e["launch"], e["t"], e["wall"]))
    return (engine.perf.report_lines() if engine.perf is not None else []), ""


def _estimated(run_dir: Path, events: list[dict], partial: RunSummary | None, rules_path: Path) -> RunSummary:
    """終了処理まで進まなかったランの RunSummary。"""
    walls = [e["wall"] for e in events if e.get("wall")]
    launches = [e for e in events if e["kind"] == "launch"]
    cmd = launches[0]["data"].get("cmd") if launches else None
    duration = 0.0
    if walls:
        duration = (datetime.fromisoformat(walls[-1]) - datetime.fromisoformat(walls[0])).total_seconds()
    name = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{4}_", "", run_dir.name)
    summary = partial or RunSummary(
        run_id=run_dir.name, build_name=name, build_path=cmd[0] if cmd else UNKNOWN, started=walls[0] if walls else UNKNOWN,
        ended="", duration_sec=0.0, mode=UNKNOWN, seed=UNKNOWN, launches=0, restarts=0, interrupted=True,
        rules_path=str(rules_path),
    )
    perf, why = _perf_lines(events, Path(summary.rules_path) if partial else rules_path)
    notes = ["終了処理まで進まなかったランを、記録から作り直した。起動のやり直しの回数と、終了処理で書くハーネスの記録"
             "（一度も反応しなかったボタンを含む）は残っていない"]
    return dataclasses.replace(
        summary, ended=walls[-1] if walls else UNKNOWN, duration_sec=duration, launches=len(launches), interrupted=True,
        stop_reason="ハーネスが終了処理まで進まなかった", perf=perf,
        harness_notes=[*summary.harness_notes, *notes, *([why] if why else [])],
    )


def rebuild_report(run_dir: Path, rules_path: Path) -> Path:
    """run_dir の report.md を作り直して、そのパスを返す。前の report.md は report.prev.md に残す。
    events.jsonl がなければ FileNotFoundError。"""
    events = load_events(run_dir / "events.jsonl")
    summary = load_summary(run_dir)
    if summary is None or not summary.ended:
        summary = _estimated(run_dir, events, summary, rules_path)
    else:
        summary = dataclasses.replace(summary, harness_notes=[*summary.harness_notes, "`yomawari report` で作り直した"])
    report = run_dir / "report.md"
    if report.exists():
        os.replace(report, run_dir / "report.prev.md")
    return write_report(summary, _issues(events), run_dir)
