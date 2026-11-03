"""ガード（DESIGN.md §9）: ボタンごとの扱い（区分）を決める。

区分は、名前での指定（[guard.buttons] の "<menu>/<ボタン>"）があればそれ、なければボタンの役割を
[guard.roles] で引いたもの。役割は RoleBook（確定 > エンジンの標準部品 > 推定）から。どちらもなければ通常の操作。
"""

from __future__ import annotations

from ..config import GUARD_ROLE_DEFAULTS
from ..state import Target
from .roles import RoleBook


class Guard:
    def __init__(
        self, buttons: dict[str, list[str]], roles: dict[str, str] | None = None, book: RoleBook | None = None,
    ) -> None:
        self._kind: dict[str, str] = {}
        for kind, keys in buttons.items():
            for key in keys:
                self._kind[key] = kind
        self._roles = dict(GUARD_ROLE_DEFAULTS if roles is None else roles)
        self.book = book or RoleBook()
        self._external_used: set[str] = set()

    def kind_of(self, target: Target) -> str | None:
        """expect_exit / external / irregular_only / deny。通常の候補なら None。"""
        kind = self._kind.get(target.key)
        if kind is None:
            role = self.book.role_of(target)
            if role:
                kind = self._roles.get(role)
        return None if kind == "normal" else kind

    def named(self, target: Target) -> bool:
        """[guard.buttons] で名前を指定したボタンか。"""
        return target.key in self._kind

    def external_available(self, key: str) -> bool:
        """external は1回のランで1回だけ押す。"""
        return key not in self._external_used

    def use_external(self, key: str) -> None:
        self._external_used.add(key)
