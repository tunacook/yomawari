"""ゲームウィンドウの位置と、正規化座標 → 画面座標の変換。変換はここに集約する（DESIGN.md §8）。

- 画面座標はポイント単位（メインディスプレイの左上原点）。OS の入力もポイントで行う
- ウィンドウの外枠（タイトルバー込み）は OS から取り（platform の window_info）、
  描画領域のピクセル数はゲームの resolution イベントから取る。外枠の幅とピクセル数の比が Retina の倍率
- 描画領域は外枠の下端に揃っている（上にタイトルバーがある）とみなす。ボーダーレスのフルスクリーンでは外枠と一致する
- フルスクリーン（resolution の mode が FULLSCREEN_MODES）では、ゲームの画面がウィンドウ全体に引き伸ばされているとみなす。
  ノッチのある画面では、ウィンドウはノッチの下（例: 1512x949 ポイント）なのに、ゲームは画面全体の解像度（3024x1964）を
  出すので、縦横の比が合わない（run 2026-11-17）。外部モニタ（3360x1890 ポイント、1920x1080）では、撮った画面の文字が
  ボタンの位置に引き伸ばしで合うことを確かめた。ノッチのある画面で引き伸ばしか上が切れているかは確かめていない
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


# フルスクリーンの画面モード（Unity の FullScreenMode の名前）
FULLSCREEN_MODES = frozenset({"FullScreenWindow", "ExclusiveFullScreen"})

# 外枠の高さより描画領域が高くなる誤差の許容（ポイント）
_TOLERANCE = 1.0


def client_area(bounds: ScreenRect, res_w: float, res_h: float, fullscreen: bool = False) -> ClientArea | None:
    """外枠と描画領域のピクセル数から描画領域を求める。辻褄が合わなければ None。
    fullscreen なら、外枠全体を描画領域とする（縦横の比が合わなくても、引き伸ばされているとみなす）。"""
    if bounds.w <= 0 or bounds.h <= 0 or res_w <= 0 or res_h <= 0:
        return None
    scale = res_w / bounds.w
    if fullscreen:
        return ClientArea(bounds, scale)
    content_h = res_h / scale
    if content_h > bounds.h + _TOLERANCE:
        return None
    content_h = min(content_h, bounds.h)
    top = bounds.y + bounds.h - content_h
    return ClientArea(ScreenRect(bounds.x, top, bounds.w, content_h), scale)
