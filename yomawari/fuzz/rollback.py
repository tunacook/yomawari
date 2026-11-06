"""エッジケース（DESIGN.md §9「エッジケース」）: 壊れやすいタイミングと、そこに差し込む狙って行うのが難しい操作。

人の手では狙いにくいタイミング（選択肢の直後、scene の境目、演出の途中、セーブ・ロードの途中や直後、質問に答えた直後）に、
連打・メニューの開け閉め・巻き戻しなどを差し込んで、壊れないかを見る。

巻き戻し（バックログの巻き戻しボタン、ホイール上など）はノベルゲームの正常な機能で、通しプレイの中で使ってよい。
エッジケースでは、1回戻る・続けて戻る・戻ってすぐ送るを繰り返す、のどれかで差し込む。

巻き戻しの操作はゲームごとに違うので、決め打ちしない。ハーネスの操作のあと、読んだことのある前の行が出たら
（ロード・scene の切り替えを挟まずに）、その操作を巻き戻しとして覚える。操作がモーダルの中のボタン
（バックログの巻き戻しボタン）なら、そのモーダルを開いた操作と合わせて、2つの操作の並びとして覚える。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .ops import Op

# 差し込むタイミング → 記録に書く名前
EDGE_TIMINGS = {
    "line": "本文が出た直後",
    "choices": "選択肢が出た直後",
    "choice": "選択肢を選んだ直後",
    "scene": "scene の境目",
    "transition": "演出の途中",
    "save": "セーブの途中",
    "load": "ロードの途中",
    "load_end": "ロードの直後（思い出から本編に戻った直後など）",
    "answer": "流れを最後まで進めた直後（質問に答えた直後など）",
    "random": "いつでも",
}
# 差し込む操作の種類 → 記録に書く名前
EDGE_OPS = {
    "rapid_click": "連打",
    "rapid_key": "キーの連打",
    "right_click": "右クリック",
    "key": "Esc",
    "wheel_up": "ホイール上",
    "wheel_down": "ホイール下",
    "double_click": "ボタンのダブルクリック",
    "click": "押せないボタン・ふだん押さないボタンを押す",
    "menu_flash": "メニューを開いてすぐ閉じる",
    "rollback": "巻き戻し",
}
PATTERNS = {"single": "1回戻る", "repeat": "続けて戻る", "bounce": "戻ってすぐ送るを繰り返す"}

Step = tuple[str, str | None]  # (操作の種類, 引数かボタンの label から #n を除いたもの)


def edge_timing(kind: str, data: dict) -> str | None:
    """ゲームのイベントから、差し込むタイミングを決める。"""
    phase = data.get("phase")
    if kind == "choices":
        return "choices" if data.get("items") else "choice"  # 選択肢が消えた = 選んだ
    if kind in ("transition", "save"):
        return kind if phase == "start" else None
    if kind == "load":
        return "load" if phase == "start" else "load_end" if phase == "end" else None
    if kind in ("line", "scene"):
        return kind
    return None


def step_of(op: Op) -> Step:
    """操作を、あとで同じように行える形にする。ボタンは、同じ名前の別の行（バックログの別の行）でもよいように #n を除く。"""
    if op.target is not None:
        return (op.kind, op.target.label.split("#", 1)[0])
    return (op.kind, None if op.arg is None else str(op.arg))


def describe(seq: tuple[Step, ...]) -> str:
    return " → ".join(f"`{k}{' ' + a if a else ''}`" for k, a in seq)


@dataclass
class RollbackWatch:
    """本文の行の順番と、巻き戻しになった操作の並び。ラン全体で持ち越す。"""

    order: dict[str, int] = field(default_factory=dict)  # line_id → 初めて見た順番
    current: int | None = None  # いま出ている行の順番
    pending: tuple[tuple[Step, ...], int] | None = None  # 直前の操作の並びと、そのときの行の順番
    learned: Counter[tuple[Step, ...]] = field(default_factory=Counter)  # 巻き戻しになった操作の並び → 回数
    rewinds: int = 0  # 前の行に戻った回数（巻き戻しの操作を差し込んだあとの確認に使う）

    def operated(self, seq: tuple[Step, ...]) -> None:
        """ハーネスが操作した（本文送りは除く）。次に前の行が出たら、この並びを巻き戻しとして覚える。"""
        if self.current is not None:
            self.pending = (seq, self.current)

    def jumped(self) -> None:
        """ロード・scene の切り替え。前の行が出ても、操作による巻き戻しとはみなさない。"""
        self.pending = None

    def line(self, line_id: str) -> bool:
        """本文の行が出た。前の行に戻ったら True。"""
        idx = self.order.get(line_id)
        if idx is None:
            idx = self.order[line_id] = len(self.order)
        back = self.current is not None and idx < self.current
        if back:
            self.rewinds += 1
            if self.pending is not None and idx < self.pending[1]:
                self.learned[self.pending[0]] += 1
        self.pending = None
        self.current = idx
        return back

    def best(self) -> tuple[Step, ...] | None:
        """いちばん多く巻き戻しになった操作の並び。"""
        return self.learned.most_common(1)[0][0] if self.learned else None
