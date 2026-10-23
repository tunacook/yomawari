"""操作の種類と、状態から候補を作る処理。

通常の操作（本文送り、ボタン、選択肢など）と、イレギュラー操作（連打、フェード中の右クリックなど）を分けて作る。
どのボタンを候補に入れるかは Guard が決める。重みは yomawari.toml の [fuzz.weights]。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..state import GameState, Target
from .guard import Guard

# kind:
#   advance       描画領域の advance_point を1回クリック（本文送り）
#   click         target を1回クリック
#   right_click   advance_point を右クリック
#   wheel_up / wheel_down  advance_point でホイール
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

    @property
    def label(self) -> str:
        parts = [self.kind]
        if self.target is not None:
            parts.append(self.target.label)
        if self.arg is not None:
            parts.append(str(self.arg))
        return " ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "irregular": self.irregular}


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


def normal_candidates(state: GameState, guard: Guard, weights: dict[str, float], allow_exit: bool) -> list[Op]:
    """通常の操作の候補。暗転などで待つべきときは空。"""
    if state.busy:
        return []
    targets = state.targets()
    ops: list[Op] = []
    for t in targets:
        kind = guard.kind_of(t.key)
        if kind is None:
            ops.append(Op("click", t, weight=float(weights["click"]), novelty=True))
        elif kind == "expect_exit" and allow_exit:
            ops.append(Op("expect_exit", t, weight=float(weights["click"]), novelty=True))
        elif kind == "external" and guard.external_available(t.key):
            ops.append(Op("external", t, weight=float(weights["click"]), novelty=True))
    if targets:
        return ops
    if state.top_modal() is not None:
        # ボタンのないモーダル（暗転の幕など）。抜け出す操作は loop が時間を見て足す
        return []
    return [
        Op("advance", weight=float(weights["advance"])),
        Op("right_click", weight=float(weights["right_click"])),
        Op("wheel_up", arg=1, weight=float(weights["wheel_up"])),
    ]


def escape_candidates() -> list[Op]:
    """ボタンのないモーダルが長く残ったときに試す通常の操作（バックログなどを閉じる）。"""
    return [Op("key", arg="esc"), Op("right_click"), Op("advance")]


def irregular_candidates(state: GameState, guard: Guard, rapid_count: int) -> list[Op]:
    """イレギュラー操作の候補。暗転中・セーブ中でも作る（そのときこそ差し込みたい）。"""
    ops = [
        Op("rapid_click", arg=rapid_count, irregular=True, novelty=True),
        Op("rapid_key", arg="enter", irregular=True, novelty=True),
        Op("rapid_key", arg="space", irregular=True, novelty=True),
        Op("right_click", irregular=True, novelty=True),
        Op("key", arg="esc", irregular=True, novelty=True),
        Op("wheel_up", arg=3, irregular=True, novelty=True),
        Op("wheel_down", arg=3, irregular=True, novelty=True),
    ]
    for t in state.targets():
        kind = guard.kind_of(t.key)
        if kind is None:
            ops.append(Op("double_click", t, irregular=True, novelty=True))
        elif kind == "irregular_only":
            ops.append(Op("click", t, irregular=True, novelty=True))
    return ops
