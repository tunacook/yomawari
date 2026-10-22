"""ゲームウィンドウの位置と、正規化座標 → 画面座標の変換。変換はここに集約する（DESIGN.md §8）。

- 画面座標はポイント単位（メインディスプレイの左上原点）。OS の入力もポイントで行う
- ウィンドウの外枠（タイトルバー込み）は OS から取り（platform の window_info）、
  描画領域のピクセル数はゲームの resolution イベントから取る。外枠の幅とピクセル数の比が Retina の倍率
- 描画領域は外枠の下端に揃っている（上にタイトルバーがある）とみなす。ボーダーレスのフルスクリーンでは外枠と一致する
- 位置はキャッシュしない。操作のたびに取り直す
"""

from __future__ import annotations

from dataclasses import dataclass

from ..state import Rect


@dataclass(frozen=True)
class ScreenRect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class WindowInfo:
    pid: int
    bounds: ScreenRect  # 外枠（タイトルバー込み）
    on_screen: bool  # 最小化・別スペースなら False


@dataclass(frozen=True)
class ClientArea:
    rect: ScreenRect  # 描画領域（ポイント）
    scale: float  # 1 ポイントあたりのピクセル数

    def to_screen(self, nx: float, ny: float) -> tuple[float, float]:
        return (self.rect.x + nx * self.rect.w, self.rect.y + ny * self.rect.h)

    def center_of(self, r: Rect) -> tuple[float, float]:
        return self.to_screen((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


# 外枠の高さより描画領域が高くなる誤差の許容（ポイント）
_TOLERANCE = 1.0


def client_area(bounds: ScreenRect, res_w: float, res_h: float) -> ClientArea | None:
    """外枠と描画領域のピクセル数から描画領域を求める。辻褄が合わなければ None。"""
    if bounds.w <= 0 or bounds.h <= 0 or res_w <= 0 or res_h <= 0:
        return None
    scale = res_w / bounds.w
    content_h = res_h / scale
    if content_h > bounds.h + _TOLERANCE:
        return None
    content_h = min(content_h, bounds.h)
    top = bounds.y + bounds.h - content_h
    return ClientArea(ScreenRect(bounds.x, top, bounds.w, content_h), scale)
