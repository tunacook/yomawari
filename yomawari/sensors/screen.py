"""ゲームの画面のキャプチャと、ボタンの見た目の比べ方（DESIGN.md §9「画像による推定」）。

キャプチャは platform（OS ごと）が撮り、ここは OS を意識せずにピクセルだけを見る。
座標は描画領域に対する正規化座標（rect）で受け取り、キャプチャのピクセルに直す。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..state import Rect

# 1つのボタンで明るさを測る点の数（縦横それぞれ）。全ピクセルは見ない
_SAMPLES = 24


@dataclass(frozen=True)
class Shot:
    """描画領域のキャプチャ。ピクセルは 1 画素 4 バイトで、order の順に並ぶ（mac は BGRA）。"""

    width: int
    height: int
    bytes_per_row: int
    data: bytes
    order: str = "BGRA"


def brightness(shot: Shot, rect: Rect) -> tuple[float, float] | None:
    """rect の範囲の (平均の明るさ, 明るい部分の明るさ)。どちらも 0〜1。範囲が画面の外なら None。

    明るい部分は、測った点のうち明るい方から 1/10 の平均（文字はボタンの面積の 1〜2 割ほど）。画像ごと暗くしたボタンは平均に、
    文字だけ灰色にしたボタン（背景が透けている）は明るい部分に差が出る。
    """
    x0 = max(0, int(rect[0] * shot.width))
    y0 = max(0, int(rect[1] * shot.height))
    x1 = min(shot.width, int(rect[2] * shot.width))
    y1 = min(shot.height, int(rect[3] * shot.height))
    if x1 <= x0 or y1 <= y0:
        return None
    r, g, b = shot.order.index("R"), shot.order.index("G"), shot.order.index("B")
    nx, ny = min(_SAMPLES, x1 - x0), min(_SAMPLES, y1 - y0)
    values: list[float] = []
    for j in range(ny):
        y = y0 + (y1 - y0) * (2 * j + 1) // (2 * ny)
        row = y * shot.bytes_per_row
        for i in range(nx):
            x = x0 + (x1 - x0) * (2 * i + 1) // (2 * nx)
            p = row + x * 4
            values.append((0.299 * shot.data[p + r] + 0.587 * shot.data[p + g] + 0.114 * shot.data[p + b]) / 255.0)
    values.sort(reverse=True)
    top = values[: max(1, len(values) // 10)]
    return sum(values) / len(values), sum(top) / len(top)


def vividness(shot: Shot, rect: Rect) -> tuple[float, float] | None:
    """rect の範囲の (濃淡, 鮮やかさ)。どちらも 0〜1。範囲が画面の外なら None。

    濃淡は明るさの標準偏差、鮮やかさは各点の (RGB の最大 - 最小) の平均。半透明のボタンは後ろの絵が透けて混ざるので、
    明るさが同じでも、濃淡が小さく色が薄く写る（スクリーンショットからは透明度そのものは測れないので、見え方で推定する）。
    """
    x0 = max(0, int(rect[0] * shot.width))
    y0 = max(0, int(rect[1] * shot.height))
    x1 = min(shot.width, int(rect[2] * shot.width))
    y1 = min(shot.height, int(rect[3] * shot.height))
    if x1 <= x0 or y1 <= y0:
        return None
    r, g, b = shot.order.index("R"), shot.order.index("G"), shot.order.index("B")
    nx, ny = min(_SAMPLES, x1 - x0), min(_SAMPLES, y1 - y0)
    lum: list[float] = []
    sat = 0.0
    for j in range(ny):
        y = y0 + (y1 - y0) * (2 * j + 1) // (2 * ny)
        row = y * shot.bytes_per_row
        for i in range(nx):
            x = x0 + (x1 - x0) * (2 * i + 1) // (2 * nx)
            p = row + x * 4
            cr, cg, cb = shot.data[p + r], shot.data[p + g], shot.data[p + b]
            lum.append((0.299 * cr + 0.587 * cg + 0.114 * cb) / 255.0)
            sat += (max(cr, cg, cb) - min(cr, cg, cb)) / 255.0
    mean = sum(lum) / len(lum)
    var = sum((v - mean) ** 2 for v in lum) / len(lum)
    return var ** 0.5, sat / len(lum)
