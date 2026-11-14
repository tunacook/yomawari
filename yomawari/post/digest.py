"""レポートの先頭の「まとめ」「やること」と、項目ごとの節（DESIGN.md §12「レポート構成」）。

検出・進めなかった区間・ハーネスの記録を、同じ種類ごとに 1 つの項目にまとめる。項目ごとに次のものを並べる。

- 誰が直すか: 決まった根拠で判断できるときだけ書く（ゲームが出したログならゲーム、押したあとに画面の文字が変わって
  いたのに反応しないとしたならハーネスの誤検知の見込み）。ほかは「未確定」とし、原因の判定はしない
- 数字: 回数・答えたもの・プロファイルの正解の有無・読み直した回数など、人が原因を切り分けるための手がかり
- 経過: events.jsonl から、その時刻の前後を抜き出したもの（押した操作・本文・開いた画面・流れの手順・セーブとロード）

events.jsonl がないとき（テストなど）は、検出から分かる範囲で作る。
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..events import SEVERITIES, Issue
from .stalls import Stall, find_stalls

WHO_GAME = "ゲーム"
WHO_HARNESS = "ハーネス（誤検知の見込み）"
WHO_UNKNOWN = "未確定"
SEVERITY_LABEL = {"critical": "Critical", "major": "Major", "minor": "Minor", "info": "Info", "check": "要確認"}
RANK = {s: n for n, s in enumerate((*SEVERITIES[:2], "check", *SEVERITIES[2:]))}
LOG_RULES = ("L-01", "L-06")
BUTTON_RULES = ("S-01", "S-05")  # ボタンを押しても反応しない検出。押したボタンの並びごとにまとめる
SUSPECT_NOTE_WORDS = ("不具合の可能性", "進めない可能性")
NOTHING_TO_DO = "問題は見つからなかった。やることはない"
NOTHING_FOUND = ("検出・進めなかった区間・ハーネスが不具合の可能性を書いた記録・一度も反応しなかったボタンの、"
                 "どれもなかった")
SHOWN_ISSUES = 5  # 項目の「検出」に並べる数（同じ項目の検出がもっとあれば、数だけ書く）


@dataclass
class Item:
    """やることの 1 項目。"""

    title: str
    severity: str  # critical / major / minor / info / check（要確認）
    who: str = WHO_UNKNOWN
    basis: str = ""  # who の根拠（未確定なら空）
    what: str = ""  # 起きたこと（1 行）
    count: int = 0
    place: str = "-"
    numbers: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)
    timeline_label: str = ""


def family(label: str) -> str | None:
    """ボタンの並びの名前: "click SaveLoadUI/Page05" や "SaveLoadUI/Slot (Clone)#3" → "SaveLoadUI/Page"・"SaveLoadUI/Slot (Clone)"。
    同じ並びの、番号だけ違うボタンを 1 つの項目にまとめるため。"""
    m = re.match(r"(?:\w+ )?([^/ ]+)/(.+)$", label.strip())
    if m is None:
        return None
    name = re.sub(r"#\d+$", "", m.group(2))
    name = re.sub(r"\d+$", "", name).rstrip() or name
    return f"{m.group(1)}/{name}"


def _button_of(issue: Issue) -> str | None:
    """反応しないと検出したボタンの label（S-05 は key、S-01 は確かめた操作）。"""
    body = issue.key.split(":", 1)[1] if ":" in issue.key else ""
    if issue.rule_id == "S-05":
        return body or None
    parts = body.split("|")
    if len(parts) >= 3 and parts[1].startswith("click "):
        return parts[1][len("click "):]
    return None


def _item_key(issue: Issue) -> tuple[str, ...]:
    if issue.rule_id in BUTTON_RULES:
        button = _button_of(issue)
        fam = family(button) if button else None
        if fam:
            return ("button", fam)
    if issue.rule_id in LOG_RULES:
        kind = issue.evidence.get("kind") or issue.detail.strip().split("\n", 1)[0]
        return (issue.rule_id, str(kind))
    return (issue.rule_id,)


def place_of(issue: Issue) -> str:
    st = issue.evidence.get("state") or {}
    # 最後の本文のあとにコマンドが進んでいれば、スクリプトが実際にいるその行（本文のない演出の途中）
    where = issue.evidence.get("where") or st.get("command_line_id") or st.get("line_id") or st.get("scene")
    return f"`{where}`" if where else "-"


def _wall(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


class Log:
    """events.jsonl を時刻で引けるようにしたもの。wall は同じ書式（ISO 8601、同じ時差）なので、文字のまま並べて引く。"""

    def __init__(self, events: list[dict], cfg: dict[str, Any]):
        self.events = [e for e in events if e.get("wall")]
        self.walls = [str(e["wall"]) for e in self.events]
        self.cfg = cfg

    def index(self, wall: str) -> int:
        return bisect_left(self.walls, wall)

    def around(self, wall: str, before: float, after: float) -> tuple[int, int]:
        at = _wall(wall)
        if at is None:
            return 0, 0
        lo = (at - timedelta(seconds=before)).isoformat(timespec="milliseconds")
        hi = (at + timedelta(seconds=after)).isoformat(timespec="milliseconds")
        return bisect_left(self.walls, lo), bisect_right(self.walls, hi)

    def between(self, start: str, end: str) -> tuple[int, int]:
        return bisect_left(self.walls, start), bisect_right(self.walls, end)


def _short(text: Any, n: int = 30) -> str:
    s = " ".join(re.sub(r"<[^>]*>", "", str(text or "")).split())
    return s[:n] + ("…" if len(s) > n else "")


def _command_shown(name: str, keep: list[str]) -> bool:
    return name in keep or (name[:1].isupper())


def timeline(log: Log, lo: int, hi: int, limit: int, launch: int | None = None) -> list[str]:
    """events.jsonl の [lo, hi) を、人が読める経過にする。同じ行が続けば「×n」にまとめ、limit 行を超えれば、
    始めの 2/3 と終わりの 1/3 を残して間を省く。"""
    keep = list(log.cfg.get("report_timeline_commands") or [])
    rows: list[list[Any]] = []  # [時刻, 文, 回数]
    opened: set[str] = set()
    last_op: list[Any] | None = None

    def add(wall: str, text: str) -> list[Any]:
        if rows and rows[-1][1] == text:
            rows[-1][2] += 1
            return rows[-1]
        rows.append([wall[11:19], text, 1])
        return rows[-1]

    for e in log.events[lo:hi]:
        if launch is not None and e.get("launch") != launch:
            continue
        k, d, wall = e.get("kind"), e.get("data") or {}, str(e.get("wall"))
        if k == "op":
            tag = "（流れ）" if d.get("by") == "flow" else "（イレギュラー）" if d.get("irregular") else ""
            last_op = add(wall, f"押した `{d.get('label')}`{tag}")
        elif k == "op_result":
            if last_op is not None and not d.get("responded", d.get("screen_changed", True)) and "反応なし" not in last_op[1]:
                last_op[1] += " → 反応なし"
            last_op = None
        elif k == "line" and e.get("source") == "game":
            add(wall, f"本文 `{d.get('line_id')}`「{_short(d.get('text'))}」")
        elif k == "scene":
            add(wall, f"scene `{d.get('name')}`")
        elif k == "menu" and d.get("name") and d["name"] not in opened:
            opened.add(d["name"])
            add(wall, f"画面が開いた `{d['name']}`")
        elif k == "menu_close" and d.get("name"):
            opened.discard(d["name"])
            add(wall, f"画面が閉じた `{d['name']}`")
        elif k == "command" and d.get("name") and _command_shown(str(d["name"]), keep):
            target = f" {d['target']}" if d.get("target") else ""
            add(wall, f"コマンド @{d['name']}{target}（`{d.get('line_id')}`）")
        elif k == "flow":
            if d.get("flow"):
                add(wall, f"流れ「{d['flow']}」手順 {d.get('step')}: {d.get('note')}")
            elif (d.get("from") or {}).get("flow"):
                add(wall, f"流れ「{d['from']['flow']}」を終えた")
        elif k == "answer":
            picked = " → ".join(str(p) for p in d.get("picked") or [])
            known = "（プロファイルの正解に沿う）" if d.get("follows") else "（プロファイルの正解: なし）" if not d.get("known") else ""
            add(wall, f"答えた: {picked}{known}")
        elif k == "judged":
            add(wall, f"答えの判定: {'正解（先へ進んだ）' if d.get('right') else '外れ（同じ質問に戻った）'}")
        elif k in ("save", "load") and d.get("phase") == "start":
            add(wall, f"{'セーブ' if k == 'save' else 'ロード'} {d.get('slot')}")
        elif k == "issue":
            add(wall, f"検出 [{d.get('rule_id')}] {d.get('title')}")
        elif k == "launch":
            add(wall, "ゲームを起動した")
        elif k == "proc_exit":
            add(wall, f"ゲームが終了した（終了コード {d.get('code')}）")
        elif k == "choices":
            add(wall, f"選択肢が出た（{len(d.get('items') or [])} 個）")
    out = [f"{t} {text}" + (f" ×{n}" if n > 1 else "") for t, text, n in rows]
    if len(out) > limit > 0:
        head = limit * 2 // 3
        tail = limit - head
        out = [*out[:head], f"…（{len(out) - head - tail} 行省略）…", *out[-tail:]]
    return out


def _texts_of(e: dict, menu: str) -> dict[str, str]:
    return {k: v for k, v in ((e.get("data") or {}).get("texts") or {}).items() if k.startswith(menu + "/")}


def text_changed_after(log: Log, fam: str, ratio: float) -> tuple[int, int]:
    """fam の並びのボタンを押して反応しないとされたうち、押す前と後で、同じ画面のボタンの文字が変わっていた回数と、
    比べられた回数。押す前と後に、同じボタンの文字を読めていたときだけ比べる。押したボタン自身は除き、比べたボタンの
    ratio 以上の文字が変わっていれば、変わっていたとする。"""
    menu = fam.split("/", 1)[0]
    changed = compared = 0
    events = log.events
    for i, e in enumerate(events):
        if e.get("kind") != "op":
            continue
        label = str((e.get("data") or {}).get("label") or "")
        if not label.startswith("click ") or family(label) != fam:
            continue
        silent = next((not (r.get("data") or {}).get("responded", (r.get("data") or {}).get("screen_changed", True))
                       for r in events[i + 1:i + 200] if r.get("kind") == "op_result"), False)
        if not silent:
            continue
        before = next((_texts_of(x, menu) for x in reversed(events[max(0, i - 400):i])
                       if x.get("kind") == "read_text" and _texts_of(x, menu)), {})
        after: dict[str, str] = {}
        for x in events[i + 1:i + 400]:
            if x.get("kind") == "op":
                break
            if x.get("kind") == "read_text" and _texts_of(x, menu):
                after = _texts_of(x, menu)
                break
        same = [k for k in after if k in before and not label.endswith(k.split("/", 1)[1])]
        if not same:
            continue
        compared += 1
        diff = sum(1 for k in same if "".join(before[k].split()) != "".join(after[k].split()))
        if diff and diff >= ratio * len(same):
            changed += 1
    return changed, compared


def _before(log: Log, issue: Issue) -> list[str]:
    """最初の検出より前の、同じ起動の手がかり: 起動した時刻と、最後に始めたロード。経過の窓の外にあることが多いので、
    数字に書く（起動してすぐ、途中のセーブを読み込んで止まった、など）。"""
    started = load = None
    for e in log.events[:log.index(issue.wall)]:
        if e.get("launch") != issue.launch:
            continue
        if e.get("kind") == "launch":
            started, load = e, None
        elif e.get("kind") == "load" and (e.get("data") or {}).get("phase") == "start":
            load = e
    out = []
    if started is not None:
        at, now = _wall(str(started["wall"])), _wall(issue.wall)
        after = f"、{(now - at).total_seconds():.0f} 秒後に検出" if at and now else ""
        out.append(f"この起動を始めた時刻: {str(started['wall'])[11:19]}{after}")
    if load is not None:
        out.append(f"この起動で最後に始めたロード: {(load.get('data') or {}).get('slot')}（{str(load['wall'])[11:19]}）")
    return out


def _issue_items(issues: list[Issue], log: Log | None, cfg: dict[str, Any]) -> list[Item]:
    groups: dict[tuple[str, ...], list[Issue]] = {}
    for i in issues:
        groups.setdefault(_item_key(i), []).append(i)
    items = []
    for key, members in groups.items():
        first = members[0]
        severity = min((i.severity for i in members), key=lambda s: RANK.get(s, 99))
        places = list(dict.fromkeys(p for p in (place_of(i) for i in members) if p != "-")) or ["-"]
        item = Item(title=f"[{first.rule_id}] {first.title}", severity=severity, what=_short(first.detail, 200),
                    count=len(members), place=places[0] + (f" ほか {len(places) - 1} か所" if len(places) > 1 else ""),
                    issues=members)
        if key[0] == "button":
            rules = "・".join(sorted({i.rule_id for i in members}))
            item.title = f"[{rules}] `{key[1]}` を押しても反応しない"
            if log is not None:
                changed, compared = text_changed_after(log, key[1], float(cfg["report_text_change_ratio"]))
                if compared:
                    item.numbers.append(f"押して反応しなかったあと、同じ画面のボタンの文字が変わっていた: {changed}/{compared} 回")
                if changed and changed * 2 >= compared:
                    item.who = WHO_HARNESS
                    item.basis = (f"押したあと、同じ画面のボタンの文字が変わっていた（{changed}/{compared} 回）。"
                                  "画面は変わったのに、ハーネスが反応と見なせていない")
        elif first.rule_id in LOG_RULES:
            item.title += f": {_short(key[1], 60)}"
            item.who, item.basis = WHO_GAME, "ゲームが出したログ"
        elif first.evidence.get("dump"):
            item.who, item.basis = WHO_GAME, "クラッシュダンプがある"
        launches = sorted({i.launch for i in members})
        item.numbers.insert(0, f"回数: {len(members)}（起動 {', '.join(f'#{n}' for n in launches)}）")
        if log is not None:
            item.numbers += _before(log, first)
            lo, hi = log.around(first.wall, float(cfg["report_timeline_before_sec"]), float(cfg["report_timeline_after_sec"]))
            item.timeline = timeline(log, lo, hi, int(cfg["report_timeline_lines"]), first.launch)
            item.timeline_label = f"最初の検出（{first.wall[11:19]}、起動 #{first.launch}）の前後"
        items.append(item)
    return items


def _answers(log: Log, lo: int, hi: int) -> list[str]:
    """区間のあいだに答えたものの数字: 答えた回数、選んだもの（多い順）、プロファイルの正解、当たり外れ。"""
    answers = [e["data"] for e in log.events[lo:hi] if e.get("kind") == "answer"]
    judged = [e["data"] for e in log.events[lo:hi] if e.get("kind") == "judged"]
    out = []
    if answers:
        picked = Counter(" → ".join(str(p) for p in a.get("picked") or []) for a in answers)
        out.append(f"答えた: {len(answers)} 回（" + "、".join(f"{p} ×{n}" for p, n in picked.most_common(5))
                   + ("、ほか" if len(picked) > 5 else "") + "）")
        known = list(dict.fromkeys(k for a in answers for k in a.get("known") or []))
        if known:
            follows = sum(1 for a in answers if a.get("follows"))
            deliberate = sum(1 for a in answers if a.get("deliberate"))
            out.append(f"プロファイルの正解: 分かっていた（{' / '.join(known)}）。正解に沿って答えた {follows} 回"
                       + (f"、わざと外れを答えるつもりだった {deliberate} 回" if deliberate else ""))
        else:
            out.append("プロファイルの正解: なし（この質問の正解はプロファイルに書いていないか、質問の行を特定できなかった）")
    if judged:
        right = sum(1 for j in judged if j.get("right"))
        out.append(f"答えの判定: 正解 {right} 回、外れ（同じ質問に戻った） {len(judged) - right} 回")
    return out


def _stall_numbers(st: Stall, log: Log | None) -> list[str]:
    out = []
    if st.ops:
        out.append(f"操作: {st.ops} 回（押しても反応しなかった {st.no_response} 回）")
        out.append("多かった操作: " + "、".join(f"`{o}` ×{c}" for o, c in st.top_ops.most_common(5)))
    if st.reread or st.loads:
        out.append(f"読んだことのある本文を読み直した: {st.reread} 行、ロード: {st.loads} 回")
    if st.flow_new:
        out.append(f"ゲームの流れの途中で新しく読んだ本文: {st.flow_new} 行")
    if log is not None:
        lo, hi = log.between(st.start_wall, st.end_wall)
        steps = Counter(f"「{e['data'].get('flow')}」手順 {e['data'].get('step')}（{e['data'].get('note')}）"
                        for e in log.events[lo:hi] if e.get("kind") == "flow" and (e.get("data") or {}).get("flow"))
        if steps:
            out.append("流れの手順に入った回数: " + "、".join(f"{s} {c} 回" for s, c in steps.most_common(3)))
        out += _answers(log, lo, hi)
    if st.issues:
        out.append("このあいだの検出: " + "、".join(f"{r} {c} 件" for r, c in st.issues.most_common()))
    if st.restarts:
        out.append(f"このあいだの起動し直し: {st.restarts} 回")
    return out


def _minutes(sec: float) -> str:
    return f"{sec / 60:.0f} 分" if sec >= 60 else f"{sec:.0f} 秒"


def _stall_items(stalls: list[Stall], log: Log | None, cfg: dict[str, Any]) -> list[Item]:
    long = [s for s in stalls if s.sec >= float(cfg["report_stall_item_sec"])]
    short = [s for s in stalls if s not in long]
    items = []
    for st in sorted(long, key=lambda s: -s.sec):
        where = st.line_id or st.scene or "-"
        item = Item(title=f"`{where}` から {_minutes(st.sec)}進めなかった", severity="check",
                    what=f"{st.start_wall[11:19]} から{_minutes(st.sec)}、新しい本文・scene・選択肢に届かなかった。{st.ended}",
                    count=1, place=f"`{where}`" + (f"「{st.line_text}」" if st.line_text else ""),
                    numbers=_stall_numbers(st, log))
        if log is not None:
            lo, hi = log.between(st.start_wall, st.end_wall)
            item.timeline = timeline(log, max(0, lo - 1), hi, int(cfg["report_timeline_lines"]))
            item.timeline_label = "区間の始めと終わり"
        items.append(item)
    if short:
        total = sum(s.sec for s in short)
        items.append(Item(
            title=f"短く進めなかった区間 {len(short)} 件", severity="check", count=len(short),
            what=f"{_minutes(min(s.sec for s in short))}〜{_minutes(max(s.sec for s in short))}、計 {_minutes(total)}",
            numbers=[f"{_minutes(s.sec)}: `{s.line_id or s.scene}`（{s.start_wall[11:19]} から）。{s.ended}"
                     for s in sorted(short, key=lambda s: -s.sec)]))
    return items


def _silent_item(silent: dict[str, int]) -> Item | None:
    if not silent:
        return None
    fams = Counter()
    for label, n in silent.items():
        fams[family(label) or label] += n
    return Item(title=f"一度も反応しなかったボタン {len(silent)} 個", severity="check", count=len(silent),
                what="押しても画面が一度も変わらなかった。ロック中・空のスロットなど仕様どおりなら問題ない",
                numbers=[f"`{f}`: 押した {n} 回" for f, n in fams.most_common()])


def build_items(issues: list[Issue], events: list[dict], cfg: dict[str, Any], notes: list[str],
                silent: dict[str, int], focus: dict[str, int] | None = None) -> list[Item]:
    """やることの項目を、重い順に。同じ重さなら、ランの目的で重点的に見るルール（focus の順）、誰が直すかが分かっている
    もの（ゲーム）、未確定、誤検知の見込みの順。"""
    focus = focus or {}
    log = Log(events, cfg) if events else None
    items = _issue_items(issues, log, cfg)
    for it in items:
        if any(i.rule_id in focus for i in it.issues):
            it.title += "（重点）"
    if events:
        items += _stall_items(find_stalls(events, float(cfg["stall_report_sec"])), log, cfg)
    suspects = [n for n in notes if any(w in n for w in SUSPECT_NOTE_WORDS)]
    if suspects:
        items.append(Item(title=f"ハーネスが不具合の可能性を書いた記録 {len(suspects)} 件", severity="check",
                          count=len(suspects), what="ハーネスの推定。プロファイルが古いか、ゲームの不具合か、ハーネスの読み違いか",
                          numbers=suspects))
    s = _silent_item(silent)
    if s is not None:
        items.append(s)
    order = {WHO_GAME: 0, WHO_UNKNOWN: 1, WHO_HARNESS: 2}

    def first_focus(it: Item) -> int:
        return min((focus[i.rule_id] for i in it.issues if i.rule_id in focus), default=len(focus))

    # 検出は回数の多い順。進めなかった区間・記録などは作った順（長い区間が先）
    return sorted(items, key=lambda it: (RANK.get(it.severity, 99), first_focus(it), order.get(it.who, 1),
                                         -it.count if it.issues else 0))


def last_progress(events: list[dict]) -> dict | None:
    """最後に新しく届いた本文の行（ゲームの流れの途中で読んだものを除く）。"""
    seen: set[str] = set()
    in_flow = False
    last = None
    for e in events:
        k, d = e.get("kind"), e.get("data") or {}
        if k == "flow" and e.get("source") == "harness":
            in_flow = d.get("flow") is not None
        if k == "line" and e.get("source") == "game" and d.get("line_id") is not None:
            if d["line_id"] not in seen:
                seen.add(d["line_id"])
                if not in_flow:
                    last = e
    return last


def render_top(items: list[Item], events: list[dict], duration_sec: float, launches: int, restarts: int,
               stopped: str, cfg: dict[str, Any], error_before: Any, interrupted: bool = False) -> list[str]:
    """「まとめ」「やること」と項目ごとの節。項目がなければ、問題が見つからず、やることがないとはっきり書く。"""
    out = ["## まとめ", ""]
    hours = duration_sec / 3600
    out.append(f"- {hours:.1f} 時間、起動 {launches} 回（再起動 {restarts} 回）。{stopped}")
    last = last_progress(events)
    if last is not None:
        d = last["data"]
        out.append(f"- 最後に新しく届いたところ: 行 `{d.get('line_id')}`「{_short(d.get('text'))}」（{str(last['wall'])[11:16]}）")
    stalls = find_stalls(events, float(cfg["stall_report_sec"])) if events else []
    if stalls:
        st = max(stalls, key=lambda s: s.sec)
        share = f"（ランの {st.sec / duration_sec:.0%}）" if duration_sec > 0 else ""
        out.append(f"- いちばん長く進めなかった区間: {_minutes(st.sec)}{share}、`{st.line_id or st.scene}` から。{st.ended}")
    who = Counter(it.who for it in items)
    if items:
        out.append(f"- やること {len(items)} 件（"
                   + "、".join(f"{w} {who[w]}" for w in (WHO_GAME, WHO_UNKNOWN, WHO_HARNESS) if who[w]) + "）")
    else:
        out.append(f"- {NOTHING_TO_DO}" + ("（途中で止めたので、止めるまでの範囲）" if interrupted else ""))
    out.append("")
    out += ["## やること", ""]
    if not items:
        return out + [f"なし。{NOTHING_FOUND}", ""]
    out += [("同じ種類の検出・区間を 1 項目にまとめ、重い順に並べた。「誰が直す」は、決まった根拠で判断できたときだけ書く"
             "（ほかは未確定。下の節の数字と経過から切り分ける）"), ""]
    out += ["| # | 起きたこと | 重さ | 誰が直す | 回数 | 場所 |", "|---|---|---|---|---|---|"]
    for n, it in enumerate(items, 1):
        out.append(f"| {n} | {_cell(it.title)} | {SEVERITY_LABEL.get(it.severity, it.severity)} | {it.who} | {it.count} | "
                   f"{_cell(it.place)} |")
    out.append("")
    for n, it in enumerate(items, 1):
        out += [f"### {n}. {it.title}", "",
                f"- 重さ: {SEVERITY_LABEL.get(it.severity, it.severity)}",
                f"- 誰が直す: {it.who}" + (f"。根拠: {it.basis}" if it.basis else ""),
                f"- 起きたこと: {it.what}"]
        if it.place != "-":
            out.append(f"- 場所: {it.place}")
        if it.numbers:
            out.append("- 数字:")
            out += [f"  - {x}" for x in it.numbers]
        if it.issues:
            out.append("- 検出:")
            for i in it.issues[:SHOWN_ISSUES]:
                line = f"  - {i.wall[11:19]}（起動 #{i.launch}）{place_of(i)}: {_short(i.detail, 100)}"
                err = error_before(i)
                if err is not None:
                    line += f"。直前のエラーログ: {_short(err.detail, 80)}"
                if i.evidence.get("recent"):
                    line += "。直前の操作: " + " → ".join(f"`{r}`" for r in i.evidence["recent"][-5:])
                out.append(line)
            if len(it.issues) > SHOWN_ISSUES:
                out.append(f"  - ほか {len(it.issues) - SHOWN_ISSUES} 件")
            first = it.issues[0]
            st = first.evidence.get("state")
            if st:
                line = f"行 `{st.get('line_id')}`"
                if st.get("command_line_id"):
                    line = (f"行 `{st['command_line_id']}`（最後の本文は `{st.get('line_id')}`。"
                            "そのあとはコマンドだけが進んでいた）")
                out.append(f"- 最初の検出のときの画面: scene `{st.get('scene')}`、{line}、"
                           f"menu {', '.join(f'`{m}`' for m in st.get('menus') or []) or 'なし'}、"
                           f"最前面のモーダル `{st.get('top_modal')}`")
            if first.evidence.get("log"):
                out += ["- ログ抜粋:", "", "```text", str(first.evidence["log"]).rstrip("\n"), "```"]
            if first.evidence.get("dump"):
                out.append(f"- クラッシュダンプ: `{first.evidence['dump']}`")
            log_name = "player.log" if first.launch == 0 else f"player.{first.launch}.log"
            out.append(f"- ログ: [{log_name}]({log_name})")
        if it.timeline:
            out += [f"- 経過（{it.timeline_label}）:", "", "```text", *it.timeline, "```"]
        out += ["", "- 判定: [ ] 正しい検出　[ ] 誤検知", ""]
    return out


def _cell(text: str) -> str:
    line = str(text).strip().split("\n", 1)[0].replace("|", "\\|")
    return line if len(line) <= 120 else line[:120] + "…"
