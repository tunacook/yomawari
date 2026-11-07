"""操作の種類と、状態から候補を作る処理。

通常の操作（本文送り、ボタン、選択肢など）と、イレギュラー操作（連打、フェード中の右クリックなど）を分けて作る。
どのボタンを候補に入れるかは Guard が決める。重みは yomawari.toml の [fuzz.weights]。

ボタンのあるモーダルな画面では、ボタンを押す操作のほかに、画面を動かす操作（ドラッグ・ホイール・方向キー）と、
画面を抜ける操作（Esc・右クリック）も候補に入れる。スクロールしないと出てこない項目や、キー操作で選ぶ画面、
閉じるボタンがボタンの山に埋もれた画面で止まらないように。3つは group で分け、Decider はまずグループを
重みで選ぶ（ボタンがいくつあっても、動かす・抜ける操作を一定の割合で試す）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from ..state import GameState, Target
from .guard import Guard

# kind:
#   advance       描画領域の advance_point を1回クリック（本文送り）
#   click         target を1回クリック
#   right_click   advance_point を右クリック
#   wheel_up / wheel_down  point（なければ advance_point）でホイール
#   drag          point から arg の向き（up / down / left / right）へドラッグ
#   key           arg のキーを押す
#   rapid_click   advance_point を arg 回連打
#   rapid_key     arg のキーを rapid_count 回連打
#   double_click  target をダブルクリック
#   expect_exit   [guard.buttons] expect_exit のボタンを押し、終了を待つ
#   external      [guard.buttons] external のボタンを押し、前面が変わるかを見てからゲームを前面に戻す


@dataclass(frozen=True)
class Op:
    kind: str
    target: Target | None = None
    arg: str | int | None = None
    irregular: bool = False
    weight: float = 1.0
    novelty: bool = False  # True なら Decider が試行回数で重みを下げる
    group: str | None = None  # Decider がまとめて選ぶ単位（button / move / leave）。None なら操作ごと
    point: tuple[float, float] | None = None  # ホイール・ドラッグの位置（描画領域の正規化座標）
    # どの仕組みが出した操作か（normal: 画面ごとの通常の操作 / search・return・blind・flow_search: 画面の外を探す /
    # escape・recover: 抜け出す）。events.jsonl の op に残し、行ったり来たりしたときに出どころを突き止める
    by: str = "normal"
    why: tuple[tuple[str, float], ...] = ()  # 重みを変えた層と倍率（screen・looks・intent・resume・save_first・flow）

    def scaled(self, layer: str, f: float) -> Op:
        """重みに f を掛け、掛けた層を why に残したもの。f が 1 ならそのまま。"""
        if f == 1.0:
            return self
        return dataclasses.replace(self, weight=self.weight * f, why=(*self.why, (layer, f)))

    @property
    def label(self) -> str:
        parts = [self.kind]
        if self.target is not None:
            parts.append(self.target.label)
        if self.arg is not None:
            parts.append(str(self.arg))
        return " ".join(parts)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "label": self.label, "irregular": self.irregular,
                               "by": "irregular" if self.irregular else self.by}
        if self.why:
            out["why"] = [[layer, round(f, 4)] for layer, f in self.why]
        return out


def state_key(state: GameState) -> str:
    """Decider が試行回数を数える単位。ボタンのある menu の組み合わせで分ける（本文の位置は含めない）。"""
    top = state.top_modal()
    if top is not None:
        return f"modal:{top.name}"
    with_buttons = sorted(m.name for m in state.menus.values() if m.buttons)
    if with_buttons:
        return "menus:" + ",".join(with_buttons)
    if state.choices:
        return "choices"
    return "reading"


def normal_candidates(
    state: GameState, guard: Guard, weights: dict[str, float], allow_exit: bool,
    screen_weights: dict[str, float] | None = None, factors: dict[str, float] | None = None,
) -> list[Op]:
    """通常の操作の候補。暗転などで待つべきときは空。

    locked のボタン（ゲームが押せないと伝えたもの）は入れない。screen_weights はプロファイルの
    [screens.<menu>] weight で、その menu のボタンの重みに掛ける。factors はボタン（label）ごとに重みに掛けるもの
    （暗い・空のボタンは小さく、新しく明るくなったボタンは大きく。fuzz/looks.py）。画面の外を探す操作は fuzz/search.py。
    """
    if state.busy:
        return []
    targets = [t for t in state.targets() if not t.locked]
    ops: list[Op] = []
    click = float(weights["click"])
    for t in targets:
        sw, lf = (screen_weights or {}).get(t.menu, 1.0), (factors or {}).get(t.label, 1.0)
        why = tuple(x for x in (("screen", sw), ("looks", lf)) if x[1] != 1.0)
        w = click * sw * lf
        kind = guard.kind_of(t)
        if kind is None:
            ops.append(Op("click", t, weight=w, novelty=True, group="button", why=why))
        elif kind == "expect_exit" and allow_exit:
            ops.append(Op("expect_exit", t, weight=w, novelty=True, group="button", why=why))
        elif kind == "external" and guard.external_available(t.label):
            ops.append(Op("external", t, weight=w, novelty=True, group="button", why=why))
    top = state.top_modal()
    if top is not None and top.buttons:
        # 見えているボタンがない・すべて押せないときも、動かす・抜ける操作は試す
        return ops + modal_candidates(top.area, weights)
    if targets and not state.reading_with_buttons():
        return ops
    if top is not None:
        # ボタンのないモーダル（暗転の幕など）。抜け出す操作は loop が時間を見て足す
        return []
    # 本文を読んでいる。操作のボタン（回想中の「本編へ戻る」など）が出ていても、本文送りを並べて候補に入れる
    return ops + reading_candidates(weights)


def reading_candidates(weights: dict[str, float]) -> list[Op]:
    return [
        Op("advance", weight=float(weights["advance"])),
        Op("right_click", weight=float(weights["right_click"])),
        Op("wheel_up", arg=1, weight=float(weights["wheel_up"])),
    ]


def modal_candidates(
    area: tuple[float, float, float, float] | None, weights: dict[str, float],
) -> list[Op]:
    """ボタンのあるモーダルな画面で、ボタンを押す以外に試す操作（動かす・抜ける）。"""
    x0, y0, x1, y1 = area or (0.0, 0.0, 1.0, 1.0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    move, leave = float(weights["move"]), float(weights["leave"])
    ops = [Op("drag", arg=d, weight=move, novelty=True, group="move", point=(cx, cy))
           for d in ("up", "down", "left", "right")]
    ops += [Op(k, arg=3, weight=move, novelty=True, group="move", point=(cx, cy)) for k in ("wheel_down", "wheel_up")]
    ops += [Op("key", arg=k, weight=move, novelty=True, group="move") for k in ("down", "up", "right", "left", "enter")]
    ops += [Op("key", arg="esc", weight=leave, novelty=True, group="leave"),
            Op("right_click", weight=leave, novelty=True, group="leave")]
    return ops


def escape_candidates() -> list[Op]:
    """ボタンのないモーダルが長く残ったときに試す通常の操作（バックログなどを閉じる）。"""
    return [Op("key", arg="esc", by="escape"), Op("right_click", by="escape"), Op("advance", by="escape")]


def irregular_candidates(state: GameState, guard: Guard, rapid_count: int, rollback: bool = False) -> list[Op]:
    """イレギュラー操作の候補。暗転中・セーブ中でも作る（そのときこそ差し込みたい）。

    rollback: 巻き戻しの操作を覚えていれば、巻き戻し（1回・続けて・戻ってすぐ送る）も候補にする（fuzz/rollback.py）。"""
    ops = [
        Op("rapid_click", arg=rapid_count, irregular=True, novelty=True),
        Op("rapid_key", arg="enter", irregular=True, novelty=True),
        Op("rapid_key", arg="space", irregular=True, novelty=True),
        Op("right_click", irregular=True, novelty=True),
        Op("key", arg="esc", irregular=True, novelty=True),
        Op("wheel_up", arg=3, irregular=True, novelty=True),
        Op("wheel_down", arg=3, irregular=True, novelty=True),
        Op("menu_flash", irregular=True, novelty=True),  # メニューを開いてすぐ閉じる
    ]
    if rollback:
        ops.append(Op("rollback", irregular=True, novelty=True))
    for t in state.targets():
        kind = guard.kind_of(t)
        if kind is None and t.locked:
            # 押せないボタンは、押しても壊れないことだけを確かめる
            ops.append(Op("click", t, irregular=True, novelty=True))
        elif kind is None:
            ops.append(Op("double_click", t, irregular=True, novelty=True))
        elif kind == "irregular_only":
            ops.append(Op("click", t, irregular=True, novelty=True))
    return ops
