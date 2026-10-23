"""操作の選択（DESIGN.md §9 Decider）。

random: 「状態 × 操作」の試行回数を数え、まだ試していない操作ほど選ばれやすい重み付きランダム。
同じ状態で直前と同じ操作を選びそうになったら、ほかに候補があれば次点を選ぶ。
押しても反応がなかった操作は、同じ状態では後回しにする（ロックされたスロットが多い画面で、時間を取られないように）。
local / jev はフェーズ7。
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Protocol

from .ops import Op


class Decider(Protocol):
    def choose(self, key: str, candidates: list[Op]) -> Op | None: ...
    def feedback(self, key: str, op: Op, responded: bool) -> None: ...


class RandomDecider:
    def __init__(self, rng: random.Random, no_response_factor: float = 1.0) -> None:
        self.rng = rng
        # 反応がなかった操作の重みに、反応がなかった回数だけ掛ける値（[fuzz] no_response_factor）
        self.no_response_factor = no_response_factor
        self.tries: Counter[tuple[str, str]] = Counter()
        self.misses: Counter[tuple[str, str]] = Counter()
        self._last: dict[str, str] = {}

    def weight(self, key: str, op: Op) -> float:
        if not op.novelty:
            return op.weight
        k = (key, op.label)
        return op.weight / (1 + self.tries[k]) * self.no_response_factor ** self.misses[k]

    def choose(self, key: str, candidates: list[Op]) -> Op | None:
        if not candidates:
            return None
        pool = candidates
        last = self._last.get(key)
        if len(candidates) > 1 and last is not None:
            others = [op for op in candidates if not (op.novelty and op.label == last)]
            pool = others or candidates
        weights = [self.weight(key, op) for op in pool]
        op = self.rng.choices(pool, weights=weights)[0]
        self.tries[(key, op.label)] += 1
        if op.novelty:
            self._last[key] = op.label
        return op

    def feedback(self, key: str, op: Op, responded: bool) -> None:
        if responded:
            self.misses.pop((key, op.label), None)
        else:
            self.misses[(key, op.label)] += 1
