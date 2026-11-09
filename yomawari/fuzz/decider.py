"""操作の選択（DESIGN.md §9 Decider）。

random: 「状態 × 操作」の試行回数を数え、まだ試していない操作ほど選ばれやすい重み付きランダム。
同じ状態で直前と同じ操作を選びそうになったら、ほかに候補があれば次点を選ぶ。
押しても反応がなかった操作は、同じ状態では後回しにする（ロックされたスロットが多い画面で、時間を取られないように）。
group の付いた操作は、まずグループを選び、その中から操作を選ぶ。グループの重みは、中の操作の [fuzz.weights] の
最大値に、中でいちばん試す価値の残っている操作の倍率（試行回数・反応のなさで下がる）を掛けたもの。
ボタンが何十個あっても、画面を動かす・抜ける操作が埋もれず、ボタンを押し尽くすとそちらが選ばれやすくなる。
local / jev はフェーズ7。
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from .ops import Op


class Decider(Protocol):
    def choose(self, key: str, candidates: list[Op]) -> Op | None: ...
    def feedback(self, key: str, op: Op, responded: bool) -> None: ...
    def forget_last(self) -> None: ...


class RandomDecider:
    def __init__(self, rng: random.Random, no_response_factor: float = 1.0) -> None:
        self.rng = rng
        # 反応がなかった操作の重みに、反応がなかった回数だけ掛ける値（[fuzz] no_response_factor）
        self.no_response_factor = no_response_factor
        self.tries: Counter[tuple[str, str]] = Counter()
        self.misses: Counter[tuple[str, str]] = Counter()
        self._last: dict[str, str] = {}
        # 経験から重みに掛けるもの（state_key, 操作）→ 倍率。random モードで、進行につながった操作を優先し、
        # 巻き戻した操作を後回しにする（fuzz/experience.py）。None なら使わない
        self.learned: Callable[[str, Op], float] | None = None
        # 最後に選んだときの、選んだ操作の確率と、確率の高かった候補（events.jsonl の op に残す。fuzz/loop.py）
        self.last_odds: dict[str, Any] = {}

    def factor(self, key: str, op: Op) -> float:
        """試行回数と反応のなさで下がる、重みの倍率。"""
        learned = self.learned(key, op) if self.learned is not None else 1.0
        if not op.novelty:
            return learned
        k = (key, op.label)
        return learned / (1 + self.tries[k]) * self.no_response_factor ** self.misses[k]

    def weight(self, key: str, op: Op) -> float:
        return op.weight * self.factor(key, op)

    def choose(self, key: str, candidates: list[Op]) -> Op | None:
        if not candidates:
            return None
        pool = candidates
        last = self._last.get(key)
        if len(candidates) > 1 and last is not None:
            others = [op for op in candidates if not (op.novelty and op.label == last)]
            pool = others or candidates
        groups: dict[str, list[Op]] = {}
        for c in pool:
            groups.setdefault(c.group or c.label, []).append(c)
        names = list(groups)
        gw = [max(c.weight for c in groups[g]) * max(self.factor(key, c) for c in groups[g]) for g in names]
        members = groups[self._pick(names, gw)]
        op = self._pick(members, [self.weight(key, c) for c in members])
        self.last_odds = self._odds(key, groups, names, gw, op)
        self.tries[(key, op.label)] += 1
        if op.novelty:
            self._last[key] = op.label
        return op

    def _odds(self, key: str, groups: dict[str, list[Op]], names: list[str], gw: list[float], op: Op) -> dict[str, Any]:
        """選んだ操作の確率と、確率の高い候補 3 つ（グループを選ぶ確率 × グループの中で選ぶ確率）。"""
        total = sum(gw)
        p: dict[str, float] = {}
        for g, w in zip(names, gw):
            ws = [self.weight(key, c) for c in groups[g]]
            inner = sum(ws)
            for c, cw in zip(groups[g], ws):
                share = (w / total if total > 0 else 1 / len(names)) * (cw / inner if inner > 0 else 1 / len(ws))
                p[c.label] = p.get(c.label, 0.0) + share
        top = sorted(p.items(), key=lambda kv: -kv[1])[:3]
        return {"p": round(p.get(op.label, 0.0), 4), "n": len(p), "top": [[label, round(v, 4)] for label, v in top]}

    def _pick(self, items: list, weights: list[float]):
        if sum(weights) <= 0:
            return self.rng.choice(items)
        return self.rng.choices(items, weights=weights)[0]

    def forget_last(self) -> None:
        """「直前に選んだ操作」を忘れる。起動し直したら、前の起動で同じ画面に選んだ操作は直前ではない
        （run 2026-11-09_2259: 前の起動のタイトルで「続きから」を押したので、次の起動のタイトルで「続きから」を候補から外し、
        「はじめから」を押して本編を最初からやり直した）。"""
        self._last.clear()

    def feedback(self, key: str, op: Op, responded: bool) -> None:
        if responded:
            self.misses.pop((key, op.label), None)
        else:
            self.misses[(key, op.label)] += 1
