"""一覧の台帳と目標（DESIGN.md §9「一覧の台帳と目標」）。

回想・ギャラリー・セーブ・言葉の一覧のように、同じ種類のボタンがスクロール領域に並ぶ画面で、人は、まず一通り眺めて
「どれが開いていて、どれが伏せ字か」を覚え、選ぶものを決めたら、そこまでまっすぐスクロールして押す。押して反応がなければ
「これは押せない」と覚え、次の候補へ向かう。ハーネスも同じように、一覧ごとに台帳を持ち、押すもの（または見にいくもの）を
目標として1つ決めて、そこへ向かう。

前は、一覧を動かす操作を出す仕組みが5つあり、操作のたびにそれぞれが候補を出してランダムで1つを選んでいたので、2つの
仕組みが逆の向きを出すと行き来した（run 2026-11-06_2334・2026-11-07_0849）。いまは、一覧を動かすのは目標へ向かう
操作だけにする。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..state import GameState, Menu, Rect, Target
from .looks import masked
from .ops import Op

UNKNOWN, OK, NO, MASKED = "unknown", "ok", "no", "masked"
STATUS_JA = {UNKNOWN: "未確認", OK: "押せた", NO: "押せない", MASKED: "伏せ字"}

# 目標のある向き → そちらを表示範囲に入れる操作（下にあるなら、ホイールを下へ・上へドラッグ・下キー）
REVEAL: dict[str, list[tuple[str, str | int]]] = {
    "down": [("wheel_down", 3), ("drag", "up"), ("key", "down")],
    "up": [("wheel_up", 3), ("drag", "down"), ("key", "up")],
    "right": [("drag", "left"), ("key", "right")],
    "left": [("drag", "right"), ("key", "left")],
}
WHOLE: Rect = (0.0, 0.0, 1.0, 1.0)


@dataclass
class Item:
    """台帳の項目（一覧のボタン1つ）。"""

    label: str
    menu: str
    name: str
    rect: Rect  # 最後に受け取った矩形
    text: str | None = None  # 見えたときに画面から読んだ文字（最後に読んだもの）
    readings: list[str] = field(default_factory=list)  # これまでに読んだ文字（重なりなし）。読むたびに番号だけ・題名だけと変わるため
    status: str = UNKNOWN  # 押した結果で決める（見た目では変えない）
    shown: bool = False  # 画面を開いてから、見えたことがあるか（閉じたら False に戻す）


@dataclass(frozen=True)
class Row:
    """一覧（同じ画面で、同じ名前のボタンが3つ以上あるもの）。"""

    menu: str
    name: str
    view: Rect  # 表示される範囲（menu の scroll の矩形。なければ描画領域）
    labels: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.menu}/{self.name}"


@dataclass
class Goal:
    """一覧の目標。purpose: pick（押す）/ fill（見えるところまで動かして文字を読む。押さない）。"""

    label: str
    purpose: str
    by: str  # 目標を決めた仕組み（list / flow）
    dist: float = 0.0  # 最後に測った、表示範囲から目標までの距離
    misses: int = 0  # 続けて近づかなかった回数


@dataclass
class Ledger:
    """一覧の台帳（ランの間、label ごとに覚える）と、一覧ごとの目標。"""

    items: dict[str, Item] = field(default_factory=dict)
    goals: dict[str, Goal] = field(default_factory=dict)  # 一覧（Row.key）→ 目標
    unreachable: set[str] = field(default_factory=set)  # 届かなかった目標（その画面を閉じるまで目標にしない）
    dead: Counter[tuple[str, str]] = field(default_factory=Counter)  # (menu, 動かす操作) → 続けて近づかなかった回数
    filling: set[str] = field(default_factory=set)  # 台帳を埋めている一覧（Row.key）
    noted: set[str] = field(default_factory=set)  # 「ハーネスの記録」に書いたこと

    def sync(self, state: GameState, masked_chars: str) -> list[Row]:
        """いまの画面から台帳を更新し、一覧を返す。"""
        rows: list[Row] = []
        for m in searchable_menus(state):
            by_name: dict[str, list[Target]] = {}
            for b in m.buttons:
                by_name.setdefault(b.name, []).append(b)
            for name, bs in by_name.items():
                if len(bs) < 3:
                    continue
                for b in bs:
                    self._update(b, masked_chars)
                rows.append(Row(m.name, name, view_of(m, bs), tuple(b.label for b in bs)))
        return rows

    def _update(self, b: Target, masked_chars: str) -> None:
        item = self.items.get(b.label)
        if item is None:
            item = self.items[b.label] = Item(b.label, b.menu, b.name, b.rect)
        item.rect = b.rect
        if b.visible:
            item.shown = True
        if not b.text:
            return
        if item.text is not None and _norm(item.text) != _norm(b.text) and item.status in (OK, NO):
            item.status = UNKNOWN  # 中身が入れ替わった（同じ並びの番号に、別の項目が来た）
        item.text = b.text
        if b.text not in item.readings:
            item.readings.append(b.text)
        if masked(b.text, masked_chars):
            item.status = MASKED
        elif item.status == MASKED:
            item.status = UNKNOWN

    def pressed(self, label: str, responded: bool, misses: int, limit: int) -> None:
        """押した結果を書く。反応があれば押せた、続けて limit 回反応がなければ押せない。"""
        item = self.items.get(label)
        if item is None:
            return
        if responded:
            item.status = OK
        elif misses >= limit and item.status != MASKED:
            item.status = NO

    def closed(self, open_menus: set[str]) -> list[str]:
        """閉じた画面の、目標・届かなかった目標・反応のなかった手段・見えたかを忘れる。閉じた画面の名前を返す。"""
        gone = {item.menu for item in self.items.values() if item.shown and item.menu not in open_menus}
        gone |= {key.split("/", 1)[0] for key in self.goals if key.split("/", 1)[0] not in open_menus}
        for item in self.items.values():
            if item.menu in gone:
                item.shown = False
        self.goals = {k: g for k, g in self.goals.items() if k.split("/", 1)[0] not in gone}
        self.unreachable = {label for label in self.unreachable if label.split("/", 1)[0] not in gone}
        self.dead = Counter({k: n for k, n in self.dead.items() if k[0] not in gone})
        self.filling = {k for k in self.filling if k.split("/", 1)[0] not in gone}
        return sorted(gone)

    def forget_progress(self) -> int:
        """ゲームの進み具合が入れ替わった（起動し直した、タイトルに戻った、流れの外でロードした）。押せた・押せないを
        未確認に戻す（開示の状態が変わるため）。戻した数を返す。"""
        n = 0
        for item in self.items.values():
            if item.status in (OK, NO):
                item.status = UNKNOWN
                n += 1
        return n

    def unshown(self, row: Row) -> list[str]:
        """画面を開いてから、まだ見えていない項目（台帳を埋めるために見にいくもの）。"""
        return [label for label in row.labels if not self.items[label].shown and label not in self.unreachable]

    def nearest(self, row: Row, labels: list[str]) -> str | None:
        """表示範囲から動かす距離がいちばん短いもの（同じなら並びの順）。"""
        if not labels:
            return None
        order = {label: i for i, label in enumerate(row.labels)}
        return min(labels, key=lambda label: (distance(self.items[label].rect, row.view), order.get(label, 0)))

    def records(self, menu: str) -> dict[str, list[str | None]]:
        """閉じた画面の台帳（events.jsonl の listing）。"""
        return {item.label: [item.text, STATUS_JA[item.status]]
                for item in self.items.values() if item.menu == menu}


def _norm(text: str) -> str:
    return "".join(text.split())


def searchable_menus(state: GameState) -> list[Menu]:
    """一覧を探す画面: 手前のモーダル（なければ、後ろに下がっていない、ボタンのある画面）。"""
    top = state.top_modal()
    if top is not None:
        return [top]
    return [m for m in state.menus.values() if m.buttons and m.name not in state.behind]


def view_of(menu: Menu, buttons: list[Target]) -> Rect:
    """一覧の表示範囲。見えている項目の中心をいちばん多く含む scroll の矩形（なければ描画領域）。"""
    best, count = WHOLE, 0
    vis = [b for b in buttons if b.visible] or buttons
    for r in menu.scroll:
        n = sum(1 for b in vis if _inside(_center(b.rect), r))
        if n > count:
            best, count = r, n
    return best


def _center(r: Rect) -> tuple[float, float]:
    return (r[0] + r[2]) / 2, (r[1] + r[3]) / 2


def _inside(p: tuple[float, float], r: Rect) -> bool:
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def distance(rect: Rect, view: Rect) -> float:
    """項目の中心が表示範囲からはみ出している長さ（縦と横の和）。入っていれば 0。"""
    cx, cy = _center(rect)
    dx = max(view[0] - cx, 0.0, cx - view[2])
    dy = max(view[1] - cy, 0.0, cy - view[3])
    return dx + dy


def direction(rect: Rect, view: Rect) -> str | None:
    """項目を表示範囲に入れるために、一覧のどちら側を見せるか。縦と横の両方にはみ出していれば、縦を先に合わせる。
    表示範囲に入っているのに見えていない（マスクの端にかかっている）ときは、表示範囲の中心に寄せる向き。"""
    cx, cy = _center(rect)
    if cy > view[3]:
        return "down"
    if cy < view[1]:
        return "up"
    if cx > view[2]:
        return "right"
    if cx < view[0]:
        return "left"
    vx, vy = _center(view)
    if abs(cy - vy) >= abs(cx - vx):
        return "down" if cy > vy else "up"
    return "right" if cx > vx else "left"


def moves(row: Row, d: str, weight: float, goal: str, by: str) -> list[Op]:
    """目標の向き d へ一覧を動かす操作（ホイール・ドラッグ・方向キー）。試行回数で重みを下げない（端まで続けて動かせるように）。"""
    point = _center(row.view)
    return [Op(kind, arg=arg, weight=weight, novelty=False, group="list", point=None if kind == "key" else point,
               by=by, goal=goal) for kind, arg in REVEAL[d]]


def stuck_rows(state: GameState, stuck: set[str], min_group: int = 3) -> list[tuple[str, str, tuple[float, float]]]:
    """同じ名前で見えているボタンが min_group 個以上あり、どれも押せそうにない（stuck）並びの (menu, name, 中心)。"""
    out = []
    for m in searchable_menus(state):
        by_name: dict[str, list[Target]] = {}
        for b in m.visible_buttons:
            by_name.setdefault(b.name, []).append(b)
        for name, vis in by_name.items():
            if len(vis) < min_group or not all(b.label in stuck for b in vis):
                continue
            x0, y0 = min(b.rect[0] for b in vis), min(b.rect[1] for b in vis)
            x1, y1 = max(b.rect[2] for b in vis), max(b.rect[3] for b in vis)
            out.append((m.name, name, ((x0 + x1) / 2, (y0 + y1) / 2)))
    return out


def blind_candidates(point: tuple[float, float], weight: float) -> list[Op]:
    """矩形が来ず、ページ送りのボタンも見つからない一覧で、押せるものを探して試す操作（すべての向きへ動かす）。

    試行回数で重みを下げ（novelty）、いろいろな向き・手段を順に試す。手段の数は向きによって違うので、向きごとの
    重みが同じになるように手段の数で割る（手段の多い下ばかり試さないように）。
    """
    ops: list[Op] = []
    for d in ("down", "up", "right", "left"):
        for kind, arg in REVEAL[d]:
            ops.append(Op(kind, arg=arg, weight=weight / len(REVEAL[d]), novelty=True, group="search", by="blind",
                          point=None if kind == "key" else point))
    return ops
