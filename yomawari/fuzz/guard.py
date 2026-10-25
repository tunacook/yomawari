"""ガード（DESIGN.md §9）: yomawari.toml の [guard.buttons] で、ボタンごとの扱いを決める。"""

from __future__ import annotations


class Guard:
    def __init__(self, buttons: dict[str, list[str]]) -> None:
        self._kind: dict[str, str] = {}
        for kind, keys in buttons.items():
            for key in keys:
                self._kind[key] = kind
        self._external_used: set[str] = set()

    def kind_of(self, key: str) -> str | None:
        """expect_exit / external / irregular_only / deny。指定がなければ None（通常の候補）。"""
        return self._kind.get(key)

    def external_available(self, key: str) -> bool:
        """external は1回のランで1回だけ押す。"""
        return key not in self._external_used

    def use_external(self, key: str) -> None:
        self._external_used.add(key)
