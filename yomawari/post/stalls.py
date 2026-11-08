"""進めなかったところ（DESIGN.md §12「レポート構成」）。

events.jsonl から、新しい本文の行・scene・選択肢に届かないまま stall_report_sec 以上たった区間を探し、
そのあいだどこにいて、何を試し、どう終わったかをまとめる。ゲームの不具合なのか、ハーネスの進め方の問題なのかを、
人がレポートから切り分けられるようにするため（DESIGN.md §1「目的」）。判定はしない（手がかりだけを並べる）。

ゲームの流れ（fuzz/flow.py。Rachel の「質問に言葉で答える」で思い出を読むなど）の途中で届いた新しい本文は、
先へ進んだとはみなさない（思い出を読むたびに区間が切れて、同じ質問で 80 分止まったことが見えなくなるため。
run 2026-11-06_1012）。流れの中で新しく読んだ行の数は、手がかりとして出す。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Stall:
    start_wall: str
    end_wall: str
    sec: float
    launch: int
    scene: str | None
    line_id: str | None
    line_text: str
    ended: str  # どう終わったか
    ops: int = 0
    no_response: int = 0
    screens: Counter[str] = field(default_factory=Counter)  # 押した操作の画面（menu）→ 回数
    top_ops: Counter[str] = field(default_factory=Counter)  # 操作の label → 回数
    flows: Counter[str] = field(default_factory=Counter)  # 流れを始めた回数（流れの名前ごと）
    issues: Counter[str] = field(default_factory=Counter)  # 検出したルール → 回数
    restarts: int = 0
    reread: int = 0  # 読んだことのある本文を読み直した行の数
    loads: int = 0  # ロードした回数
    flow_new: int = 0  # ゲームの流れの途中で新しく読んだ行の数（思い出など）


def _place(ev: dict) -> str | None:
    d = ev.get("data") or {}
    k = ev.get("kind")
    if k == "line" and d.get("line_id") is not None:
        return f"line:{d['line_id']}"
    if k == "scene" and d.get("name"):
        return f"scene:{d['name']}"
    if k == "choices" and d.get("items"):
        return "choices:" + "|".join(str(c.get("text")) for c in d["items"] if isinstance(c, dict))
    return None


def _text(d: dict) -> str:
    text = " ".join(re.sub(r"<[^>]*>", "", str(d.get("text") or "")).split())
    return text[:40] + ("…" if len(text) > 40 else "")


def find_stalls(events: list[dict], min_sec: float) -> list[Stall]:
    """新しい本文・scene・選択肢に届かない区間のうち、min_sec 以上のもの（ランの最初の起動前は除く）。"""
    if min_sec <= 0:
        return []
    seen: set[str] = set()
    out: list[Stall] = []
    since: dict | None = None  # 最後に新しいところへ届いたイベント
    scene: str | None = None
    line: dict = {}
    at_since: tuple[str | None, dict] = (None, {})  # since のときの scene と行
    pending: list[dict] = []  # since からのイベント
    in_flow = False

    def close(ended: str, at: dict) -> None:
        nonlocal pending
        if since is not None:
            sec = float(at.get("t", 0)) - float(since.get("t", 0))
            if sec >= min_sec:
                sc, ln = at_since
                st = Stall(str(since.get("wall", "")), str(at.get("wall", "")), sec, int(since.get("launch", 0)),
                           sc, ln.get("line_id"), _text(ln), ended)
                last_op = None
                for e in pending:
                    k, d = e.get("kind"), e.get("data") or {}
                    if k == "op":
                        st.ops += 1
                        label = str(d.get("label") or "")
                        last_op = label
                        st.top_ops[label] += 1
                        m = re.match(r"\w+ ([^/ ]+)/", label)
                        st.screens[m.group(1) if m else "（画面のボタン以外）"] += 1
                    elif k == "op_result" and not d.get("responded", True):
                        st.no_response += 1
                    elif k == "flow" and (d.get("from") or {}).get("flow") is None:
                        st.flows[str(d.get("flow"))] += 1
                    elif k == "issue":
                        st.issues[str(d.get("rule_id"))] += 1
                    elif k == "launch":
                        st.restarts += 1
                    elif k == "load" and d.get("phase") == "start":
                        st.loads += 1
                    elif k == "line" and e.get("_reread"):
                        st.reread += 1
                    elif k == "line" and e.get("_flow_new"):
                        st.flow_new += 1
                if ended == "progress" and last_op:
                    st.ended = f"`{last_op}` のあと、新しいところへ進んだ"
                out.append(st)
        pending = []

    for ev in events:
        k = ev.get("kind")
        place = _place(ev)
        if k == "flow" and ev.get("source") == "harness":
            in_flow = (ev.get("data") or {}).get("flow") is not None
        if ev.get("source") == "game":
            if k == "scene":
                scene = (ev.get("data") or {}).get("name") or scene
            elif k == "line":
                line = ev.get("data") or {}
        if place is not None and place not in seen:
            seen.add(place)
            if in_flow:
                # 流れの途中（思い出を読むなど）の新しい本文は、先へ進んだとはみなさない
                ev = {**ev, "_flow_new": True}
            else:
                close("progress", ev)
                since, at_since = ev, (scene, line)
                continue
        elif place is not None and k == "line":
            ev = {**ev, "_reread": True}
        if since is None and k == "launch":
            since, at_since = ev, (scene, line)  # 起動してから最初に届くまで
            continue
        if since is not None:
            pending.append(ev)
    if since is not None and events:
        close("ランが終わるまで進まなかった", events[-1])
    return out


def load_events(path: Path) -> list[dict]:
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def _minutes(sec: float) -> str:
    return f"{sec / 60:.0f} 分" if sec >= 60 else f"{sec:.0f} 秒"


def stall_rows(stalls: list[Stall]) -> list[tuple[str, str]]:
    """「不具合の疑い」に載せる、区間ごとの (場所, 手がかり)。長い順。"""
    rows = []
    for st in sorted(stalls, key=lambda s: -s.sec):
        where = st.line_id or st.scene
        rows.append((f"`{where}`" if where else "-", f"{_minutes(st.sec)}。{st.ended}"))
    return rows


def render_stalls(stalls: list[Stall], min_sec: float) -> list[str]:
    """レポートの「進めなかったところ」の節。"""
    out = ["## 進めなかったところ", "",
           (f"新しい本文・scene・選択肢に {_minutes(min_sec)}以上届かなかった区間。ゲームの不具合（入力を受けない・"
            "先へ進む道がない）か、ハーネスの進め方の問題（答えが分からない・同じ画面を行き来している）かを、"
            "下の手がかりから切り分ける"), ""]
    if not stalls:
        return out + ["なし", ""]
    for n, st in enumerate(sorted(stalls, key=lambda s: -s.sec), 1):
        where = f"scene `{st.scene}`" if st.scene else "scene 不明"
        if st.line_id:
            where += f"、行 `{st.line_id}`「{st.line_text}」"
        out += [f"### {n}. {_minutes(st.sec)}（{st.start_wall} から、起動 #{st.launch}）", "",
                f"- 場所（最後に新しく届いたところ）: {where}",
                f"- 終わり方: {st.ended}"]
        if st.ops:
            rate = st.no_response / st.ops
            out.append(f"- 操作: {st.ops} 回（押しても反応しなかった {st.no_response} 回、{rate:.0%}）"
                       + ("。反応しない操作が多い: ゲームが入力を受けていない可能性" if rate >= 0.5 else ""))
            out.append("- 押した画面: " + "、".join(f"`{m}` {c} 回" for m, c in st.screens.most_common(5)))
            out.append("- 多かった操作: " + "、".join(f"`{o}` {c} 回" for o, c in st.top_ops.most_common(5)))
        if st.reread or st.loads:
            out.append(f"- 読んだことのある本文を読み直した: {st.reread} 行（ロード {st.loads} 回）"
                       + ("。ロード・巻き戻し・はじめからで前に戻り、同じところを読み直していた" if st.reread >= 20 else ""))
        if st.flow_new:
            out.append(f"- ゲームの流れの途中で新しく読んだ本文: {st.flow_new} 行（思い出など。先へ進んだとはみなさない）")
        if st.flows:
            out.append("- ゲームの流れ: " + "、".join(f"「{f}」を {c} 回始めた" for f, c in st.flows.most_common())
                       + "（同じ質問に何度も答えていれば、答えが見つかっていない。ハーネスの記録の「試せる組み合わせ」も見る）")
        if st.issues:
            out.append("- このあいだの検出: " + "、".join(f"{r} {c} 件" for r, c in st.issues.most_common()))
        if st.restarts:
            out.append(f"- このあいだの起動し直し: {st.restarts} 回")
        out.append("")
    return out
