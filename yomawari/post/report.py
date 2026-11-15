"""report.md の生成（DESIGN.md §12「レポート構成」）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import FUZZ_DEFAULTS
from ..events import SEVERITIES, Issue
from .compare import PrevRun, load_previous
from .coverage import render as render_coverage
from .digest import _cell, build_items, render_top
from .stalls import load_events

SEVERITY_LABEL = {"critical": "Critical", "major": "Major", "minor": "Minor", "info": "Info"}
LOG_ERROR_RULE = "L-01"

@dataclass
class RunSummary:
    run_id: str
    build_name: str
    build_path: str
    started: str
    ended: str
    duration_sec: float
    mode: str
    seed: str
    launches: int
    restarts: int
    interrupted: bool
    rules_path: str
    env: dict[str, str] = field(default_factory=dict)
    harness_notes: list[str] = field(default_factory=list)
    silent_buttons: dict[str, int] = field(default_factory=dict)  # 一度も反応しなかったボタン → 押した回数
    purpose: str | None = None  # ランの目的（yomawari/purpose.py）
    purpose_description: str = ""
    focus: list[str] = field(default_factory=list)  # 目的で重点的に見るルール。深刻度ごとに先に並べる
    perf: list[str] = field(default_factory=list)  # 「パフォーマンス」の節（detect/perf.py）
    suspect_error_window_sec: float = 120.0  # 検出のこの秒数前までのエラーログを、手がかりとして項目に並べる
    stop_reason: str = ""  # 中断されたときの理由（手動か、外から止められたか）
    planned_hours: float | None = None  # 予定していた時間
    # レポートの設定（[fuzz] の report_* と stall_report_sec）。ないもの（古い run.json・操作しないモード）は既定値
    report_cfg: dict[str, Any] = field(default_factory=dict)


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _wall(issue: Issue) -> datetime | None:
    try:
        return datetime.fromisoformat(issue.wall)
    except ValueError:
        return None


def _error_before(issue: Issue, issues: list[Issue], window: float) -> Issue | None:
    """同じ起動で、検出の直前（window 秒以内）に出たエラーログ。進まない・反応しないなどの原因の手がかりになる。"""
    at = _wall(issue)
    if at is None or issue.rule_id == LOG_ERROR_RULE:
        return None
    near = []
    for other in issues:
        when = _wall(other)
        if other.rule_id == LOG_ERROR_RULE and other.launch == issue.launch and when is not None:
            if 0 <= (at - when).total_seconds() <= window:
                near.append((when, other))
    return max(near, key=lambda pair: pair[0])[1] if near else None


def render(summary: RunSummary, issues: list[Issue], run_dir: Path) -> str:
    """report.md の中身。先頭に「まとめ」「やること」と項目ごとの節（post/digest.py）、そのあとにランの情報・性能・
    一度も反応しなかったボタン・実行環境を置く（DESIGN.md §12「レポート構成」）。"""
    cfg = {**{k: v for k, v in FUZZ_DEFAULTS.items() if k.startswith("report_") or k == "stall_report_sec"},
           **summary.report_cfg}
    if summary.mode == "aging":
        cfg["stall_report_sec"] = 0.0  # 操作しないので、進まないのが正常
    events = load_events(run_dir / "events.jsonl")
    focus = {rule: n for n, rule in enumerate(summary.focus)}
    window = summary.suspect_error_window_sec
    counts = {s: len({i.key for i in issues if i.severity == s}) for s in SEVERITIES}
    out: list[str] = [f"# Yomawari レポート {summary.run_id}", ""]

    items = build_items(issues, events, cfg, summary.harness_notes, summary.silent_buttons, focus)
    stopped = _stopped(summary) if summary.interrupted else "予定どおり終わった"
    prev = load_previous(run_dir, summary.build_name)
    out += render_top(items, events, summary.duration_sec, summary.launches, summary.restarts, stopped, cfg,
                      lambda i: _error_before(i, issues, window), summary.interrupted,
                      prev.items if prev is not None else None)

    out += ["## ランの情報", "", "| 項目 | 値 |", "|---|---|"]
    out += [
        f"| ビルド | {summary.build_name} (`{summary.build_path}`) |",
        f"| 開始 / 終了 | {summary.started} / {summary.ended} |",
        f"| 実行時間 | {_hms(summary.duration_sec)}{'（中断）' if summary.interrupted else ''} |",
        *([f"| 終わり方 | {_stopped(summary)} |"] if summary.interrupted else []),
        *([f"| 目的 | {summary.purpose}: {summary.purpose_description} |"] if summary.purpose else []),
        *([f"| 重点的に見るルール | {', '.join(summary.focus)} |"] if summary.focus else []),
        f"| モード / シード | {summary.mode} / {summary.seed} |",
        f"| 起動回数 / 再起動回数 | {summary.launches} / {summary.restarts} |",
        "| 深刻度別件数 | "
        + " ".join(f"{SEVERITY_LABEL[s]} {counts[s]}" for s in SEVERITIES)
        + " |",
        f"| 前回比 | {_vs_prev(items, prev)} |",
        "",
    ]
    out += _gone(items, prev)
    out += summary.perf

    if summary.silent_buttons:
        out += [
            "## 一度も反応しなかったボタン", "",
            ("押しても画面が一度も変わらなかったボタン（S-05 の対象にしていない）。ロック中・空のスロットなど仕様どおりなら問題ない。"
             "押せそうに見えて何も起きないボタンがないかを見る"), "",
            "| ボタン | 押した回数 |", "|---|---|",
        ]
        out += [f"| `{k}` | {n} |" for k, n in sorted(summary.silent_buttons.items())]
        out.append("")

    out += render_coverage(events, cfg)

    out += ["## 実行環境", ""]
    out += [f"- {k}: {v}" for k, v in summary.env.items()]
    out.append(f"- ルール: `{summary.rules_path}`")
    if summary.harness_notes:
        out += ["", "### ハーネスの記録", ""]
        out += [f"- {n}" for n in summary.harness_notes]
    out.append("")
    return "\n".join(out)


def _vs_prev(items: list, prev: PrevRun | None) -> str:
    if prev is None:
        return "前回のランがない（同じビルドで、これより前に始めたランがない）"
    keys = {it.key for it in items if it.key is not None}
    new = sum(1 for k in keys if k not in prev.items)
    gone = sum(1 for k in prev.items if k not in keys)
    took = f"、{_hms(prev.duration_sec)}" if prev.duration_sec is not None else ""
    return (f"`{prev.run_id}`（{len(prev.items)} 項目{took}）と比べて、新規 {new}・継続 {len(keys) - new}・出なくなった {gone}"
            "（検出の項目の数。「やること」の表の「前回」の列）")


def _gone(items: list, prev: PrevRun | None) -> list[str]:
    """前回は出て、今回は出なかった検出の項目。"""
    if prev is None:
        return []
    keys = {it.key for it in items if it.key is not None}
    gone = [(title, n) for k, (title, n) in prev.items.items() if k not in keys]
    if not gone:
        return []
    out = ["## 前回から出なくなったもの", "",
           (f"前回（`{prev.run_id}`）は出て、今回は出なかった検出。直ったか、今回はそこまで行かなかったか、ルールが変わったか"
            "は、これだけでは分からない"), "",
           "| 起きたこと | 前回の回数 |", "|---|---|"]
    out += [f"| {_cell(title)} | {n} |" for title, n in sorted(gone, key=lambda g: -g[1])]
    return out + [""]


def _stopped(summary: RunSummary) -> str:
    """中断されたランの終わり方。途中で止めたので、予定の時間まで見ていないことが分かるように書く。"""
    reason = summary.stop_reason or "中断された（理由は不明）"
    if summary.planned_hours is None:
        return reason
    return f"{reason}。予定の {summary.planned_hours:g} 時間より前に止めたので、ここまでの結果"


def write_report(summary: RunSummary, issues: list[Issue], run_dir: Path) -> Path:
    path = run_dir / "report.md"
    path.write_text(render(summary, issues, run_dir), encoding="utf-8")
    return path
