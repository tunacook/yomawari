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
    "purpose": "stuck",  # ランの目的（yomawari/purpose.py）。--purpose で変えられる
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
    "role_window_sec": 5.0,
    # 同じ名前のボタンが並ぶとき、いちばん明るいものの dim_ratio 倍より暗く、差が dim_min_gap 以上のものを「暗い」とし、
    # 押す重みに dim_factor を掛ける（DESIGN.md §9「画像による推定」）。dim_factor = 1 で無効
    "dim_ratio": 0.6,
    "dim_min_gap": 0.1,
    # 画面の見た目から、後ろに下がった画面を判定する（DESIGN.md §9「手前の画面を見た目で判定する」）。ボタンの位置が
    # 変わらないのに、手前にあったときと比べて behind_ratio 倍より暗い（明るい）、かつ差が behind_min_gap 以上になった
    # ボタンが behind_share 以上ある画面は、後ろに下がったとみなす。暗幕は暗いボタンも暗くするので、差は小さく取る
    "behind_ratio": 0.6,
    "behind_min_gap": 0.02,
    "behind_share": 0.6,
    "dim_factor": 0.05,
    "new_bright_factor": 20.0,  # 前に見たときは暗かったのに明るくなったボタン（新しくアンロックされたもの）の重みに掛ける
    "explore_factor": 3.0,  # 見えている並びがどれも押せそうにない・明るさに差がないとき、探す操作（fuzz/search.py）の重みに掛ける
    "hover_wait_sec": 0.3,  # 明るさを測る前に、カーソルをボタンのない場所へ動かしてから待つ時間（ホバーの演出が戻るまで）
    "quiet_wait_sec": 1.0,  # 暗い・空のボタン（反応しないと見込んでいるもの）を押したあと、反応を待つ時間
    # 同じ名前の並びで、まだ見ていないボタンが画面の外にある向きへ動かす操作（fuzz/search.py）の重み。
    # [fuzz.weights] click × 画面の重み × search_factor。見えているものがすべて暗い・押しても反応しなかった・明るさに差がないときは explore_factor も掛ける
    "search_factor": 1.0,
    # 前に見て明るかった（開示済みと分かっている）ボタンが画面の外にあるとき、そこへ戻る操作の重み。click × 画面の重み × return_factor。
    # まだ見ていないボタンを探す（search_factor）のとは別の行動として扱う
    "return_factor": 3.0,
    "search_give_up": 6,  # その向きへ動かしても続けてこの回数反応しなければ、動かせないとみなしてやめる
    # ゲームが開いた画面で、見えているものがどれも押せないとき、向きを決めずに動かして探す回数の上限（開き直すと数え直す）
    "blind_search_limit": 30,
    # 進め方の経験（DESIGN.md §9「経験の蓄積」）。押してから progress_window_sec のうちに、まだ見ていない本文・scene・選択肢に
    # 届いたら「進行につながった」、ロードかタイトル戻りが始まったら「巻き戻した」と数え、random モードで重みに掛ける
    "progress_window_sec": 3.0,
    "progress_bonus": 5.0,  # 押すといつも進行につながる操作の重みに掛ける（割合に応じて 1〜この値）
    "rewind_factor": 0.1,  # 押すといつも巻き戻す操作の重みに掛ける（割合に応じて 1〜この値）
    # プレイの目的（random モード）。起動したときとタイトルに戻ったときに、重みで1つ選ぶ（ランの最初のプレイは progress）。
    # progress: 本編を先へ進める（寄り道を減らし、進め方の経験を使う）、explore: 画面を見て回る（寄り道を増やす）
    "intent_progress": 3.0,
    "intent_explore": 1.0,
    # 寄り道を始める操作（本文を読んでいるときの右クリック・ホイール上、本文中に出ている操作のボタン）の重みに掛ける
    "progress_detour_factor": 0.2,
    "explore_detour_factor": 3.0,
    # S-02 / S-03（ゲームは動いているが進まない）のとき、再起動する前に、抜け出す操作をこの回数まで試す
    "recover_ops": 8,
    # random モードで、進め方の経験（進行につながった操作を優先する）を使う。目的（purpose）で変える
    "learn": True,
    "weights": {"advance": 20, "right_click": 1, "wheel_up": 1, "click": 5, "move": 2, "leave": 1},
}
GUARD_KINDS = ("expect_exit", "external", "irregular_only", "deny")
# ボタンの役割（menu の role）→ 区分の既定値（DESIGN.md §9 ガード）。"normal" は通常の操作。yomawari.example.toml と揃える
GUARD_ROLE_DEFAULTS: dict[str, str] = {
    "exit": "expect_exit",
    "external": "external",
    "to_title": "irregular_only",
    "system_setting": "irregular_only",
    "new_game": "normal",
    "deny": "deny",
}


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
    profile: Path | None = None  # ゲームのプロファイル（yomawari.profile.toml）
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
    def purposes(self) -> dict[str, Any]:
        """[purpose.<名前>] の上書き（yomawari/purpose.py）。"""
        raw = self.doc.get("purpose", {})
        return raw.unwrap() if hasattr(raw, "unwrap") else dict(raw)

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
            profile=opt_path("profile"),
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
