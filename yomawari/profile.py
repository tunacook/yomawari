"""ゲーム固有の仕様（DESIGN.md §9「ゲーム固有の仕様」③）。

ゲームのリポジトリに置く yomawari.profile.toml と、yomawari.toml の同じ節を、
コードの既定値 → プロファイル → yomawari.toml の順に重ねる（あとのものが上書き。配列は足し合わせる）。
扱う節は [guard.roles]、[guard.buttons]、[screens.<menu>]、[rules.<ID>]、[[flow]]（ゲームの流れ。fuzz/flow.py）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .config import GUARD_KINDS, GUARD_ROLE_DEFAULTS, BuildConfig, Config, ConfigError
from .detect.engine import Rule
from .fuzz.flow import Flow, parse_flows

SCREEN_KEYS = ("weight",)
PROFILE_SECTIONS = ("guard", "screens", "rules", "flow")


@dataclass
class GameSpec:
    guard_buttons: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in GUARD_KINDS})
    guard_roles: dict[str, str] = field(default_factory=lambda: dict(GUARD_ROLE_DEFAULTS))
    screen_weights: dict[str, float] = field(default_factory=dict)
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)  # ルールID → params の上書き
    flows: list[Flow] = field(default_factory=list)  # ゲームの流れ（[[flow]]）
    profile_path: Path | None = None


def merge(base: Any, over: Any) -> Any:
    """辞書は再帰的に重ね、配列は足し合わせ（重複は除く）、それ以外は上書きする。"""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge(out[k], v) if k in out else v
        return out
    if isinstance(base, list) and isinstance(over, list):
        return base + [v for v in over if v not in base]
    return over


def read_profile(path: Path) -> dict[str, Any]:
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"プロファイルを読めません: {path} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"プロファイルの書き方が正しくありません: {path} ({e})") from e
    unknown = sorted(set(doc) - set(PROFILE_SECTIONS))
    if unknown:
        raise ConfigError(f"{path}: 知らない節です: {', '.join(unknown)}（使えるのは {', '.join(PROFILE_SECTIONS)}）")
    return doc


def build_spec(profile: dict[str, Any], local: dict[str, Any], profile_path: Path | None = None) -> GameSpec:
    """プロファイルと yomawari.toml（の guard / screens / rules）を重ねて、検証する。"""
    doc = merge(merge({}, profile), {k: local[k] for k in PROFILE_SECTIONS if k in local})
    spec = GameSpec(profile_path=profile_path)
    guard = doc.get("guard", {})

    for kind, keys in guard.get("buttons", {}).items():
        if kind not in GUARD_KINDS:
            raise ConfigError(f"guard.buttons の区分が正しくありません: {kind}（{', '.join(GUARD_KINDS)}）")
        if not isinstance(keys, list) or not all(isinstance(x, str) for x in keys):
            raise ConfigError(f"guard.buttons.{kind} は文字列の配列にしてください")
        spec.guard_buttons[kind] = [str(x) for x in keys]

    kinds = (*GUARD_KINDS, "normal")
    for role, kind in guard.get("roles", {}).items():
        if role not in GUARD_ROLE_DEFAULTS:
            raise ConfigError(f"guard.roles の役割が正しくありません: {role}（{', '.join(GUARD_ROLE_DEFAULTS)}）")
        if kind not in kinds:
            raise ConfigError(f"guard.roles.{role} の区分が正しくありません: {kind}（{', '.join(kinds)}）")
        spec.guard_roles[role] = str(kind)

    for menu, raw in doc.get("screens", {}).items():
        if not isinstance(raw, dict):
            raise ConfigError(f"screens.{menu} は表にしてください")
        unknown = sorted(set(raw) - set(SCREEN_KEYS))
        if unknown:
            raise ConfigError(f"screens.{menu}: 知らない項目です: {', '.join(unknown)}（{', '.join(SCREEN_KEYS)}）")
        if "weight" in raw:
            w = raw["weight"]
            if not isinstance(w, int | float) or w < 0:
                raise ConfigError(f"screens.{menu}.weight は 0 以上の数にしてください")
            spec.screen_weights[menu] = float(w)

    for rid, params in doc.get("rules", {}).items():
        if not isinstance(params, dict):
            raise ConfigError(f"rules.{rid} は表にしてください")
        spec.rules[rid] = dict(params)
    spec.flows = parse_flows(doc.get("flow"))
    return spec


def apply_rules(rules: dict[str, Rule], spec: GameSpec) -> dict[str, Rule]:
    """rules.yaml のパラメータに、プロファイルの [rules.<ID>] を重ねる。"""
    out = dict(rules)
    for rid, params in spec.rules.items():
        if rid not in out:
            raise ConfigError(f"rules.{rid}: rules.yaml にないルールです")
        rule = out[rid]
        unknown = sorted(set(params) - set(rule.params))
        if unknown:
            raise ConfigError(f"rules.{rid}: rules.yaml にないパラメータです: {', '.join(unknown)}")
        out[rid] = replace(rule, params=merge(rule.params, params))
    return out


def load_spec(cfg: Config, build: BuildConfig) -> GameSpec:
    """ビルドの profile（あれば）と yomawari.toml を重ねる。"""
    profile = read_profile(build.profile) if build.profile else {}
    return build_spec(profile, cfg.doc.unwrap(), build.profile)
