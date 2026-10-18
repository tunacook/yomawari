"""asyncio 内で使う最小のイベントバス。購読者への配送と events.jsonl への記録を行う。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import IO

from .events import Event

Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, jsonl_path: Path | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._fp: IO[str] | None = (
            jsonl_path.open("a", encoding="utf-8") if jsonl_path else None
        )

    def subscribe(self, cb: Subscriber) -> None:
        self._subscribers.append(cb)

    def publish(self, ev: Event) -> None:
        if self._fp is not None and ev.kind != "tick":
            self._fp.write(json.dumps(ev.to_json(), ensure_ascii=False) + "\n")
            self._fp.flush()
        for cb in self._subscribers:
            cb(ev)

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None
