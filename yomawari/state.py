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

    @property
    def key(self) -> str:
        """[guard.buttons] で使う "<menu の name>/<ボタンの name>"。"""
        return f"{self.menu}/{self.name}"


@dataclass
class Menu:
    name: str
    modal: bool
    buttons: list[Target]
    order: int  # 表示された順番（大きいほど新しい）


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
        for b in d.get("buttons") or []:
            r = _rect(b.get("rect")) if isinstance(b, dict) else None
            if r is not None:
                buttons.append(Target("button", name, str(b.get("name")), r))
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
        """いま押せるもの。モーダルな menu があれば、いちばん新しいモーダルのボタンだけ。"""
        top = self.top_modal()
        if top is not None:
            return list(top.buttons)
        out = list(self.choices)
        for m in sorted(self.menus.values(), key=lambda m: -m.order):
            out.extend(m.buttons)
        return out

    def find(self, key: str) -> Target | None:
        """"<menu>/<name>" で探す。押せるかどうか（モーダル）は問わない。"""
        for m in self.menus.values():
            for b in m.buttons:
                if b.key == key:
                    return b
        for c in self.choices:
            if c.key == key:
                return c
        return None

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
