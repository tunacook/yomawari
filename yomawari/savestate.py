"""ラン前のセーブ（persistentDataPath）と PlayerPrefs の退避、ラン後の復元。

ユーザーの本物のセーブを扱うため、削除はせず必ず runs/<id>/backup/ 以下へ移動する。
退避が終わった時点で runs/restore_pending.json を置き、復元が済んだら消す。
異常終了でマーカーが残っていれば、次回起動時（または yomawari restore）に復元する。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import BuildConfig
from .platform import GameIdentity, Platform

MARKER_NAME = "restore_pending.json"
# ゲームが boot で教えたセーブの場所（persistentDataPath）を、ビルドごとに覚えるファイル（runs/ の下）
SAVE_DIRS_NAME = "_save_dirs.json"


@dataclass
class SaveTarget:
    save_dir: Path | None
    prefs_domain: str | None


def resolve_target(build: BuildConfig, platform: Platform, learned: Path | None = None) -> SaveTarget:
    """退避するセーブの場所と PlayerPrefs のドメイン。セーブの場所は、設定（builds.<name>.save_dir）、ゲームが前に boot で
    教えた場所（learned）、OS とビルドからの推測の順に使う。推測は、製品名などのビルドの設定によって外れることがある
    （run 2026-11-21_1613: 製品名を英語にしたビルドで、バンドル ID の名前ではなく会社名/製品名のフォルダに書いた）。"""
    guess = platform.guess_identity(build.path)
    ident = GameIdentity(build.company or guess.company, build.product or guess.product, guess.bundle_id)
    return SaveTarget(
        save_dir=build.save_dir or learned or platform.default_save_dir(ident),
        prefs_domain=build.prefs_domain or platform.default_prefs_domain(ident),
    )


def learned_save_dir(runs_dir: Path, build_name: str) -> Path | None:
    """ゲームが前のランの boot で教えたセーブの場所（覚えていなければ None）。"""
    try:
        known = json.loads((runs_dir / SAVE_DIRS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    p = known.get(build_name) if isinstance(known, dict) else None
    return Path(p) if isinstance(p, str) and p else None


def remember_save_dir(runs_dir: Path, build_name: str, save_dir: Path) -> None:
    """ゲームが boot で教えたセーブの場所を、次のランの退避に使うために覚える。"""
    path = runs_dir / SAVE_DIRS_NAME
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    known[build_name] = str(save_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return p.with_name(f"{p.name}.{stamp}")


def previous_run_save(runs_dir: Path, build_name: str, exclude: Path | None = None) -> Path | None:
    """前のラン（同じビルドの、いちばん新しいラン）が終わったときのセーブ（runs/<id>/backup/after_run*/save）。

    ラン中のセーブは、ランの終わりに after_run へ移され、本物のセーブが戻される。--continue では、これを初期セーブとして置く。"""
    pat = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}_\d{{4}}_{re.escape(build_name)}(_\d+)?")
    try:
        dirs = sorted((p for p in runs_dir.iterdir() if p.is_dir() and pat.fullmatch(p.name)),
                      key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for d in dirs:
        if exclude is not None and d.resolve() == exclude.resolve():
            continue
        # ゲームが教えたセーブの場所が違って退避し直したランは、実際のセーブが save_dir_moved の下に残る（最初の退避の
        # after_run より後に作られるので、新しい方を使う）
        saves = [a / "save" for b in (d / "backup", d / "save_dir_moved" / "backup") for a in b.glob("after_run*")
                 if (a / "save").is_dir()]
        if saves:
            return max(saves, key=lambda p: p.parent.stat().st_mtime)
    return None


class SaveState:
    def __init__(self, runs_dir: Path, platform: Platform) -> None:
        self.runs_dir = runs_dir
        self.platform = platform
        self.marker = runs_dir / MARKER_NAME

    def pending(self) -> dict | None:
        if not self.marker.exists():
            return None
        return json.loads(self.marker.read_text(encoding="utf-8"))

    def backup(self, run_dir: Path, target: SaveTarget, initial_save: Path | None,
               initial_label: str = "テスト用初期セーブ") -> list[str]:
        if self.pending():
            raise RuntimeError("未復元の退避があります。先に yomawari restore を実行してください")
        bdir = run_dir / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        state: dict = {"run_dir": str(run_dir), "backup_dir": str(bdir)}

        if target.save_dir is not None:
            existed = target.save_dir.exists()
            if existed:
                shutil.copytree(target.save_dir, bdir / "save", symlinks=True)
            state["save_dir"] = str(target.save_dir)
            state["save_existed"] = existed
            notes.append(f"セーブを退避: {target.save_dir}" + ("" if existed else "（存在しないため空として記録）"))
        else:
            notes.append("セーブの場所が分からないため退避していません（builds.<name>.save_dir で指定できます）")

        if target.prefs_domain is not None:
            existed = self.platform.prefs_export(target.prefs_domain, bdir / "prefs.plist")
            state["prefs_domain"] = target.prefs_domain
            state["prefs_existed"] = existed
            notes.append(f"PlayerPrefs を退避: {target.prefs_domain}" + ("" if existed else "（未作成）"))
        else:
            notes.append("PlayerPrefs のドメインが分からないため退避していません（builds.<name>.prefs_domain で指定できます）")

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.marker.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.marker)

        if initial_save is not None and target.save_dir is not None:
            if target.save_dir.exists():
                shutil.move(str(target.save_dir), str(_unique(bdir / "replaced_by_initial")))
            shutil.copytree(initial_save, target.save_dir, symlinks=True)
            notes.append(f"{initial_label}を配置: {initial_save}")
        return notes

    def restore(self) -> list[str]:
        state = self.pending()
        if state is None:
            return []
        bdir = Path(state["backup_dir"])
        after = _unique(bdir / "after_run")
        after.mkdir(parents=True)
        notes: list[str] = []

        if "save_dir" in state:
            save_dir = Path(state["save_dir"])
            if save_dir.exists():
                shutil.move(str(save_dir), str(after / "save"))
            if state["save_existed"]:
                save_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(bdir / "save", save_dir, symlinks=True)
            notes.append(f"セーブを復元: {save_dir}（ラン後の状態は {after / 'save'}）")

        if "prefs_domain" in state:
            domain = state["prefs_domain"]
            self.platform.prefs_export(domain, after / "prefs.plist")
            src = bdir / "prefs.plist" if state["prefs_existed"] else None
            self.platform.prefs_replace(domain, src)
            notes.append(f"PlayerPrefs を復元: {domain}")

        self.marker.unlink()
        return notes
