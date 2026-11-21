"""再起動の前に、そのときの状況（画面の写しと、ハーネスが知っているゲームの状態）をランのフォルダに残す。

再起動すると画面は消えるので、検証が終わってから原因を調べられるように、検出した時点で残す（run 2026-11-21_2157:
画面のボタンがどれも反応しなくなった原因を、ログからは調べられなかった）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .sensors.screen import Shot, to_png
from .state import GameState

DIR_NAME = "snapshots"


def state_summary(state: GameState) -> dict[str, Any]:
    """ゲームの状態のうち、止まった原因を調べるのに要るもの。"""
    menus = sorted(state.menus.values(), key=lambda m: m.order)
    return {
        "scene": state.scene,
        "line": state.line,
        "line_done": state.line_done,
        "command_line": state.command_line,
        "menus": [
            {"name": m.name, "modal": m.modal, "order": m.order,
             "buttons": [{"name": b.name, "nth": b.nth, "rect": list(b.rect), "text": b.text, "type": b.type,
                          "locked": b.locked, "hidden": b.hidden, "covered": b.covered} for b in m.buttons]}
            for m in menus
        ],
        "choices": [{"name": c.name, "rect": list(c.rect), "text": c.text} for c in state.choices],
        "selected": state.selected,
        "muted": sorted(state.muted),
        "advance_muted": state.advance_muted,
        "transitions": sorted(state.transitions),
        "saving": state.saving,
        "loading": state.loading,
        "read_text": dict(state.read_text),
    }


def take(run_dir: Path, name: str, info: dict[str, Any], state: GameState, shot: Shot | None) -> dict[str, str]:
    """run_dir/snapshots/<name>.json（状態）と <name>.png（画面の写し。撮れなければなし）を書く。
    戻り値はランのフォルダからの相対パス（json / png）。"""
    d = run_dir / DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    if shot is not None:
        (d / f"{name}.png").write_bytes(to_png(shot))
        out["png"] = f"{DIR_NAME}/{name}.png"
    body = {**info, "state": state_summary(state)}
    (d / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out["json"] = f"{DIR_NAME}/{name}.json"
    return out
