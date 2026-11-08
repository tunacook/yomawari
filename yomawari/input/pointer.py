"""状態トラッカーの Target（正規化座標の rect）を、画面座標に直して押す。

押す直前に毎回ウィンドウを取り直し（キャッシュしない）、ゲームが前面でなければ前面にする。
OS の差分は platform（ウィンドウ）と InputDriver（入力）に任せ、ここは OS を意識しない。
"""

from __future__ import annotations

import time

from ..platform import Platform
from ..sensors.screen import Shot
from ..sensors.window import ClientArea, client_area
from ..state import GameState, Target
from .base import InputDriver


class PointerError(Exception):
    """押せなかった理由。ゲームの不具合とは限らないので、呼び出し側で扱いを決める。"""


class InputBlocked(PointerError):
    """OS がゲームへの入力を受け付けない（画面がロックされている、ゲームを前面にできない）。人が画面を戻すまで続く。"""


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
        locked = getattr(self.platform, "screen_locked", None)
        if locked is not None and locked():
            raise InputBlocked("画面がロックされています")
        if self.platform.frontmost_pid() == self.pid:
            return
        self.platform.activate(self.pid)
        time.sleep(self.activate_wait)
        front = self.platform.frontmost_pid()
        if front is None:
            # 分からないまま押すと、ほかのアプリに入力が届くことがあるので押さない
            raise InputBlocked("前面のアプリを OS から取得できません（ハーネスの不具合か、OS の変更の可能性があります）")
        if front != self.pid:
            raise InputBlocked("ゲームを前面にできません")

    def click(self, target: Target, button: str = "left", clicks: int = 1) -> tuple[float, float]:
        """target を押し、押した画面座標を返す。押す直前に、target が最新の状態にまだあるか確かめる。"""
        current = self.state.find(target.key, target.nth)
        if current is None:
            raise PointerError(f"{target.key} は押す直前に画面から消えました")
        if not current.visible:
            raise PointerError(f"{target.label} は押す直前に見えなくなりました（描画領域の外、スクロールで隠れた、ほかの UI に覆われた）")
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

    def drag(self, nx0: float, ny0: float, nx1: float, ny1: float) -> tuple[float, float]:
        """描画領域の正規化座標 (nx0, ny0) から (nx1, ny1) までドラッグし、始点の画面座標を返す。"""
        area = self.area()
        x0, y0 = area.to_screen(nx0, ny0)
        x1, y1 = area.to_screen(nx1, ny1)
        self.ensure_front()
        self.driver.drag(x0, y0, x1, y1)
        return (x0, y0)

    # カーソルを逃がす先の候補（描画領域の正規化座標）。ボタンに重ならない最初の点を使う
    _PARK = ((0.995, 0.005), (0.005, 0.005), (0.995, 0.995), (0.005, 0.995), (0.5, 0.005), (0.5, 0.995))

    def capture(self, hover_wait: float = 0.0) -> Shot | None:
        """描画領域を撮る（ほかのウィンドウが重ならないように、ゲームを前面にしてから）。

        押したあとのカーソルがボタンの上に残っていると、ホバーの演出でそのボタンだけ明るく写り、
        明るさの比較が狂う。撮る前にカーソルをボタンのない場所へ動かし、hover_wait 秒待つ。
        """
        area = self.area()
        self.ensure_front()
        rects = [b.rect for m in self.state.menus.values() for b in m.buttons] + [c.rect for c in self.state.choices]
        park = next(
            (p for p in self._PARK if not any(r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3] for r in rects)), None,
        )
        if park is not None:
            self.driver.move(*area.to_screen(*park))
            if hover_wait > 0:
                time.sleep(hover_wait)
        return self.platform.capture(area.rect)

    def key(self, name: str, times: int = 1) -> None:
        self.area()  # ウィンドウが出ていなければ押さない
        self.ensure_front()
        for _ in range(times):
            self.driver.key(name)
