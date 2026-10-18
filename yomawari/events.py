"""ハーネス内を流れるイベントと、検出された ISSUE のデータ型。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SEVERITIES = ("critical", "major", "minor", "info")


def now_wall() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class Event:
    """センサーやハーネスが発行するイベント。

    source: game（[YOMAWARI] 行）/ log（Unity のログエントリ）/ proc / crash / harness
    kind: game なら ev の値。それ以外はセンサーごとの種別
    t: 受信時の monotonic 時刻（秒）
    """

    source: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    launch: int = 0
    t: float = field(default_factory=time.monotonic)
    wall: str = field(default_factory=now_wall)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Issue:
    rule_id: str
    severity: str
    title: str
    detail: str
    key: str  # 重複排除キー
    launch: int
    t: float
    wall: str
    action: str | None = None  # rules.yaml の on_detect（restart など）
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
