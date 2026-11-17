"""レポートの「設定」の節（DESIGN.md §12「レポート構成」、§9「設定を変えに行く」）。

Naninovel の標準の設定画面にある項目ごとに、プロファイルでの扱い（変えさせる・外した）と、このランで変わった回数、
最後の値を並べる。ゲームが標準の項目のどれを残し、どれをあえて外したかを、ランのたびに確かめられるようにする。
値はパッケージの settings から読む。変わった回数は起動ごとに数える（起動し直して値が戻ったかは F-03 で見る）。
"""

from __future__ import annotations

from typing import Any

from ..fuzz.settings_tour import SETTING_ITEMS
from .digest import _cell

_SKIP = {"locales", "seq", "t", "f", "ev"}


def _value(v: Any) -> str:
    if v is None:
        return "—"
    if v == "":
        return "（空）"  # 空のコード（``）は、Markdown で記号のまま見える（Naninovel の既定のフォント名など）
    return f"`{_cell(str(v))}`"


def render(events: list[dict], offered: list[str] | None) -> list[str]:
    """「設定」の節の行。ゲームが settings を一度も出していなければ空。"""
    changes: dict[str, int] = {}
    last: dict[str, Any] = {}
    prev: dict[str, Any] | None = None
    launch = None
    seen = False
    for e in events:
        if e.get("source") != "game" or e.get("kind") != "settings":
            continue
        seen = True
        values = {k: v for k, v in (e.get("data") or {}).items() if k not in _SKIP}
        if e.get("launch") != launch:
            launch, prev = e.get("launch"), None  # 起動し直したあとの最初の値は、変わったと数えない
        for k, v in values.items():
            changes.setdefault(k, 0)
            if prev is not None and prev.get(k) != v:
                changes[k] += 1
            last[k] = v
        prev = values
    if not seen:
        return []
    keys = list(SETTING_ITEMS) + sorted(k for k in changes if k not in SETTING_ITEMS)
    out = [
        "## 設定", "",
        "Naninovel の標準の設定画面にある項目ごとの、プロファイル（`[settings] offered`）での扱いと、このランで変わった回数"
        + ("" if offered is not None else "。プロファイルに書いていないので、ゲームが値を出した項目をすべて変えさせるものとして扱った"),
        "",
        "| 項目 | 名前 | プロファイル | このランで変わった | 最後の値 |", "|---|---|---|---|---|",
    ]
    for k in keys:
        mark = "—" if offered is None else "変えさせる" if k in offered else "外した"
        count = f"{changes[k]} 回" if k in changes else "値が出ていない"
        out.append(f"| {SETTING_ITEMS.get(k, k)} | `{k}` | {mark} | {count} | {_value(last.get(k))} |")
    out.append("")
    if offered is not None:
        never = [k for k in offered if changes.get(k) == 0]
        removed = [k for k in changes if k not in offered and changes[k] > 0]
        if never:
            out.append("- 変えさせる項目のうち、このランで一度も変わらなかったもの: " + "、".join(f"`{k}`" for k in never))
        if removed:
            out.append("- 外した項目なのに、このランで変わったもの: " + "、".join(f"`{k}`" for k in removed)
                       + "（外したはずの設定を変える口が残っているか、ほかの操作で値が動いた）")
        if never or removed:
            out.append("")
    return out
