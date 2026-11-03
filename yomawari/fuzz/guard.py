"""ガード（DESIGN.md §9）: ボタンごとの扱い（区分）を決める。

区分は、名前での指定（[guard.buttons] の "<menu>/<ボタン>"）があればそれ、なければボタンの役割（menu の role）を
[guard.roles] で引いたもの。どちらもなければ通常の操作。
"""

from __future__ import annotations

from ..config import GUARD_ROLE_DEFAULTS
from ..state import Target


class Guard:
    def __init__(self, buttons: dict[str, list[str]], roles: dict[str, str] | None = None) -> None:
        self._kind: dict[str, str] = {}
        for kind, keys in buttons.items():
            for key in keys:
                self._kind[key] = kind
        self._roles = dict(GUARD_ROLE_DEFAULTS if roles is None else roles)
        self._external_used: set[str] = set()

    def kind_of(self, target: Target) -> str | None:
        """expect_exit / external / irregular_only / deny。通常の候補なら None。"""
        kind = self._kind.get(target.key)
        if kind is None and target.role:
            kind = self._roles.get(target.role)
        return None if kind == "normal" else kind

    def external_available(self, key: str) -> bool:
        """external は1回のランで1回だけ押す。"""
        return key not in self._external_used

    def use_external(self, key: str) -> None:
        self._external_used.add(key)
