"""ランの目的（DESIGN.md §9「ランの目的」）。

デバッグの目的（進行不能を探す、ボタンの不具合を探す、ガチャプレイ、最短で進める…）ごとに、操作のモードと
[fuzz] の設定の上書き、レポートで先に見せるルールをまとめて決める。`yomawari run --purpose <名前>` で選び、
指定がなければ yomawari.toml の [run] purpose を使う。

組み込みの目的は PURPOSE_DEFAULTS にあり、yomawari.toml の [purpose.<名前>] で上書きしたり、新しい目的を足したり
できる（description / mode / focus / [purpose.<名前>.fuzz]）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODES = ("fuzz", "random", "aging")

# 組み込みの目的。yomawari.example.toml と揃える
PURPOSE_DEFAULTS: dict[str, dict[str, Any]] = {
    "stuck": {
        "description": "進行不能を探す。本編を進めつつ、ロード・回想などの寄り道と行き来もして、先へ進めなくなる経路を探す。"
                       "進めなくなったら、抜け出す操作を試さずにそのまま記録する",
        "mode": "random",
        "focus": ["S-02", "S-03", "S-01", "H-01"],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 1.0, "recover_ops": 0, "learn": True},
    },
    "play": {
        "description": "通常のプレイ。人が遊ぶように本編を先へ進め、寄り道は少なめにする",
        "mode": "random",
        "focus": [],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 0.0, "learn": True},
    },
    "buttons": {
        "description": "ボタンの不具合を探す。画面のボタンを網羅的に押し、押しても何も起きない・暗いのに押せる・"
                       "別の画面へ飛ぶものを見る。進め方の経験は使わない",
        "mode": "random",
        "focus": ["S-05", "V-07", "S-01"],
        "fuzz": {"intent_progress": 0.0, "intent_explore": 1.0, "learn": False, "dim_factor": 0.3,
                 "weights": {"click": 10}},
    },
    "gacha": {
        "description": "ガチャプレイ。連打・タイミングをずらした入力・イレギュラーな操作を多めにして、落ちる・固まる操作を探す",
        "mode": "fuzz",
        "focus": ["C-01", "C-02", "C-03", "C-04", "H-01", "L-01"],
        "fuzz": {"irregular_prob": 0.3, "trigger_prob": 0.6, "learn": False},
    },
    "rta": {
        "description": "最短で進める。進行につながった操作を強く優先し、寄り道と巻き戻しを避ける。どこまで届いたかを記録する",
        "mode": "random",
        "focus": ["S-02", "S-03"],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 0.0, "learn": True, "progress_bonus": 20.0,
                 "rewind_factor": 0.02, "progress_detour_factor": 0.05},
    },
    "aging": {
        "description": "放置。操作せずに起動したままにして、ハング・メモリの増加・ログの異常を見る",
        "mode": "aging",
        "focus": ["H-01", "L-01", "C-01"],
        "fuzz": {},
    },
}
DEFAULT_PURPOSE = "stuck"


class PurposeError(ValueError):
    pass


@dataclass
class Purpose:
    name: str
    description: str
    mode: str
    focus: list[str] = field(default_factory=list)  # レポートで先に見せるルールの ID
    fuzz: dict[str, Any] = field(default_factory=dict)  # [fuzz] の上書き（weights は項目ごとに上書き）

    def apply(self, fuzz_cfg: dict[str, Any]) -> dict[str, Any]:
        """[fuzz] の設定に、目的の上書きを重ねたもの。"""
        out = {**fuzz_cfg, **{k: v for k, v in self.fuzz.items() if k != "weights"}}
        out["weights"] = {**fuzz_cfg["weights"], **dict(self.fuzz.get("weights") or {})}
        return out


def _plain(v: Any) -> Any:
    return v.unwrap() if hasattr(v, "unwrap") else v


def purposes(overrides: dict[str, Any] | None = None) -> dict[str, Purpose]:
    """組み込みの目的に、yomawari.toml の [purpose.<名前>] を重ねたもの。"""
    raw: dict[str, dict[str, Any]] = {k: dict(v) for k, v in PURPOSE_DEFAULTS.items()}
    for name, table in (_plain(overrides) or {}).items():
        base = raw.setdefault(str(name), {"description": "", "mode": "random", "focus": [], "fuzz": {}})
        table = dict(_plain(table))
        fuzz = {**base.get("fuzz", {}), **dict(table.pop("fuzz", {}) or {})}
        if "weights" in fuzz and "weights" in base.get("fuzz", {}):
            fuzz["weights"] = {**base["fuzz"]["weights"], **dict(fuzz["weights"])}
        raw[str(name)] = {**base, **table, "fuzz": fuzz}
    out = {}
    for name, d in raw.items():
        mode = str(d.get("mode", "random"))
        if mode not in MODES:
            raise PurposeError(f"目的 {name} の mode が不正です: {mode}（{' / '.join(MODES)}）")
        out[name] = Purpose(name, str(d.get("description", "")), mode, [str(x) for x in d.get("focus") or []],
                            dict(d.get("fuzz") or {}))
    return out


def resolve(name: str, overrides: dict[str, Any] | None = None) -> Purpose:
    found = purposes(overrides)
    if name not in found:
        raise PurposeError(f"目的が見つかりません: {name}（{' / '.join(found)}）")
    return found[name]
