"""状態トラッカーの Target（正規化座標の rect）を、画面座標に直して押す。

押す直前に毎回ウィンドウを取り直し（キャッシュしない）、ゲームが前面でなければ前面にする。
OS の差分は platform（ウィンドウ）と InputDriver（入力）に任せ、ここは OS を意識しない。
"""

from __future__ import annotations

import time

from ..platform import Platform
from ..sensors.window import ClientArea, client_area
from ..state import GameState, Target
from .base import InputDriver


class PointerError(Exception):
    """押せなかった理由。ゲームの不具合とは限らないので、呼び出し側で扱いを決める。"""


class Pointer:
    def __init__(
        self,
        platform: Platform,
        driver: InputDriver,
        pid: int,
        state: GameState,
        activate_wait: float = 0.3,
    ) -> None:
        self.platform = platform
        self.driver = driver
        self.pid = pid
        self.state = state
        self.activate_wait = activate_wait

    def area(self) -> ClientArea:
        info = self.platform.window_info(self.pid)
        if info is None:
            raise PointerError("ゲームのウィンドウが見つかりません")
        if not info.on_screen:
            raise PointerError("ゲームのウィンドウが画面に出ていません（最小化など）")
        res = self.state.resolution
        if not res or not res.get("w") or not res.get("h"):
            raise PointerError("resolution イベントがまだ来ていません")
        area = client_area(info.bounds, float(res["w"]), float(res["h"]))
        if area is None:
            raise PointerError(f"ウィンドウの外枠 {info.bounds} と解像度 {res['w']}x{res['h']} が合いません")
        return area

    def ensure_front(self) -> None:
        if self.platform.frontmost_pid() == self.pid:
            return
        self.platform.activate(self.pid)
        time.sleep(self.activate_wait)
        if self.platform.frontmost_pid() != self.pid:
            raise PointerError("ゲームを前面にできません")

    def click(self, target: Target, button: str = "left", clicks: int = 1) -> tuple[float, float]:
        """target を押し、押した画面座標を返す。押す直前に、target が最新の状態にまだあるか確かめる。"""
        current = self.state.find(target.key, target.nth)
        if current is None:
            raise PointerError(f"{target.key} は押す直前に画面から消えました")
        return self.click_at((current.rect[0] + current.rect[2]) / 2, (current.rect[1] + current.rect[3]) / 2,
                             button, clicks)

    def click_at(self, nx: float, ny: float, button: str = "left", clicks: int = 1) -> tuple[float, float]:
        """描画領域の正規化座標を押す（本文送りなど、ボタン以外の場所）。"""
        area = self.area()
        x, y = area.to_screen(nx, ny)
        self.ensure_front()
        self.driver.click(x, y, button=button, clicks=clicks)
        return (x, y)

    def scroll_at(self, nx: float, ny: float, amount: int) -> tuple[float, float]:
        area = self.area()
        x, y = area.to_screen(nx, ny)
        self.ensure_front()
        self.driver.scroll(x, y, amount)
        return (x, y)

    def key(self, name: str, times: int = 1) -> None:
        self.area()  # ウィンドウが出ていなければ押さない
        self.ensure_front()
        for _ in range(times):
            self.driver.key(name)
