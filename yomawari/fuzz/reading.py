"""ボタンの文字を画面から読む（OCR）（DESIGN.md §9「画面の文字を読む」）。

ボタンの文字は、UI の部品の作り（Text / TextMeshPro のどれを使っているか、ビルドの設定で型を探せるか）に頼らず、
人がボタンの文字を読むように、撮った画像の文字を OS の文字認識で読む。文字が画像に焼き込まれたボタンも読める。
文字の矩形の中心が入っているボタンの文字とする。
"""

from __future__ import annotations

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
