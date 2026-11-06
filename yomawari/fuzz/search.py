"""見えていない項目を探しにいく（DESIGN.md §9「画像による推定」の「画面の外を探す」）。

回想の一覧のように、同じ名前のボタンがスクロール領域に並ぶ画面では、開いた直後に見えているのは一部だけで、
アンロックされた項目が画面の外にあることがある。人は、見えている項目がどれも暗ければ、スクロールして残りを見にいく。

ボタンの矩形は、スクロールで隠れていても（hidden、または中心が描画領域の外）ゲームから来る。同じ名前で見えている
ボタンの範囲より下・上・右・左にあって、このランでまだ一度も見えていないボタンがあれば、その向きに「まだ見ていない
項目がある」とし、そちらへ動かす操作（ホイール・ドラッグ・方向キー）を候補に入れる。この操作は試行回数で重みを
下げない（端まで続けて動かせるように）。見ていない項目が見えれば、その向きの候補は消える。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..state import GameState, Menu, Target
from .ops import Op

# まだ見ていない項目がある向き → それを見えるようにする操作（下にあるなら、ホイールを下へ・上へドラッグ・下キー）
REVEAL: dict[str, list[tuple[str, str | int]]] = {
    "down": [("wheel_down", 3), ("drag", "up"), ("key", "down")],
    "up": [("wheel_up", 3), ("drag", "down"), ("key", "up")],
    "right": [("drag", "left"), ("key", "right")],
    "left": [("drag", "right"), ("key", "left")],
}


@dataclass(frozen=True)
class Lead:
    """まだ見ていない項目がある向き。"""

    menu: str
    name: str  # ボタンの名前（同じ名前の並び）
    direction: str  # up / down / left / right
    unseen: int  # その向きにある、まだ見ていないボタンの数
    point: tuple[float, float]  # ホイール・ドラッグをする位置（見えているボタンの範囲の中心）
    visible: tuple[str, ...]  # 見えているボタンの label
    # unseen: まだ見ていない項目を探す / known: 前に見て明るかった（開示済みと分かっている）項目へ戻る /
    # flow: ゲームの流れで、まだ試していない選び方を探す
    kind: str = "unseen"


def searchable_menus(state: GameState) -> list[Menu]:
    top = state.top_modal()
    if top is not None:
        return [top]
    return [m for m in state.menus.values() if m.buttons and m.name not in state.behind]


def leads(state: GameState, seen: set[str]) -> list[Lead]:
    """menu ごと・同じ名前の並びごとに、まだ見ていないボタンがある向き（まだ認識していない項目を探す）。"""
    return _directions(state, lambda vis, b: b.label not in seen, "unseen")


def known_leads(state: GameState, known: set[str]) -> list[Lead]:
    """見えているボタンに known がない並びで、画面の外の known（前に見て明るかった項目）がある向き。

    すでに認識している開示済みの項目へ戻る。まだ見ていない項目を探す（leads）のとは別の行動として扱う。
    """
    def want(vis: list[Target], b: Target) -> bool:
        return b.label in known and not any(v.label in known for v in vis)
    return _directions(state, want, "known")


def toward(state: GameState, menu: str, want) -> list[Lead]:
    """menu の画面の外にある、want(b) に当てはまるボタンの向き（ゲームの流れで、見えている選び方を試し終えたあとに
    まだ試していない選び方を探しにいく。fuzz/loop.py）。"""
    return [lead for lead in _directions(state, lambda vis, b: want(b), "flow") if lead.menu == menu]


def _directions(state: GameState, want, kind: str) -> list[Lead]:
    out: list[Lead] = []
    for m in searchable_menus(state):
        by_name: dict[str, list[Target]] = {}
        for b in m.buttons:
            by_name.setdefault(b.name, []).append(b)
        for name, bs in by_name.items():
            vis = [b for b in bs if b.visible]
            if not vis:
                continue
            x0 = min(b.rect[0] for b in vis)
            y0 = min(b.rect[1] for b in vis)
            x1 = max(b.rect[2] for b in vis)
            y1 = max(b.rect[3] for b in vis)
            count: Counter[str] = Counter()
            for b in bs:
                if b.visible or b.covered or not want(vis, b):
                    continue
                cx, cy = (b.rect[0] + b.rect[2]) / 2, (b.rect[1] + b.rect[3]) / 2
                if cx > x1 or cx < x0:
                    # 見えている列の外（斜めも）は、縦に動かしても見えてこないので、横の向きにだけ数える。横に動かせない
                    # 一覧なら、横の向きは search_give_up であきらめる（run 2026-11-06_1846: 横に7列並ぶうち4列しか
                    # 見えない回想の一覧で、上の行の右の列と下の行の右の列を「上にも下にもある」と数え、上下に動かし続けた）
                    count["right" if cx > x1 else "left"] += 1
                elif cy > y1:
                    count["down"] += 1
                elif cy < y0:
                    count["up"] += 1
            point = ((x0 + x1) / 2, (y0 + y1) / 2)
            labels = tuple(b.label for b in vis)
            out += [Lead(m.name, name, d, n, point, labels, kind) for d, n in sorted(count.items())]
    return out


def search_candidates(found: list[Lead], weights: dict[str, float], group: str = "search") -> list[Op]:
    """見ていない（または戻りたい）項目のある向きへ動かす操作。同じ操作は重みの大きいほうだけ残す。"""
    best: dict[tuple[str, str | int], Op] = {}
    for lead in found:
        for kind, arg in REVEAL[lead.direction]:
            w = float(weights.get(f"{lead.menu}/{lead.name}", 0.0))
            if w <= 0:
                continue
            point = None if kind == "key" else lead.point
            op = Op(kind, arg=arg, weight=w, novelty=False, group=group, point=point)
            if (kind, arg) not in best or best[(kind, arg)].weight < w:
                best[(kind, arg)] = op
    return list(best.values())


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
    """どの向きに項目があるか分からないときに、押せるものを探して試す操作（すべての向きへ動かす）。

    試行回数で重みを下げ（novelty）、いろいろな向き・手段を順に試す。手段の数は向きによって違うので、向きごとの
    重みが同じになるように手段の数で割る（手段の多い下ばかり試さないように）。
    """
    ops: list[Op] = []
    for d in ("down", "up", "right", "left"):
        for kind, arg in REVEAL[d]:
            ops.append(Op(kind, arg=arg, weight=weight / len(REVEAL[d]), novelty=True, group="search",
                          point=None if kind == "key" else point))
    return ops
