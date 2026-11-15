"""古いランの削除（`run.keep_runs_days`）。DESIGN.md §12「runs/ 構成」。

消すのは、名前がランの形（`YYYY-MM-DD_HHMM_<ビルド>`）のディレクトリだけ。`_experience` などの `_` で始まるものと、
`coverage.json` などのファイルは消さない。次のものは古くても残す。
- 未復元のセーブ退避があるラン（`restore_pending.json` が指すラン。ユーザーの本物のセーブが入っている）
- ビルドごとのいちばん新しいラン（`--continue` で続きを遊ぶセーブと、前回比のパフォーマンスの記録）
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .savestate import MARKER_NAME

RUN_DIR = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{4})_(.+)")


@dataclass
class PruneResult:
    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)  # 古いが残したラン（名前, 理由）
    errors: list[tuple[str, str]] = field(default_factory=list)


def _runs(runs_dir: Path) -> list[tuple[datetime, str, Path]]:
    out = []
    try:
        entries = list(runs_dir.iterdir())
    except OSError:
        return out
    for p in entries:
        m = RUN_DIR.fullmatch(p.name)
        if not (m and p.is_dir()):
            continue
        try:
            start = datetime.strptime(m.group(1), "%Y-%m-%d_%H%M")
        except ValueError:
            continue
        # 同じ分に始めたランには _2, _3 … が付く（watchdog.new_run_dir）
        out.append((start, re.sub(r"_\d+$", "", m.group(2)), p))
    return out


def _pending_run(runs_dir: Path) -> Path | None:
    try:
        state = json.loads((runs_dir / MARKER_NAME).read_text(encoding="utf-8"))
        return Path(state["run_dir"]).resolve()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prune_runs(runs_dir: Path, keep_days: float, now: datetime, dry_run: bool = False) -> PruneResult:
    """始めてから keep_days 日より経ったランを消す。keep_days が 0 以下なら何もしない。"""
    res = PruneResult()
    if keep_days <= 0:
        return res
    runs = _runs(runs_dir)
    newest: dict[str, tuple[datetime, str]] = {}
    for start, build, p in runs:
        if build not in newest or (start, p.name) > newest[build]:
            newest[build] = (start, p.name)
    pending = _pending_run(runs_dir)
    limit = now - timedelta(days=keep_days)
    for start, build, p in sorted(runs, key=lambda r: r[2].name):
        if start >= limit:
            continue
        if pending is not None and p.resolve() == pending:
            res.kept.append((p.name, "未復元のセーブ退避がある"))
            continue
        if newest[build][1] == p.name:
            res.kept.append((p.name, f"ビルド {build} のいちばん新しいラン"))
            continue
        if not dry_run:
            try:
                shutil.rmtree(p)
            except OSError as e:
                res.errors.append((p.name, str(e)))
                continue
        res.removed.append(p.name)
    return res
