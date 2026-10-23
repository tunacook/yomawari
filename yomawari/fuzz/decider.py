"""操作の選択（DESIGN.md §9 Decider）。

random: 「状態 × 操作」の試行回数を数え、まだ試していない操作ほど選ばれやすい重み付きランダム。
同じ状態で直前と同じ操作を選びそうになったら、ほかに候補があれば次点を選ぶ。
local / jev はフェーズ7。
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Protocol

from .ops import Op


class Decider(Protocol):
    def choose(self, key: str, candidates: list[Op]) -> Op | None: ...


class RandomDecider:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.tries: Counter[tuple[str, str]] = Counter()
        self._last: dict[str, str] = {}

    def weight(self, key: str, op: Op) -> float:
        if not op.novelty:
            return op.weight
        return op.weight / (1 + self.tries[(key, op.label)])

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
