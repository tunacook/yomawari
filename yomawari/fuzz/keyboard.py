"""入力の方式（--input・--input-mix。DESIGN.md §9「入力の方式」）。マウス中心とキーボード中心を選び、もう一方を割合で混ぜる。

操作の種類（本文送り・メニュー・ボタンを押す）は変えず、押し方だけを変える。S-02 の確かめ方や進行の学習などは、操作の種類で
数えているため。キーボードで押した操作は、Op の via_key にそのキーを入れる（events.jsonl の op の via）。

- 本文送り（advance）: advance_keys のキーで押す。どのキーで本文が進むかはゲームごとに違うので決め打ちしない。まだ
  key_learn_tries 回試していないキーを先に試し、本文が進んだ割合がいちばん高いキーを使う。どのキーでも進まなければ、
  マウスで押す（ハーネスの記録に書く）
- メニュー（右クリック）: menu_key（Esc）を押す
- ボタン: 方向キーでフォーカス（ゲームの selected）を目的のボタンまで移し、submit_key（Enter）で押す（via_key は NAV）。
  向きは、いまフォーカスのあるボタンから目的のボタンへの向きに近い方向キーから試す。フォーカスが動かなかった向きは、
  同じボタンの上では試さない。nav_max_steps 回で着かない・試せる向きがなくなったら、たどり着けなかったボタンとして
  覚えてハーネスの記録に書き、そのボタンは以後マウスで押す（キーボードで遊ぶ人が詰まるところ）
- キーボードで押せない操作（ホイール・ドラッグ）は、キーボード中心なら重みに混ぜる割合を掛ける（0 なら、ほかに操作が
  あるあいだは入れない）
"""

from __future__ import annotations

import dataclasses
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..state import Rect
from .ops import Op

INPUTS = ("mouse", "keyboard")
# キーボードでは押せない操作（キーボード中心なら、混ぜる割合だけ入れる）
MOUSE_ONLY = frozenset({"wheel_up", "wheel_down", "drag"})
# キーボードで押してよい操作の出どころ（ハーネスが確かめるための本文送り・抜け出す操作は、決まった押し方のまま）
_CONVERTIBLE_BY = frozenset({"normal", "list", "flow", "save_first", "settings", "locale", "resume"})
# ボタンを方向キーでたどって押す操作の via_key（たどり着いたら submit_key にする）
NAV = "nav"
# 方向キー → 画面上の向き（正規化座標。y は下向き）
DIRECTIONS = {"up": (0.0, -1.0), "down": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}


class InputError(ValueError):
    pass


