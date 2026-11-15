"""前回のランとの比較（DESIGN.md §12「レポート構成」）。

比べる相手は、同じビルドの、このランより前に始めたいちばん新しいラン（`events.jsonl` があるもの）。検出は、レポートの
「やること」と同じまとめ方（post/digest.py の item_key）で項目にして、項目ごとに「新規」「継続」「解消」に分ける。
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..events import Issue
from .digest import item_key
from .stalls import load_events


@dataclass
class PrevRun:
    run_id: str
    duration_sec: float | None
    items: dict[tuple[str, ...], tuple[str, int]] = field(default_factory=dict)  # 項目 → (見出し, 回数)


def previous_run(runs_dir: Path, run_id: str, build_name: str) -> Path | None:
    """同じビルドの、run_id より前に始めたいちばん新しいラン。"""
    pat = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}_\d{{4}}_{re.escape(build_name)}(_\d+)?")
    try:
        dirs = [p for p in runs_dir.iterdir() if p.is_dir() and pat.fullmatch(p.name) and p.name < run_id]
    except OSError:
        return None
    for d in sorted(dirs, key=lambda p: p.name, reverse=True):
        if (d / "events.jsonl").is_file():
            return d
    return None


def _issues(events: list[dict]) -> list[Issue]:
    names = {f.name for f in dataclasses.fields(Issue)}
    out = []
    for e in events:
        if e.get("kind") == "issue":
            try:
                out.append(Issue(**{k: v for k, v in (e.get("data") or {}).items() if k in names}))
            except TypeError:
                continue
    return out


def _duration(events: list[dict]) -> float | None:
    walls = [e["wall"] for e in events if e.get("wall")]
    try:
        return (datetime.fromisoformat(walls[-1]) - datetime.fromisoformat(walls[0])).total_seconds()
    except (IndexError, ValueError):
        return None


def item_titles(issues: list[Issue]) -> dict[tuple[str, ...], tuple[str, int]]:
    """検出を「やること」と同じ単位にまとめる: 項目 → (見出し, 回数)。見出しは最初の検出から作る。"""
    out: dict[tuple[str, ...], tuple[str, int]] = {}
    for i in issues:
        key = item_key(i)
        if key in out:
            out[key] = (out[key][0], out[key][1] + 1)
        elif key[0] == "button":
            out[key] = (f"`{key[1]}` を押しても反応しない", 1)
        else:
            out[key] = (f"[{i.rule_id}] {i.title}" + (f": {key[1][:60]}" if len(key) > 1 else ""), 1)
    return out


def load_previous(run_dir: Path, build_name: str) -> PrevRun | None:
    prev = previous_run(run_dir.parent, run_dir.name, build_name)
    if prev is None:
        return None
    events = load_events(prev / "events.jsonl")
    return PrevRun(prev.name, _duration(events), item_titles(_issues(events)))
