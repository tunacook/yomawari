"""report.md の生成（フェーズ1の最小構成）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..events import SEVERITIES, Issue

SEVERITY_LABEL = {"critical": "Critical", "major": "Major", "minor": "Minor", "info": "Info"}
# 「不具合の疑い」に載せる、ハーネスの記録の言い回し（流れ・巻き戻し・一覧の探索が、不具合かもしれないと書くときのもの）
SUSPECT_NOTE_WORDS = ("不具合の可能性", "進めない可能性")
LOG_ERROR_RULE = "L-01"
CELL_LEN = 120  # 「不具合の疑い」の表の1マスの文字数（長いものは下の節で読む）


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
    stalls: list[str] = field(default_factory=list)  # 「進めなかったところ」の節（post/stalls.py）
    perf: list[str] = field(default_factory=list)  # 「パフォーマンス」の節（detect/perf.py）
    stall_rows: list[tuple[str, str]] = field(default_factory=list)  # 進めなかった区間の (場所, 手がかり)（post/stalls.py）
    suspect_error_window_sec: float = 120.0  # 検出のこの秒数前までのエラーログを、手がかりとして「不具合の疑い」に並べる


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


def _cell(text: str) -> str:
    """表の1マス: 1行目だけを、表を崩さない形で短くする。"""
    line = text.strip().split("\n", 1)[0].replace("|", "\\|")
    return line if len(line) <= CELL_LEN else line[:CELL_LEN] + "…"


def _place(issue: Issue) -> str:
    st = issue.evidence.get("state") or {}
    where = st.get("line_id") or st.get("scene")
    return f"`{where}`" if where else "-"


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


def _suspects(summary: RunSummary, issues: list[Issue], groups: list[IssueGroup]) -> list[str]:
    """「不具合の疑い」の節: 検出・進めなかった区間・ハーネスの記録に散らばった疑いを、重い順に1つの表にまとめる。
    Critical と Major は検出ごとに、Minor と Info は件数が多くなるのでルールごとに1行にする。"""
    rows: list[str] = []
    window = summary.suspect_error_window_sec
    for sev in ("critical", "major"):
        # 同じ重さなら、目的で重点的に見るルール、動作の検出、性能（R-）の順
        rank = {rule: n for n, rule in enumerate(summary.focus)}
        for g in sorted((g for g in groups if g.first.severity == sev),
                        key=lambda g: (rank.get(g.first.rule_id, len(rank)), g.first.rule_id.startswith("R-"))):
            i = g.first
            err = next((e for e in (_error_before(x, issues, window) for x in issues if x.key == i.key) if e), None)
            clue = f"直前のエラーログ: {err.detail}" if err is not None else i.detail
            rows.append(f"| {SEVERITY_LABEL[sev]} | [{i.rule_id}] {i.title} | {_place(i)} | {g.count} | {_cell(clue)} |")
    rows += [f"| 要確認 | 進めなかった区間 | {where} | 1 | {_cell(clue)} |" for where, clue in summary.stall_rows]
    for sev in ("minor", "info"):
        by_rule: dict[str, list[IssueGroup]] = {}
        for g in groups:
            if g.first.severity == sev:
                by_rule.setdefault(g.first.rule_id, []).append(g)
        for rule, gs in by_rule.items():
            total = sum(g.count for g in gs)
            rows.append(f"| {SEVERITY_LABEL[sev]} | [{rule}] {gs[0].first.title} | {len(gs)} か所 | {total} | "
                        f"{_cell(gs[0].first.detail)} |")
    notes = [n for n in summary.harness_notes if any(w in n for w in SUSPECT_NOTE_WORDS)]
    rows += [f"| 要確認 | ハーネスの記録 | - | 1 | {_cell(n)} |" for n in notes]
    if summary.silent_buttons:
        names = "、".join(f"`{k}`" for k in sorted(summary.silent_buttons))
        rows.append(f"| 要確認 | 一度も反応しなかったボタン | - | {len(summary.silent_buttons)} | {_cell(names)} |")
    out = ["## 不具合の疑い", "",
           ("このランで不具合かもしれないと見たものを、重い順にまとめた。確定ではなく、ハーネスの進め方が原因のものや、"
            "仕様どおりのものも含む。詳しくは下の各節を見る"), ""]
    if not rows:
        return out + ["なし", ""]
    return out + ["| 重さ | 疑い | 場所 | 回数 | 手がかり |", "|---|---|---|---|---|", *rows, ""]


def render(summary: RunSummary, issues: list[Issue], run_dir: Path) -> str:
    groups = group_issues(issues)
    counts = {s: sum(1 for g in groups if g.first.severity == s) for s in SEVERITIES}
    out: list[str] = [f"# Yomawari レポート {summary.run_id}", ""]

    out += ["## サマリ", "", "| 項目 | 値 |", "|---|---|"]
    out += [
        f"| ビルド | {summary.build_name} (`{summary.build_path}`) |",
        f"| 開始 / 終了 | {summary.started} / {summary.ended} |",
        f"| 実行時間 | {_hms(summary.duration_sec)}{'（中断）' if summary.interrupted else ''} |",
        *([f"| 目的 | {summary.purpose}: {summary.purpose_description} |"] if summary.purpose else []),
        *([f"| 重点的に見るルール | {', '.join(summary.focus)} |"] if summary.focus else []),
        f"| モード / シード | {summary.mode} / {summary.seed} |",
        f"| 起動回数 / 再起動回数 | {summary.launches} / {summary.restarts} |",
        "| 深刻度別件数 | "
        + " ".join(f"{SEVERITY_LABEL[s]} {counts[s]}" for s in SEVERITIES)
        + " |",
        "| 前回比 | （フェーズ4で対応） |",
        "",
    ]
    out += _suspects(summary, issues, groups)

    for sev in SEVERITIES:
        sev_groups = [g for g in groups if g.first.severity == sev]
        # 目的で重点的に見るルールを先に（focus に書いた順）
        rank = {rule: n for n, rule in enumerate(summary.focus)}
        sev_groups.sort(key=lambda g: rank.get(g.first.rule_id, len(rank)))
        out += [f"## {SEVERITY_LABEL[sev]}", ""]
        if not sev_groups:
            out += ["なし", ""]
            continue
        for g in sev_groups:
            i = g.first
            star = "（重点）" if i.rule_id in rank else ""
            out += [
                f"### [{i.rule_id}] {i.title}{star}",
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

    out += summary.stalls
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
