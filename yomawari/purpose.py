"""ランの目的（DESIGN.md §9「ランの目的」）。

デバッグの目的（進行不能を探す、ボタンの不具合を探す、ガチャプレイ、最短で進める…）ごとに、操作のモードと
[fuzz] の設定の上書き、レポートで先に見せるルールをまとめて決める。`yomawari run --purpose <名前>` で選び、
指定がなければ yomawari.toml の [run] purpose を使う。

組み込みの目的は PURPOSE_DEFAULTS にあり、yomawari.toml の [purpose.<名前>] で上書きしたり、新しい目的を足したり
できる（description / mode / focus / [purpose.<名前>.fuzz] / [purpose.<名前>.burst]）。

burst は、通常のプレイで先へ進めながら、ある地点（初めて来た画面・scene）でときどき切り替える区間（ガチャプレイ・
ボタンの網羅）。sec（長さ）/ prob（初めての地点で始める確率）/ share（ランのうち区間に使ってよい割合）/ mode /
intent（その区間のプレイの目的。explore で画面を見て回る）/ fuzz（その区間の [fuzz] の上書き）を書く。
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
        "fuzz": {"intent_progress": 1.0, "intent_explore": 1.0, "recover_ops": 0, "learn": True,
                 "saved_slot_factor": 1.0},
    },
    "play": {
        "description": "通常のプレイ。人が遊ぶように本編を先へ進め、寄り道は少なめにする",
        "mode": "random",
        "focus": [],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 0.0, "learn": True},
    },
    "buttons": {
        "description": "ボタンの不具合を探す。通常のプレイで先へ進めながら、初めて来た画面で、ときどきその画面のボタンを"
                       "網羅的に押す（暗いボタンも押す）。押しても何も起きない・暗いのに押せる・別の画面へ飛ぶものを見る",
        "mode": "random",
        "focus": ["S-05", "V-07", "S-01"],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 0.0, "learn": True},
        "burst": {"sec": 60, "prob": 0.3, "share": 0.5, "mode": "random", "intent": "explore",
                  "fuzz": {"learn": False, "use_flow": False, "ai_choice": False, "dim_factor": 0.3,
                           "saved_slot_factor": 1.0, "weights": {"click": 10}}},
    },
    "gacha": {
        "description": "ガチャプレイ。通常のプレイで先へ進めながら、初めて来た画面・scene で、ときどきその場で連打・"
                       "タイミングをずらした入力・イレギュラーな操作をして、落ちる・固まる操作を探す",
        "mode": "random",
        "focus": ["C-01", "C-02", "C-03", "C-04", "H-01", "L-01"],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 0.0, "learn": True},
        "burst": {"sec": 60, "prob": 0.3, "share": 0.5, "mode": "fuzz",
                  "fuzz": {"irregular_prob": 0.3, "trigger_prob": 0.6, "learn": False, "use_flow": False,
                           "ai_choice": False, "saved_slot_factor": 1.0}},
    },
    "rta": {
        "description": "最短で進める。進行につながった操作を強く優先し、寄り道と巻き戻しを避ける。どこまで届いたかを記録する",
        "mode": "random",
        "focus": ["S-02", "S-03"],
        "fuzz": {"intent_progress": 1.0, "intent_explore": 0.0, "learn": True, "progress_bonus": 20.0,
                 "rewind_factor": 0.02, "progress_detour_factor": 0.05, "edge_prob": 0.0},
    },
    "aging": {
        "description": "放置。操作せずに起動したままにして、ハング・メモリの増加・ログの異常を見る",
        "mode": "aging",
        "focus": ["H-01", "L-01", "C-01"],
        "fuzz": {},
    },
}
DEFAULT_PURPOSE = "play"


class PurposeError(ValueError):
    pass


@dataclass
class Purpose:
    name: str
    description: str
    mode: str
    focus: list[str] = field(default_factory=list)  # レポートで先に見せるルールの ID
    fuzz: dict[str, Any] = field(default_factory=dict)  # [fuzz] の上書き（weights は項目ごとに上書き）
    burst: Burst | None = None  # ある地点でときどき切り替える区間

    def apply(self, fuzz_cfg: dict[str, Any]) -> dict[str, Any]:
        """[fuzz] の設定に、目的の上書きを重ねたもの。"""
        return _merge(fuzz_cfg, self.fuzz)


@dataclass
class Burst:
    """通常のプレイの途中で、ある地点でときどき切り替える区間（ガチャプレイ・ボタンの網羅）。"""

    sec: float
    prob: float
    share: float
    mode: str
    intent: str | None = None
    fuzz: dict[str, Any] = field(default_factory=dict)

    def apply(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """通常のプレイの設定（目的の上書きを重ねたもの）に、区間の上書きを重ねたもの。"""
        return _merge(cfg, self.fuzz)


def _merge(cfg: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = {**cfg, **{k: v for k, v in over.items() if k != "weights"}}
    out["weights"] = {**cfg["weights"], **dict(over.get("weights") or {})}
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
        burst = None
        if "burst" in table or "burst" in base:
            b0 = dict(base.get("burst") or {})
            b1 = dict(_plain(table.pop("burst", None)) or {})
            f0, f1 = dict(b0.get("fuzz") or {}), dict(_plain(b1.pop("fuzz", None)) or {})
            bf = {**f0, **f1}
            if "weights" in f0 and "weights" in f1:
                bf["weights"] = {**f0["weights"], **dict(f1["weights"])}
            burst = {**b0, **b1, "fuzz": bf}
        raw[str(name)] = {**base, **table, "fuzz": fuzz, "burst": burst}
    out = {}
    for name, d in raw.items():
        mode = str(d.get("mode", "random"))
        if mode not in MODES:
            raise PurposeError(f"目的 {name} の mode が不正です: {mode}（{' / '.join(MODES)}）")
        out[name] = Purpose(name, str(d.get("description", "")), mode, [str(x) for x in d.get("focus") or []],
                            dict(d.get("fuzz") or {}), _burst(name, d.get("burst")))
    return out


def _burst(name: str, raw: Any) -> Burst | None:
    if not raw:
        return None
    unknown = sorted(set(raw) - {"sec", "prob", "share", "mode", "intent", "fuzz"})
    if unknown:
        raise PurposeError(f"目的 {name} の burst に知らない項目があります: {', '.join(unknown)}（sec / prob / share / mode / intent / fuzz）")
    mode = str(raw.get("mode", "random"))
    if mode not in ("fuzz", "random"):
        raise PurposeError(f"目的 {name} の burst.mode は fuzz か random にしてください: {mode}")
    intent = raw.get("intent")
    if intent not in (None, "progress", "explore"):
        raise PurposeError(f"目的 {name} の burst.intent は progress か explore にしてください: {intent}")
    return Burst(float(raw.get("sec", 60)), float(raw.get("prob", 0.3)), float(raw.get("share", 0.5)), mode, intent,
                 dict(raw.get("fuzz") or {}))


def resolve(name: str, overrides: dict[str, Any] | None = None) -> Purpose:
    found = purposes(overrides)
    if name not in found:
        raise PurposeError(f"目的が見つかりません: {name}（{' / '.join(found)}）")
    return found[name]
