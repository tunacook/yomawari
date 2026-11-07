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
    "purpose": "play",  # ランの目的（yomawari/purpose.py）。--purpose で変えられる
    "seed": "auto",
    "open_report": True,
    "keep_runs_days": 14,
    "max_restarts": 20,
    "quit_timeout_sec": 10.0,
    "display_off": False,  # ゲームが起動したあと、画面（ディスプレイ）を消す（macOS: pmset displaysleepnow）。--display-off
    "display_off_delay_sec": 10.0,  # ゲームが起動してから画面を消すまでの時間
    "continue": False,  # ランの最初の起動から、前のランのセーブの続きを遊ぶ（既定ははじめから）。--continue
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
    # random モード（通常のプレイ）で、壊れやすいタイミング（選択肢の直後、scene の境目、演出の途中、セーブ・ロード、
    # 質問に答えた直後。本文が出るたびには差し込まない）に、エッジケースの操作を差し込む確率。fuzz モードは trigger_prob
    "edge_prob": 0.03,
    "menu_flash_sec": 0.05,  # メニューを開いてすぐ閉じる操作で、開いてから閉じるまでの時間
    # 巻き戻しのエッジケース: 続けて戻る・戻ってすぐ送るの回数の上限、モーダル（バックログ）の巻き戻しボタンが出るまで待つ時間、
    # 差し込んだあと本文が前の行に戻ったかを見るまで待つ時間
    "rollback_repeat_max": 4,
    "rollback_menu_wait_sec": 1.0,
    "rollback_check_sec": 2.0,
    "trigger_delay_max_sec": 0.5,
    "irregular_prob": 0.05,
    "rapid_count": 10,
    "stuck_modal_sec": 5.0,
    "external_wait_sec": 3.0,
    "exit_min_interval_sec": 1800.0,
    # 終了ボタンを試す前にセーブする（起動し直したら続きから遊べるように）。セーブしてからこの秒数のうちなら終了を試す（0 で使わない）
    "save_before_exit_sec": 300.0,
    "save_first_sec": 180.0,  # セーブしようとする時間。過ぎたらセーブしないまま終了を試す
    "save_first_factor": 20.0,  # セーブしようとするあいだ、セーブするボタンと空きのスロットの重みに掛ける
    # 長いランでは、expect_exit の間隔をランの長さのこの割合以上にする（起動し直すたびに最初から遊び直すので、
    # 8 時間のランで 30 分ごとに終わらせると先へ進めない）。0 で無効
    "exit_interval_ratio": 0.25,
    "no_response_factor": 0.2,
    "drag_distance": 0.3,
    "role_window_sec": 5.0,
    # 同じ名前のボタンが並ぶとき、いちばん明るいものの dim_ratio 倍より暗く、差が dim_min_gap 以上のものを「暗い」とし、
    # 押す重みに dim_factor を掛ける（DESIGN.md §9「画像による推定」）。dim_factor = 1 で無効
    "dim_ratio": 0.6,
    "dim_min_gap": 0.1,
    # 明るさでは差がない並びで、濃淡（明るさの標準偏差）と鮮やかさのどちらも、いちばん大きいものの fade_ratio 倍より
    # 小さく、差が fade_min_gap 以上のもの（半透明に見えるもの）も「暗い」と同じに扱う（DESIGN.md §9「画像による推定」）
    "fade_ratio": 0.6,
    "fade_min_gap": 0.05,
    # 同じく、鮮やかさだけがいちばん大きいものの grey_ratio 倍より小さいもの（色を抜いた、灰色のもの）も「暗い」と同じに扱う
    "grey_ratio": 0.3,
    # 画面の見た目から、後ろに下がった画面を判定する（DESIGN.md §9「手前の画面を見た目で判定する」）。ボタンの位置が
    # 変わらないのに、手前にあったときと比べて behind_ratio 倍より暗い（明るい）、かつ差が behind_min_gap 以上になった
    # ボタンが behind_share 以上ある画面は、後ろに下がったとみなす。暗幕は暗いボタンも暗くするので、差は小さく取る
    "behind_ratio": 0.6,
    "behind_min_gap": 0.02,
    "behind_share": 0.6,
    # 手前にあったときと位置を比べるとき、ずれがボタンの幅・高さのこの倍以内なら同じ位置とする（開いたあとの小さな動き）
    "behind_move_ratio": 0.15,
    # 後ろに下がったとみなした画面があるあいだ、手前のものを押して続けてこの回数反応がなければ、判定を取り消す
    # （開いている途中で測って誤って後ろとした画面が、実は手前で入力を受けていた）
    "behind_verify_misses": 2,
    # 押して反応したことのあるボタンでも、続けてこの回数反応しなければ、反応しないボタンとして後回しにする
    "answered_miss_limit": 3,
    # 押しても反応しなかったボタンが、そのあと開示されたら（反応しなかったあとに見たいちばん明るい見た目より dim_ratio /
    # dim_min_gap の基準で明るくなった、または読める文字が revive_letters 文字以上増えた）、反応しないボタンから外して候補に戻す
    "revive_letters": 3,
    "dim_factor": 0.05,
    "hover_wait_sec": 0.3,  # 明るさを測る前に、カーソルをボタンのない場所へ動かしてから待つ時間（ホバーの演出が戻るまで）
    "blocked_wait_sec": 5.0,
    "blocked_note_sec": 30.0,  # これより長く入力を受け付けなかったときだけ「ハーネスの記録」に書く  # 画面のロックなどで OS が入力を受け付けないとき、次に試すまで待つ時間
    "ai_choice": True,  # 流れの手順で選ぶもの（言葉・思い出・選択肢）を、本文の文脈から AI に点数を付けさせて先に試す
    "ai_context_lines": 20,  # AI に渡す、質問の前の本文の行数
    "ai_timeout_sec": 60.0,  # AI の答えを待つ時間（裏で待つので、操作は止めない）
    "read_text": True,  # ボタンの文字を画面から読む（OCR。DESIGN.md §9「画面の文字を読む」）
    "read_text_min_conf": 0.3,  # 読んだ文字の確からしさ（0〜1）がこれより低ければ使わない
    "quiet_wait_sec": 1.0,  # 暗い・空のボタン、押しても一度も反応しなかったボタン（反応しないと見込んでいるもの）を押したあと、反応を待つ時間
    # 一覧の目標へ向かう操作（目標を押す・目標の向きへ動かす）の重みに掛ける（DESIGN.md §9「一覧の台帳と目標」。fuzz/listing.py）。
    # 重みは [fuzz.weights] click × 画面の重み × list_factor。試行回数で下げない
    "list_factor": 20.0,
    "list_move_give_up": 6,  # 目標の向きへ動かしても、続けてこの回数目標が近づかなければ、届かないとして目標から外す
    # 矩形が来ず、ページ送りのボタンも見つからない一覧で、ゲームが開いた画面の見えているものがどれも押せないとき、
    # 向きを決めずに動かして探す回数の上限（開き直すと数え直す）
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
    # 一覧を動かす操作（ドラッグ・ホイール・方向キー）の反応を待つ時間。効けば見た目がすぐ変わる。応答確認では S-01 の時間まで待つ
    "move_wait_sec": 1.0,
    # random モードで、進め方の経験（進行につながった操作を優先する）を使う。目的（purpose）で変える
    "learn": True,
    # ゲームの流れ（プロファイルの [[flow]]。fuzz/flow.py）に沿って進める。目的（purpose）で変える
    "use_flow": True,
    "flow_factor": 20.0,  # いまの手順で押すボタン・本文送り・選択肢の重みに掛ける
    "flow_avoid_factor": 0.02,  # 流れの途中で押すと流れから外れるボタン（avoid）の重みに掛ける
    "flow_idle_sec": 10.0,  # 流れのどの画面・scene でもない状態がこれだけ続いたら、流れが途切れたとする
    "flow_retry_lines": 6,  # 流れを最後まで進めて（答えて）からこの行数以内に同じ流れが始まったら、同じ質問に戻ったとする
    # 続きから遊ぶ（fuzz/resume.py）。ラン中にゲームを起動し直したら、この秒数のあいだ「続きから」のボタン・いちばん新しい
    # セーブ済みのスロット・（見えているスロットがどれも空なら）ページを送るボタンの重みに resume_factor を、「はじめから」に
    # resume_new_game_factor を掛ける。0 にすると使わない
    "resume_sec": 180.0,
    "resume_factor": 20.0,
    "resume_new_game_factor": 0.05,
    # 続きから遊ぶとき、見えているいちばん新しいセーブが、このランで最後にセーブした時刻よりこの秒数以上前なら、
    # ほかのページに自分のセーブがあるとして、ページを送って探す。resume_page_limit 回送っても見つからなければ、見えているものでよい
    "resume_stamp_slack_sec": 60.0,
    "resume_page_limit": 10,
    # 続きから遊ぶとき以外に、セーブ済みのスロット（文字に日時がある）の重みに掛ける。0 なら押さない（自分のセーブのロードは
    # 目的があるときだけにする）。ロードとの行き来を試す目的（stuck）や、画面のボタンを網羅的に押す区間では 1 にする
    "saved_slot_factor": 0.0,
    # レポートの「進めなかったところ」: 新しい本文・scene・選択肢にこの秒数以上届かなかった区間を載せる。0 で載せない
    "stall_report_sec": 300.0,
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
