"""pyautogui によるマウス・キーボード入力。

pyautogui の FAILSAFE（マウスを画面の隅に動かすと例外で止まる）は有効のままにする。人が止めたいときの非常口になる。
操作の間隔はハーネスが決めるので、pyautogui の既定の待ち（PAUSE）は 0 にする。
"""

from __future__ import annotations

from typing import Any


class PyAutoGuiDriver:
    def __init__(self) -> None:
        import pyautogui  # 権限やディスプレイがない環境でも、import だけでは落とさない

        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = True
        self._g: Any = pyautogui

    def move(self, x: float, y: float) -> None:
        self._g.moveTo(x, y)

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        self._g.click(x, y, clicks=clicks, interval=0.05, button=button)

    def scroll(self, x: float, y: float, amount: int) -> None:
        self._g.scroll(amount, x=x, y=y)

    def drag(self, x0: float, y0: float, x1: float, y1: float) -> None:
        # 一瞬で動かすと、ScrollRect がドラッグではなくクリックとして扱うことがある
        self._g.moveTo(x0, y0)
        self._g.mouseDown()
        self._g.moveTo(x1, y1, duration=0.3)
        self._g.mouseUp()

    def key(self, name: str) -> None:
        self._g.press(name)

    def hotkey(self, *names: str) -> None:
        self._g.hotkey(*names)
