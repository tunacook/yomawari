"""状態トラッカーの Target（正規化座標の rect）を、画面座標に直して押す。

押す直前に毎回ウィンドウを取り直し（キャッシュしない）、ゲームが前面でなければ前面にする。
ウィンドウがディスプレイの見える範囲からはみ出していれば、人がするようにタイトルバーをドラッグして戻す。それでも
見える範囲の外にある点は押さない（DESIGN.md §8「ウィンドウが画面からはみ出したとき」）。
OS の差分は platform（ウィンドウ）と InputDriver（入力）に任せ、ここは OS を意識しない。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..platform import Platform
from ..sensors.screen import Shot
from ..sensors.window import FULLSCREEN_MODES, ClientArea, ScreenRect, client_area
from ..state import GameState, Target
from .base import InputDriver


class PointerError(Exception):
    """押せなかった理由。ゲームの不具合とは限らないので、呼び出し側で扱いを決める。"""


class InputBlocked(PointerError):
    """OS がゲームへの入力を受け付けない（画面がロックされている、ゲームを前面にできない）。人が画面を戻すまで続く。"""


class ScreenChanged(PointerError):
    """操作を決めてから入力を送るまでに、押せるもの（選択肢・ボタンのある画面）が変わったので送らなかった。"""


# タイトルバーをつかむとき、左端の閉じる・最小化・拡大のボタンを避ける幅（ポイント）
_TITLE_BUTTONS = 80.0
# ウィンドウが見える範囲に収まっているとみなす誤差（ポイント）
_FIT_TOLERANCE = 1.0


def _inside(r: ScreenRect, area: ScreenRect) -> bool:
    t = _FIT_TOLERANCE
    return (r.x >= area.x - t and r.y >= area.y - t and r.x + r.w <= area.x + area.w + t
            and r.y + r.h <= area.y + area.h + t)


def _shift(start: float, size: float, lo: float, span: float) -> float:
    """[start, start+size] を [lo, lo+span] に収める移動量。収まらなければ始まりを揃える。"""
    if size > span or start < lo:
        return lo - start
    return min(0.0, lo + span - (start + size))


@dataclass
class WindowFit:
    """ウィンドウが見える範囲からはみ出していたので、タイトルバーをドラッグして戻そうとした記録。"""

    before: ScreenRect
    after: ScreenRect | None  # 動かしたあとの外枠（取れなければ None）
    visible: ScreenRect  # ディスプレイの見える範囲
    fitted: bool  # 見える範囲に収まった


class Pointer:
    def __init__(
        self,
        platform: Platform,
        driver: InputDriver,
        pid: int,
        state: GameState,
        activate_wait: float = 0.3,
        fit_wait: float = 0.5,
        fit_tries: int = 3,
    ) -> None:
        self.platform = platform
        self.driver = driver
        self.pid = pid
        self.state = state
        self.activate_wait = activate_wait
        self.fit_wait = fit_wait  # タイトルバーをドラッグしたあと、ウィンドウを取り直すまで待つ秒数
        self.fit_tries = fit_tries  # 同じ大きさのウィンドウで、戻そうとする回数（OS が動かさないときに試し続けない）
        self.fits: list[WindowFit] = []  # 戻そうとした記録。FuzzSession が取り出してイベントとハーネスの記録にする
        self._fit_size: tuple[float, float] | None = None
        self._fit_count = 0
        self._visible: ScreenRect | None = None  # 最後に area() で見た、ディスプレイの見える範囲
        # 操作を決めたときの state.ui_seq。入れておくと、入力を送る直前に押せるものの並びが変わっていれば送らない
        # （run 2026-11-21_1512: 本文送りの Enter を送るあいだに選択肢が出て、フォーカスの当たった 1 つめを選び続けた）
        self.expect_ui: int | None = None

    def area(self) -> ClientArea:
        info = self.platform.window_info(self.pid)
        if info is None:
            raise PointerError("ゲームのウィンドウが見つかりません")
        if not info.on_screen:
            raise PointerError("ゲームのウィンドウが画面に出ていません（最小化など）")
        area = self._client(info.bounds)
        if self._fullscreen():
            # メニューバー・Dock は隠れていて、画面の端までゲーム。見える範囲（メニューバーを除く）で断らない
            self._visible = None
            return area
        visible_area = getattr(self.platform, "visible_area", None)
        self._visible = visible_area(info.bounds) if visible_area is not None else None
        if self._visible is not None and not _inside(info.bounds, self._visible):
            area = self._fit(info.bounds, area, self._visible)
        return area

    def _client(self, bounds: ScreenRect) -> ClientArea:
        res = self.state.resolution
        if not res or not res.get("w") or not res.get("h"):
            raise PointerError("resolution イベントがまだ来ていません")
        area = client_area(bounds, float(res["w"]), float(res["h"]), self._fullscreen())
        if area is None:
            raise PointerError(f"ウィンドウの外枠 {bounds} と解像度 {res['w']}x{res['h']} が合いません")
        return area

    def _fullscreen(self) -> bool:
        return (self.state.resolution or {}).get("mode") in FULLSCREEN_MODES

    def _fit(self, bounds: ScreenRect, area: ClientArea, visible: ScreenRect) -> ClientArea:
        """ウィンドウが見える範囲からはみ出している（ゲームの設定でウィンドウを大きくした、など）。人がするように、
        タイトルバーをドラッグして見える範囲に戻す。タイトルバーがない・見えない、同じ大きさで fit_tries 回戻せなかった
        ときは、そのままにする（見える範囲の外の点は、押すときに断る）。"""
        size = (bounds.w, bounds.h)
        if size != self._fit_size:
            self._fit_size, self._fit_count = size, 0
        title = area.rect.y - bounds.y
        if self._fit_count >= self.fit_tries or title < 4:
            return area
        lo = max(bounds.x + _TITLE_BUTTONS, visible.x + 4)
        hi = min(bounds.x + bounds.w - 20, visible.x + visible.w - 4)
        y0 = bounds.y + title / 2
        if lo > hi or not visible.y <= y0 <= visible.y + visible.h:
            return area
        self._fit_count += 1
        x0 = (lo + hi) / 2
        dx = _shift(bounds.x, bounds.w, visible.x, visible.w)
        dy = _shift(bounds.y, bounds.h, visible.y, visible.h)
        self.ensure_front()
        self.driver.drag(x0, y0, x0 + dx, y0 + dy)
        if self.fit_wait > 0:
            time.sleep(self.fit_wait)
        info = self.platform.window_info(self.pid)
        after = info.bounds if info is not None else None
        fitted = after is not None and _inside(after, visible)
        self.fits.append(WindowFit(bounds, after, visible, fitted))
        if fitted:
            self._fit_count = 0
        return self._client(after) if after is not None else area

    def _check_visible(self, x: float, y: float) -> None:
        """見える範囲の外の点は押さない（押しても OS がゲームに届けない。反応しないとも数えない）。"""
        v = self._visible
        if v is not None and not (v.x <= x <= v.x + v.w and v.y <= y <= v.y + v.h):
            raise PointerError(f"押す点 ({x:.0f}, {y:.0f}) がディスプレイの見える範囲の外です（ウィンドウが画面からはみ出している）")

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

    def _check_screen(self) -> None:
        """入力を送る直前: 操作を決めたあとに、押せるもの（選択肢・ボタンのある画面）が変わっていれば送らない。"""
        if self.expect_ui is not None and self.state.ui_seq != self.expect_ui:
            raise ScreenChanged("操作を決めたあとに、選択肢・ボタンのある画面が変わった")

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
        self._check_visible(x, y)
        self.ensure_front()
        self._check_screen()
        self.driver.click(x, y, button=button, clicks=clicks)
        return (x, y)

    def scroll_at(self, nx: float, ny: float, amount: int) -> tuple[float, float]:
        area = self.area()
        x, y = area.to_screen(nx, ny)
        self._check_visible(x, y)
        self.ensure_front()
        self._check_screen()
        self.driver.scroll(x, y, amount)
        return (x, y)

    def drag(self, nx0: float, ny0: float, nx1: float, ny1: float) -> tuple[float, float]:
        """描画領域の正規化座標 (nx0, ny0) から (nx1, ny1) までドラッグし、始点の画面座標を返す。"""
        area = self.area()
        x0, y0 = area.to_screen(nx0, ny0)
        x1, y1 = area.to_screen(nx1, ny1)
        self._check_visible(x0, y0)
        self.ensure_front()
        self._check_screen()
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
            self._check_screen()
            self.driver.key(name)
