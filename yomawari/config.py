"""yomawari.toml の読み書き、ビルドの解決、対話でのビルド登録。"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table

ENV_CONFIG = "YOMAWARI_CONFIG"
CONFIG_NAME = "yomawari.toml"

# [run] の既定値。yomawari.example.toml と揃える
RUN_DEFAULTS: dict[str, Any] = {
    "hours": 8.0,
    "seed": "auto",
    "open_report": True,
    "keep_runs_days": 14,
    "max_restarts": 20,
    "quit_timeout_sec": 10.0,
}
SENSOR_DEFAULTS: dict[str, Any] = {
    "log_poll_sec": 0.1,
    "log_flush_sec": 0.5,
    "process_poll_sec": 1.0,
    "crash_poll_sec": 2.0,
    "tick_sec": 0.5,
}

# [fuzz] の既定値（DESIGN.md §9）。yomawari.example.toml と揃える
FUZZ_DEFAULTS: dict[str, Any] = {
    "op_interval_sec": 0.5,
    "settle_sec": 0.5,
    "advance_point": [0.5, 0.5],
    "trigger_prob": 0.3,
    "trigger_delay_max_sec": 0.5,
    "irregular_prob": 0.05,
    "rapid_count": 10,
    "stuck_modal_sec": 5.0,
    "external_wait_sec": 3.0,
    "exit_min_interval_sec": 1800.0,
    "no_response_factor": 0.2,
    "drag_distance": 0.3,
    "weights": {"advance": 20, "right_click": 1, "wheel_up": 1, "click": 5, "move": 2, "leave": 1},
}
GUARD_KINDS = ("expect_exit", "external", "irregular_only", "deny")


class ConfigError(ValueError):
    pass


@dataclass
class BuildConfig:
    name: str
    path: Path
    args: list[str] = field(default_factory=list)
    company: str | None = None
    product: str | None = None
    save_dir: Path | None = None
    prefs_domain: str | None = None
    initial_save: Path | None = None
    registered: bool = True  # --build-path でその場限りに使うものは False


def normalize_path_input(text: str) -> Path:
    """Finder からのドラッグ&ドロップや手入力のパスを正規化する。"""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    s = re.sub(r"\\(.)", r"\1", s)  # "\ " などのアンエスケープ
    s = s.rstrip("/") or "/"
    return Path(os.path.expanduser(s))


def find_config_path(option: Path | None) -> Path:
    if option is not None:
        return option.expanduser()
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    return Path.cwd() / CONFIG_NAME


class Config:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.base_dir = self.path.parent
        if self.path.exists():
            try:
                self.doc = tomlkit.parse(self.path.read_text(encoding="utf-8"))
            except Exception as e:  # tomlkit の例外型は多岐にわたる
                raise ConfigError(f"{self.path} を読めません: {e}") from e
        else:
            self.doc = tomlkit.document()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _rel(self, value: str) -> Path:
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else self.base_dir / p

    @property
    def runs_dir(self) -> Path:
        return self._rel(str(self.doc.get("runs_dir", "runs")))

    @property
    def rules_path(self) -> Path:
        return self._rel(str(self.doc.get("rules", "rules.yaml")))

    @property
    def default_build(self) -> str | None:
        v = self.doc.get("default_build")
        return str(v) if v else None

    def section(self, name: str, defaults: dict[str, Any]) -> dict[str, Any]:
        raw = self.doc.get(name, {})
        out = dict(defaults)
        for k, v in raw.items():
            if k in defaults:
                out[k] = v.unwrap() if hasattr(v, "unwrap") else v
        return out

    @property
    def run(self) -> dict[str, Any]:
        return self.section("run", RUN_DEFAULTS)

    @property
    def sensors(self) -> dict[str, Any]:
        return self.section("sensors", SENSOR_DEFAULTS)

    @property
    def fuzz(self) -> dict[str, Any]:
        out = self.section("fuzz", FUZZ_DEFAULTS)
        out["weights"] = {**FUZZ_DEFAULTS["weights"], **dict(out["weights"])}
        return out

    @property
    def guard_buttons(self) -> dict[str, list[str]]:
        """[guard.buttons] の区分 → "<menu>/<ボタン>" の一覧。"""
        raw = self.doc.get("guard", {}).get("buttons", {})
        out: dict[str, list[str]] = {}
        for k in GUARD_KINDS:
            v = raw.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ConfigError(f"guard.buttons.{k} は文字列の配列にしてください")
            out[k] = [str(x) for x in v]
        unknown = set(raw.keys()) - set(GUARD_KINDS)
        if unknown:
            raise ConfigError(f"guard.buttons に不明な区分があります: {', '.join(sorted(unknown))}")
        return out

    @property
    def judge(self) -> dict[str, Any]:
        raw = self.doc.get("judge", {})
        return raw.unwrap() if hasattr(raw, "unwrap") else dict(raw)

    def build_names(self) -> list[str]:
        return list(self.doc.get("builds", {}).keys())

    def get_build(self, name: str) -> BuildConfig:
        builds = self.doc.get("builds", {})
        if name not in builds:
            raise ConfigError(f"ビルド '{name}' は登録されていません")
        raw = builds[name].unwrap()
        if "path" not in raw:
            raise ConfigError(f"builds.{name} に path がありません")

        def opt_path(key: str) -> Path | None:
            return self._rel(raw[key]) if raw.get(key) else None

        return BuildConfig(
            name=name,
            path=Path(os.path.expanduser(raw["path"])),
            args=[str(a) for a in raw.get("args", [])],
            company=raw.get("company"),
            product=raw.get("product"),
            save_dir=opt_path("save_dir"),
            prefs_domain=raw.get("prefs_domain"),
            initial_save=opt_path("initial_save"),
        )

    def resolve_build(self, build: str | None, build_path: Path | None) -> BuildConfig | None:
        """--build-path > --build > default_build の順で解決する。未登録なら None。"""
        if build_path is not None:
            p = normalize_path_input(str(build_path))
            return BuildConfig(name=p.stem.lower(), path=p, registered=False)
        name = build or self.default_build
        if name is None:
            return None
        return self.get_build(name)

    def set_build(self, name: str, path: Path, args: list[str] | None = None) -> None:
        if "builds" not in self.doc:
            builds = tomlkit.table(is_super_table=True)
            self.doc["builds"] = builds
        builds = self.doc["builds"]
        if name in builds:
            builds[name]["path"] = str(path)
            if args is not None:
                builds[name]["args"] = args
        else:
            t: Table = tomlkit.table()
            t["path"] = str(path)
            t["args"] = args or []
            builds[name] = t
        if not self.default_build:
            # default_build は先頭に置く
            items = list(self.doc.items())
            self.doc.clear()
            self.doc["default_build"] = name
            for k, v in items:
                if k != "default_build":
                    self.doc[k] = v

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(tomlkit.dumps(self.doc), encoding="utf-8")


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False
