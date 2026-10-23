"""report.md の生成（フェーズ1の最小構成）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..events import SEVERITIES, Issue

SEVERITY_LABEL = {"critical": "Critical", "major": "Major", "minor": "Minor", "info": "Info"}


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


@dataclass
class IssueGroup:
    first: Issue
    count: int
    launches: list[int]


def group_issues(issues: list[Issue]) -> list[IssueGroup]:
    groups: dict[str, IssueGroup] = {}
    for i in issues:
        g = groups.get(i.key)
        if g is None:
            groups[i.key] = IssueGroup(i, 1, [i.launch])
        else:
            g.count += 1
            if i.launch not in g.launches:
                g.launches.append(i.launch)
    return list(groups.values())


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _fence(text: str) -> str:
    return "```text\n" + text.rstrip("\n") + "\n```"


def render(summary: RunSummary, issues: list[Issue], run_dir: Path) -> str:
    groups = group_issues(issues)
    counts = {s: sum(1 for g in groups if g.first.severity == s) for s in SEVERITIES}
    out: list[str] = [f"# Yomawari レポート {summary.run_id}", ""]

    out += ["## サマリ", "", "| 項目 | 値 |", "|---|---|"]
    out += [
        f"| ビルド | {summary.build_name} (`{summary.build_path}`) |",
        f"| 開始 / 終了 | {summary.started} / {summary.ended} |",
        f"| 実行時間 | {_hms(summary.duration_sec)}{'（中断）' if summary.interrupted else ''} |",
        f"| モード / シード | {summary.mode} / {summary.seed} |",
        f"| 起動回数 / 再起動回数 | {summary.launches} / {summary.restarts} |",
        "| 深刻度別件数 | "
        + " ".join(f"{SEVERITY_LABEL[s]} {counts[s]}" for s in SEVERITIES)
        + " |",
        "| 前回比 | （フェーズ4で対応） |",
        "",
    ]

    for sev in SEVERITIES:
        sev_groups = [g for g in groups if g.first.severity == sev]
        out += [f"## {SEVERITY_LABEL[sev]}", ""]
        if not sev_groups:
            out += ["なし", ""]
            continue
        for g in sev_groups:
            i = g.first
            out += [
                f"### [{i.rule_id}] {i.title}",
                "",
                f"- 内容: {i.detail}",
                f"- 発生回数: {g.count}",
                f"- 最初の発生: {i.wall}（起動 #{i.launch}）",
                f"- 発生した起動: {', '.join(f'#{n}' for n in g.launches)}",
            ]
            log_name = "player.log" if i.launch == 0 else f"player.{i.launch}.log"
            out.append(f"- ログ: [{log_name}]({log_name})")
            if "dump" in i.evidence:
                out.append(f"- クラッシュダンプ: `{i.evidence['dump']}`")
            if i.evidence.get("log"):
                out += ["- ログ抜粋:", "", _fence(i.evidence["log"])]
            if i.evidence.get("recent"):
                out.append(f"- 直前の操作（古い順）: {' → '.join(f'`{r}`' for r in i.evidence['recent'])}")
            st = i.evidence.get("state")
            if st:
                out.append(
                    f"- そのときの画面: scene `{st.get('scene')}`、行 `{st.get('line_id')}`、"
                    f"menu {', '.join(f'`{m}`' for m in st.get('menus') or []) or 'なし'}、"
                    f"最前面のモーダル `{st.get('top_modal')}`"
                )
            out += ["", "- 判定: [ ] 正しい検出　[ ] 誤検知", ""]

    out += ["## 実行環境", ""]
    out += [f"- {k}: {v}" for k, v in summary.env.items()]
    out.append(f"- ルール: `{summary.rules_path}`")
    if summary.harness_notes:
        out += ["", "### ハーネスの記録", ""]
        out += [f"- {n}" for n in summary.harness_notes]
    out.append("")
    return "\n".join(out)


def write_report(summary: RunSummary, issues: list[Issue], run_dir: Path) -> Path:
    path = run_dir / "report.md"
    path.write_text(render(summary, issues, run_dir), encoding="utf-8")
    return path
