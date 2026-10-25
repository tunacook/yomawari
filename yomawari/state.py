"""状態トラッカー。ゲームのイベント（[YOMAWARI] 行）から、いま画面に何が出ているかを保つ。

操作の選択（Decider）とガードはこの状態だけを見る。座標は rect のまま（正規化座標）で持ち、
画面座標への変換は押す直前に input/pointer.py で行う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event

Rect = tuple[float, float, float, float]  # 正規化座標 [x0, y0, x1, y1]、左上原点


def _rect(v: Any) -> Rect | None:
    if not isinstance(v, list) or len(v) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(n) for n in v)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


@dataclass(frozen=True)
class Target:
    """押せるもの。kind は button（menu のボタン）か choice（選択肢）。"""

    kind: str
    menu: str  # choice のときは "choices"
    name: str  # button は GameObject 名、choice は選択肢の id
    rect: Rect
    text: str | None = None
    index: int | None = None
    nth: int = 0  # 同じ menu に同じ名前のボタンが複数あるとき、何番目か（セーブスロットなど）

    @property
    def key(self) -> str:
        """[guard.buttons] で使う "<menu の name>/<ボタンの name>"。"""
        return f"{self.menu}/{self.name}"

    @property
    def label(self) -> str:
        """同じ名前のボタンを区別する名前。操作の記録と試行回数の数え方に使う。

        選択肢は本文で表す（Naninovel の選択肢の id は表示のたびに変わり、周回をまたいで数えられないため）。
        """
        if self.kind == "choice" and self.text:
            return f"{self.menu}/{self.text}"
        return f"{self.key}#{self.nth}" if self.nth else self.key

    @property
    def visible(self) -> bool:
        """中心が描画領域の中にあるか。スクロールで隠れた項目は、ゲームが矩形を画面の外のまま出してくることがある。"""
        cx, cy = (self.rect[0] + self.rect[2]) / 2, (self.rect[1] + self.rect[3]) / 2
        return 0 <= cx <= 1 and 0 <= cy <= 1


@dataclass
class Menu:
    name: str
    modal: bool
    buttons: list[Target]
    order: int  # 表示された順番（大きいほど新しい）

    @property
    def visible_buttons(self) -> list[Target]:
        return [b for b in self.buttons if b.visible]

    @property
    def area(self) -> Rect | None:
        """ボタン全体を囲む矩形を描画領域で切り取ったもの。スクロールの操作をこの上でする。"""
        if not self.buttons:
            return None
        x0 = max(0.0, min(b.rect[0] for b in self.buttons))
        y0 = max(0.0, min(b.rect[1] for b in self.buttons))
        x1 = min(1.0, max(b.rect[2] for b in self.buttons))
        y1 = min(1.0, max(b.rect[3] for b in self.buttons))
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


@dataclass
class GameState:
    launch: int = -1
    booted: bool = False
    scene: str | None = None
    line: dict[str, Any] | None = None
    line_done: bool = False
    menus: dict[str, Menu] = field(default_factory=dict)
    choices: list[Target] = field(default_factory=list)
    transitions: set[str] = field(default_factory=set)
    resolution: dict[str, Any] | None = None
    saving: bool = False
    loading: bool = False
    last_game_t: float | None = None
    last_progress_t: float | None = None  # scene / line / choices が来た時刻
    _order: int = 0

    def feed(self, ev: Event) -> None:
        if ev.source == "harness" and ev.kind == "launch":
            # 起動し直したら状態を捨てる
            self.__dict__.update(vars(GameState(launch=ev.launch)))
            return
        if ev.source != "game":
            return
        self.last_game_t = ev.t
        d = ev.data
        handler = getattr(self, f"_on_{ev.kind}", None)
        if handler is not None:
            handler(d, ev.t)

    # --- イベントごと ---

    def _on_boot(self, d: dict[str, Any], t: float) -> None:
        self.booted = True

    def _on_resolution(self, d: dict[str, Any], t: float) -> None:
        self.resolution = {"w": d.get("w"), "h": d.get("h"), "mode": d.get("mode")}

    def _on_scene(self, d: dict[str, Any], t: float) -> None:
        self.scene = d.get("name")
        self.last_progress_t = t

    def _on_line(self, d: dict[str, Any], t: float) -> None:
        self.line = {k: d.get(k) for k in ("label", "line_id", "speaker", "text")}
        self.line_done = False
        self.last_progress_t = t

    def _on_line_done(self, d: dict[str, Any], t: float) -> None:
        self.line_done = True

    def _on_menu(self, d: dict[str, Any], t: float) -> None:
        name = str(d.get("name"))
        buttons = []
        seen: dict[str, int] = {}
        for b in d.get("buttons") or []:
            r = _rect(b.get("rect")) if isinstance(b, dict) else None
            if r is not None:
                bname = str(b.get("name"))
                buttons.append(Target("button", name, bname, r, nth=seen.get(bname, 0)))
                seen[bname] = seen.get(bname, 0) + 1
        prev = self.menus.get(name)
        if prev is None:
            self._order += 1
        self.menus[name] = Menu(name, bool(d.get("modal")), buttons, prev.order if prev else self._order)

    def _on_menu_close(self, d: dict[str, Any], t: float) -> None:
        self.menus.pop(str(d.get("name")), None)

    def _on_choices(self, d: dict[str, Any], t: float) -> None:
        items = []
        for i, c in enumerate(d.get("items") or []):
            r = _rect(c.get("rect")) if isinstance(c, dict) else None
            if r is None or c.get("locked"):
                continue
            items.append(Target("choice", "choices", str(c.get("id")), r, c.get("text"), i))
        self.choices = items
        self.last_progress_t = t

    def _on_choices_done(self, d: dict[str, Any], t: float) -> None:
        self.choices = []

    def _on_transition(self, d: dict[str, Any], t: float) -> None:
        name = str(d.get("name"))
        if d.get("phase") == "start":
            self.transitions.add(name)
        else:
            self.transitions.discard(name)

    def _on_save(self, d: dict[str, Any], t: float) -> None:
        self.saving = d.get("phase") == "start"

    def _on_load(self, d: dict[str, Any], t: float) -> None:
        self.loading = d.get("phase") == "start"

    # --- 参照 ---

    @property
    def busy(self) -> bool:
        """暗転・セーブ・ロードの途中。通常の操作は待つ。"""
        return bool(self.transitions) or self.saving or self.loading

    def top_modal(self) -> Menu | None:
        modals = [m for m in self.menus.values() if m.modal]
        return max(modals, key=lambda m: m.order) if modals else None

    def targets(self) -> list[Target]:
        """いま押せるもの。モーダルな menu があれば、いちばん新しいモーダルのボタンだけ。画面の外のボタンは除く。"""
        top = self.top_modal()
        if top is not None:
            return top.visible_buttons
        out = [c for c in self.choices if c.visible]
        for m in sorted(self.menus.values(), key=lambda m: -m.order):
            out.extend(m.visible_buttons)
        return out

    def find(self, key: str, nth: int = 0) -> Target | None:
        """"<menu>/<name>" で探す（同じ名前が複数あれば nth 番目）。押せるかどうか（モーダル）は問わない。"""
        for m in self.menus.values():
            for b in m.buttons:
                if b.key == key and b.nth == nth:
                    return b
        for c in self.choices:
            if c.key == key:
                return c
        return None

    def signature(self) -> tuple[Any, ...]:
        """画面の状態の要約。操作のあとにこれが変われば「反応があった」とみなす。

        ボタンの矩形は含めない（マウスを載せただけで変わることがあるため）。見えているボタンの並びは含める
        （スクロールすると変わる）。
        """
        return (
            self.scene,
            self.line["line_id"] if self.line else None,
            self.line_done,
            tuple(sorted((m.name, m.modal, tuple(b.label for b in m.visible_buttons)) for m in self.menus.values())),
            tuple(c.key for c in self.choices),
            self.busy,
        )

    def snapshot(self) -> dict[str, Any]:
        top = self.top_modal()
        return {
            "scene": self.scene,
            "line_id": self.line["line_id"] if self.line else None,
            "line_done": self.line_done,
            "menus": sorted(self.menus),
            "top_modal": top.name if top else None,
            "choices": len(self.choices),
            "busy": self.busy,
        }
