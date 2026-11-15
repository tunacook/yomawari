"""レポートの「カバレッジ」の節（DESIGN.md §12「レポート構成」）。

events.jsonl から、ランで着いた scene と本文の行、出た画面と押したボタン、試した「状態×操作」の組、出た選択肢と
選んだものを数える。ゲームのスクリプト全体の大きさはハーネスに分からないので、割合ではなく、着いたものの数で出す。
状態は、操作したときにいちばん上に開いていたモーダルな画面（menu）の名前。選択肢が出ていれば「選択肢」、どちらもなければ「本文」
（いつも出ているモーダルでない画面は、状態に入れない。本文を読んでいるあいだも出ているため）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TEXT_STATE = "本文"
CHOICE_STATE = "選択肢"


@dataclass
class _Menu:
    buttons: set[str] = field(default_factory=set)  # 出たボタン（state.py と同じく、同じ名前の 2 つめから #n を付ける）
    pressed: set[str] = field(default_factory=set)


@dataclass
class Coverage:
    scenes: dict[str, float] = field(default_factory=dict)  # scene → 初めて着いた時刻（ランの始めからの秒）
    lines: dict[str, set[str]] = field(default_factory=dict)  # scene（本文の label）→ 表示した行
    menus: dict[str, _Menu] = field(default_factory=dict)
    pairs: Counter = field(default_factory=Counter)  # (状態, 操作) → 回数
    choices: dict[tuple[str, ...], set[int]] = field(default_factory=dict)  # 選択肢の並び → 選んだ番号


def _button_labels(menu: str, buttons: list[Any]) -> list[str]:
    seen: Counter = Counter()
    out = []
    for b in buttons:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name"))
        nth = seen[name]
        seen[name] += 1
        out.append(f"{menu}/{name}#{nth}" if nth else f"{menu}/{name}")
    return out


def _target(label: str) -> str | None:
    """操作の label（`click 画面/ボタン` など）が押したボタン。"""
    _, _, rest = label.partition(" ")
    return rest if "/" in rest else None


def collect(events: list[dict]) -> Coverage:
    cov = Coverage()
    walls = [e for e in events if e.get("wall")]
    start = walls[0]["wall"] if walls else None
    modals: list[str] = []  # 開いているモーダルな画面（あとに開いたものが後ろ）
    pending: tuple[str, ...] | None = None  # いま出ている選択肢
    last: tuple[str, ...] | None = None  # 最後に出た選択肢

    def elapsed(e: dict) -> float:
        try:
            return (datetime.fromisoformat(e["wall"]) - datetime.fromisoformat(start)).total_seconds()
        except (TypeError, ValueError, KeyError):
            return 0.0

    for e in events:
        kind, d = e.get("kind"), e.get("data") or {}
        if e.get("source") == "game":
            if kind in ("scene", "load"):
                pending = None  # ロード・scene の切り替えで消えた選択肢には、空の choices が来ないことがある
            if kind == "scene" and d.get("name"):
                cov.scenes.setdefault(str(d["name"]), elapsed(e))
            elif kind == "line" and d.get("line_id"):
                cov.lines.setdefault(str(d.get("label") or "?"), set()).add(str(d["line_id"]))
            elif kind == "menu" and d.get("name"):
                name = str(d["name"])
                labels = _button_labels(name, d.get("buttons") or [])
                if name in modals:
                    modals.remove(name)
                if d.get("modal"):
                    modals.append(name)
                if labels:
                    cov.menus.setdefault(name, _Menu()).buttons.update(labels)
            elif kind == "menu_close" and d.get("name") in modals:
                modals.remove(d["name"])
            elif kind == "choices":
                items = tuple(str(i.get("text", "")) for i in d.get("items") or [] if isinstance(i, dict))
                pending = items or None  # 空の choices は、選択肢が消えたという知らせ
                if items:
                    last = items
                    cov.choices.setdefault(items, set())
            elif kind == "choices_done" and last is not None and isinstance(d.get("index"), int):
                # 選んだ選択肢の本文の行と空の choices が、choices_done より先に届くことがある
                cov.choices[last].add(d["index"])
        elif e.get("source") == "harness":
            if kind == "launch":
                modals.clear()
                pending = None
            elif kind == "op" and d.get("label"):
                label = str(d["label"])
                state = modals[-1] if modals else CHOICE_STATE if pending is not None else TEXT_STATE
                cov.pairs[(state, label)] += 1
                target = _target(label)
                if target is not None:
                    cov.menus.setdefault(target.split("/", 1)[0], _Menu()).pressed.add(target)
    return cov


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _names(items: list[str], limit: int) -> str:
    shown = ", ".join(f"`{x}`" for x in items[:limit])
    return shown + (f" ほか {len(items) - limit}" if len(items) > limit else "")


def render(events: list[dict], cfg: dict[str, Any]) -> list[str]:
    """「カバレッジ」の節の行。記録に本文も操作もなければ空。"""
    cov = collect(events)
    if not (cov.scenes or cov.lines or cov.pairs):
        return []
    rows = int(cfg.get("report_coverage_rows", 30))
    lines_total = sum(len(v) for v in cov.lines.values())
    chosen = sum(len(v) for v in cov.choices.values())
    states = Counter(s for s, _ in cov.pairs)
    menus = {k: m for k, m in cov.menus.items() if m.buttons}  # 選択肢など、ボタンを出さない画面は除く
    out = [
        "## カバレッジ", "",
        "このランで着いたもの・試したもの。ゲームの全体の大きさは分からないので、数だけを出す", "",
        f"- scene: {len(cov.scenes)} 個、本文の行: {lines_total} 行（同じ行は 1 つに数える）",
        f"- 選択肢: {len(cov.choices)} 種類が出て、選んだのはのべ {chosen} 通り",
        f"- 画面: {len(menus)} 個。ボタン {sum(len(m.buttons) for m in menus.values())} 個のうち、"
        f"押したのは {sum(len(m.pressed & m.buttons) for m in menus.values())} 個",
        f"- 状態×操作: {len(cov.pairs)} 組（状態 {len(states)} 個。状態は、操作したときにいちばん上にあったモーダルな画面。"
        f"なければ「{CHOICE_STATE}」か「{TEXT_STATE}」）",
        "",
    ]
    if cov.scenes or cov.lines:
        names = sorted(set(cov.scenes) | set(cov.lines), key=lambda n: (cov.scenes.get(n, float("inf")), n))
        out += ["### scene", "", "| scene | 本文の行 | 初めて着いた |", "|---|---|---|"]
        for n in names[:rows]:
            when = _hms(cov.scenes[n]) if n in cov.scenes else "—"
            out.append(f"| `{n}` | {len(cov.lines.get(n, ()))} | {when} |")
        if len(names) > rows:
            out.append(f"| ほか {len(names) - rows} 個 | | |")
        out.append("")
    if menus:
        out += ["### 画面とボタン", "", "| 画面 | ボタン | 押した | 押していないボタン |", "|---|---|---|---|"]
        ordered = sorted(menus.items(), key=lambda kv: (-len(kv[1].buttons - kv[1].pressed), kv[0]))
        for name, m in ordered[:rows]:
            rest = sorted(m.buttons - m.pressed)
            out.append(f"| `{name}` | {len(m.buttons)} | {len(m.pressed & m.buttons)} | {_names(rest, 5) if rest else '—'} |")
        if len(ordered) > rows:
            out.append(f"| ほか {len(ordered) - rows} 個 | | | |")
        out.append("")
    if cov.pairs:
        out += ["### 状態×操作", "", "| 状態 | 操作の種類 | 操作の回数 |", "|---|---|---|"]
        ops: Counter = Counter()
        for (st, _), c in cov.pairs.items():
            ops[st] += c
        for st, n in ops.most_common()[:rows]:
            out.append(f"| `{st}` | {states[st]} | {n} |")
        if len(states) > rows:
            out.append(f"| ほか {len(states) - rows} 個 | | |")
        out.append("")
    return out