@dataclass
class InputStyle:
    main: str = "mouse"  # mouse / keyboard
    mix: float = 0.0  # 主でない方を混ぜる割合（0〜1）
    advance_keys: tuple[str, ...] = ("enter", "space")
    menu_key: str = "esc"
    submit_key: str = "enter"  # フォーカスのあるボタンを押すキー
    learn_tries: int = 3  # 本文送りのキーを、それぞれこの回数は試してから選ぶ
    tried: Counter[str] = field(default_factory=Counter)  # 本文送りのキー → 押した回数
    worked: Counter[str] = field(default_factory=Counter)  # 本文送りのキー → 本文が進んだ回数
    used: Counter[str] = field(default_factory=Counter)  # 操作の種類 → キーボードで押した回数
    gave_up_noted: bool = False
    unreachable: Counter[str] = field(default_factory=Counter)  # ボタン → 方向キーでたどり着けなかった回数
    reached: Counter[str] = field(default_factory=Counter)  # ボタン → 方向キーでたどり着いて押した回数

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> InputStyle:
        main = str(cfg.get("input", "mouse"))
        if main not in INPUTS:
            raise InputError(f"入力の方式は {' / '.join(INPUTS)} のどれかです: {main}")
        mix = float(cfg.get("input_mix", 0.0))
        if not 0.0 <= mix <= 1.0:
            raise InputError(f"混ぜる割合は 0〜1 です: {mix}")
        keys = tuple(str(k) for k in cfg.get("advance_keys", ("enter", "space")))
        return cls(main, mix, keys or ("enter",), str(cfg.get("menu_key", "esc")), str(cfg.get("submit_key", "enter")),
                   int(cfg.get("key_learn_tries", 3)))

    @property
    def keyboard_share(self) -> float:
        """操作をキーボードで押す割合。"""
        return self.mix if self.main == "mouse" else 1.0 - self.mix

    @property
    def active(self) -> bool:
        return self.keyboard_share > 0

    def scale(self, cands: list[Op]) -> list[Op]:
        """キーボード中心なら、キーボードで押せない操作の重みに混ぜる割合を掛ける。ほかに操作がなければそのまま。"""
        if self.main != "keyboard":
            return cands
        mouse_only = [op for op in cands if op.kind in MOUSE_ONLY and not op.irregular]
        if not mouse_only:
            return cands
        if self.mix <= 0:
            rest = [op for op in cands if op not in mouse_only]
            return rest or cands
        return [op.scaled("input", self.mix) if op in mouse_only else op for op in cands]

    def convert(self, op: Op, rng: random.Random) -> Op:
        """選んだ操作を、キーボードで押す割合でキーボードの押し方にする。"""
        if not self.active or op.irregular or op.by not in _CONVERTIBLE_BY or op.via_key is not None:
            return op
        if op.kind == "advance":
            key = self._advance_key()
        elif op.kind == "right_click":
            key = self.menu_key
        elif op.kind == "click" and op.target is not None and op.target.kind == "button" \
                and op.target.label not in self.unreachable:
            key = NAV
        else:
            return op
        if key is None or rng.random() >= self.keyboard_share:
            return op
        self.used[op.kind] += 1
        return dataclasses.replace(op, via_key=key)

    def _advance_key(self) -> str | None:
        """本文送りに使うキー。まだ試し足りないキーを先に、あとは本文が進んだ割合がいちばん高いもの。どれも進まなければ None。"""
        for k in self.advance_keys:
            if self.tried[k] < self.learn_tries:
                return k
        best = max(self.advance_keys, key=lambda k: (self.worked[k] / self.tried[k], -self.advance_keys.index(k)))
        return best if self.worked[best] else None

    def advanced(self, key: str, worked: bool) -> None:
        """本文送りをキーで押した結果（本文が進んだか）。"""
        self.tried[key] += 1
        if worked:
            self.worked[key] += 1

    def gave_up(self) -> bool:
        """どの本文送りのキーでも本文が進まなかった（以後はマウスで押す）。最初の 1 回だけ True。"""
        if self.gave_up_noted or any(self.tried[k] < self.learn_tries for k in self.advance_keys):
            return False
        if any(self.worked[k] for k in self.advance_keys):
            return False
        self.gave_up_noted = True
        return True

    def navigated(self, label: str, reached: bool) -> None:
        (self.reached if reached else self.unreachable)[label] += 1

    def notes(self) -> list[str]:
        if not self.active:
            return []
        name = "キーボード中心" if self.main == "keyboard" else "マウス中心"
        out = [f"入力の方式: {name}（キーボードで押す割合 {self.keyboard_share:.0%}）"]
        if self.used:
            out.append("キーボードで押した操作: " + "、".join(f"{k} {n} 回" for k, n in sorted(self.used.items())))
        if self.reached:
            out.append(f"方向キーでたどり着いて押したボタン: {len(self.reached)} 個（{sum(self.reached.values())} 回）")
        if self.unreachable:
            out.append("方向キーでたどり着けなかったボタン（以後はマウスで押した。キーボードで遊ぶ人が押せないかもしれない）: "
                       + "、".join(f"`{k}`" for k in sorted(self.unreachable)))
        if self.tried:
            out.append("本文送りのキー（本文が進んだ回数 / 押した回数）: " + "、".join(
                f"`{k}` {self.worked[k]}/{self.tried[k]}" for k in self.advance_keys if self.tried[k]))
        return out


def next_direction(cur: Rect, target: Rect, tried: set[str]) -> str | None:
    """フォーカスのあるボタン（cur）から目的のボタン（target）へ向かう方向キー。向きに近いものから、tried を除いて選ぶ。
    目的のボタンのほうへ向かない方向キー（内積が 0 以下）は選ばない。"""
    dx = (target[0] + target[2]) / 2 - (cur[0] + cur[2]) / 2
    dy = (target[1] + target[3]) / 2 - (cur[1] + cur[3]) / 2
    scored = sorted(((vx * dx + vy * dy, k) for k, (vx, vy) in DIRECTIONS.items() if k not in tried), reverse=True)
    return next((k for score, k in scored if score > 0), None)
