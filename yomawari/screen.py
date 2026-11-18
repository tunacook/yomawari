"""ランの画面の前提（--screen）。決めた大きさのウィンドウで起動し、ランのあいだは変えさせない（DESIGN.md §9「画面の前提」）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 起動の引数のうち、画面の大きさ・画面モードを決めるもの（Unity の Player の引数）。--screen では、これらを外して付け直す
_SCREEN_ARGS = ("-screen-width", "-screen-height", "-screen-fullscreen", "-window-mode")
_SCREEN_FLAGS = ("-popupwindow",)  # 値を取らないもの


class ScreenError(ValueError):
    pass


@dataclass(frozen=True)
class Screen:
    w: int  # 描画領域のピクセル数（Unity の -screen-width。Retina では、ウィンドウのポイントの倍率倍）
    h: int

    @property
    def mode(self) -> str:
        """ゲームが resolution の mode に出す値（Unity の FullScreenMode）。"""
        return "Windowed"

    def __str__(self) -> str:
        return f"{self.w}x{self.h} のウィンドウ"

    def args(self, args: list[str]) -> list[str]:
        """起動の引数から画面の大きさ・画面モードの指定を外し、この画面の指定を付けたもの。"""
        out: list[str] = []
        skip = False
        for a in args:
            if skip:
                skip = False
                continue
            if a in _SCREEN_ARGS:
                skip = True
                continue
            if a in _SCREEN_FLAGS:
                continue
            out.append(a)
        return [*out, "-screen-fullscreen", "0", "-screen-width", str(self.w), "-screen-height", str(self.h)]

    @staticmethod
    def guard_roles(roles: dict[str, str]) -> dict[str, str]:
        """ガードの役割ごとの区分（[guard.roles]）のうち、画面モード・解像度などを変えるボタン（役割 system_setting）を
        押さない（deny）にしたもの。イレギュラー操作でも押さない。"""
        return {**roles, "system_setting": "deny"}

    def matches(self, w: object, h: object, mode: object) -> bool:
        return w == self.w and h == self.h and mode == self.mode


def parse_screen(text: str) -> Screen | None:
    """「1920x1080」を読む。空なら None（画面を決めない）。"""
    text = text.strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", text)
    if m is None:
        raise ScreenError(f"画面の大きさは「1920x1080」の形で書いてください: {text}")
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        raise ScreenError(f"画面の大きさが 0 です: {text}")
    return Screen(w, h)
