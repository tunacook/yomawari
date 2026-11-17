"""ボタンの文字を画面から読む（OCR）（DESIGN.md §9「画面の文字を読む」）。

ボタンの文字は、UI の部品の作り（Text / TextMeshPro のどれを使っているか、ビルドの設定で型を探せるか）に頼らず、
人がボタンの文字を読むように、撮った画像の文字を OS の文字認識で読む。文字が画像に焼き込まれたボタンも読める。
文字の矩形の中心が入っているボタンの文字とする。
"""

from __future__ import annotations

import difflib
import re

from ..state import Target

TextBox = tuple[str, float, tuple[float, float, float, float]]

MAX_TEXT = 40  # パッケージの text と同じく、この文字数で切る


def unread(targets: list[Target], done: set[str]) -> list[Target]:
    """まだ画面から文字を読んでいないボタン。"""
    return [t for t in targets if t.kind == "button" and t.visible and t.label not in done]


def assign(boxes: list[TextBox], targets: list[Target], min_conf: float) -> dict[str, str]:
    """文字の矩形の中心が入っているボタンに、その文字を割り当てる（上から、同じ行は左から順につなぐ）。

    ボタンが重なっているとき（並びの中のボタンと、それを囲むボタンなど）は、いちばん小さいボタンに割り当てる。
    """
    found: dict[str, list[tuple[float, float, str]]] = {}
    for text, conf, (x0, y0, x1, y1) in boxes:
        text = " ".join(text.split())
        if conf < min_conf or not text:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        inside = [t for t in targets if t.rect[0] <= cx <= t.rect[2] and t.rect[1] <= cy <= t.rect[3]]
        if not inside:
            continue
        t = min(inside, key=lambda b: (b.rect[2] - b.rect[0]) * (b.rect[3] - b.rect[1]))
        found.setdefault(t.label, []).append((round(y0, 2), x0, text))
    return {label: " ".join(s for _y, _x, s in sorted(parts))[:MAX_TEXT] for label, parts in found.items()}


def _row(label: str) -> str:
    """ボタンの並びの名前（"SaveLoadUI/Slot (Clone)#3"・"SaveLoadUI/Page05" → "SaveLoadUI/Slot (Clone)"・"SaveLoadUI/Page"）。"""
    name = re.sub(r"#\d+$", "", label)
    return re.sub(r"\d+$", "", name).rstrip() or name


def text_changed(before: dict[str, str], after: dict[str, str], ratio: float) -> bool:
    """押す前と後に読めた同じボタンの文字のうち、ratio 以上が変わっていたか（空白の違いは数えない）。

    ページを送る・タブを切り替えるボタンは、押しても画面の並び（ボタンの名前）は変わらず、スロットの文字だけが
    入れ替わる。1 つだけ変わったもの（読み取りの揺れ）は、ratio で外す。
    画面全体のほか、同じ並び（番号だけ違う名前のボタン。2 つ以上変わったもの）ごとにも比べる。ページのボタン・戻る
    ボタンのように、ページを替えても文字が変わらないボタンが多い画面では、全体の割合では足りないため
    （run 2026-11-16_2241: セーブ画面でページを替えてスロット 9 個の文字が変わったのに、全体の 22 個の半分に届かず
    「反応しない」とした）。"""
    same = [k for k in after if k in before]
    if not same:
        return False
    changed = {k for k in same if "".join(before[k].split()) != "".join(after[k].split())}
    if changed and len(changed) >= ratio * len(same):
        return True
    # 並びごとに比べるときは、読み取りの揺れ（「Empty」→「Emptw」など）を数えないよう、はっきり変わったものだけ数える
    clear = {k for k in changed if _clearly_changed(before[k], after[k])}
    rows: dict[str, list[str]] = {}
    for k in same:
        rows.setdefault(_row(k), []).append(k)
    return any(len(c := [k for k in keys if k in clear]) >= 2 and len(c) >= ratio * len(keys)
               for keys in rows.values())


def _clearly_changed(a: str, b: str) -> bool:
    """数字（スロットの番号・日時）が変わったか、文字の半分以上が違う。"""
    a, b = "".join(a.split()), "".join(b.split())
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() < 0.5
