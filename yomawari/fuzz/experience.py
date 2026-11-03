"""進め方の経験（DESIGN.md §9「経験の蓄積」）。

状態（state_key）× 操作ごとに、押した回数・進行につながった回数・巻き戻した回数を数え、ランをまたいで
runs/_experience/<ゲーム>.json の "progress" に足し合わせる。random モードでは、進行につながった操作を優先し、
巻き戻した操作を後回しにする（Decider が weight() を掛ける）。

- 進行: このランでまだ見ていない本文の行（line_id）・scene・選択肢に届いた。ロードで戻った先や、ロードのあとに
  読み直す行は、もう見たものなので数えない
- 巻き戻し: ロードが始まった、またはタイトル画面に戻った

どちらも、起きる前 progress_window_sec のうちに押した最後の操作と、いま開いている寄り道の画面を開いた操作
（右クリックメニューの「ロード」など）のものとして数える。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("tries", "progress", "rewind")


@dataclass
class Press:
    key: str  # state_key
    label: str  # 操作の label
    t: float  # 押した時刻（time.monotonic）
    credited: set[str] = field(default_factory=set)  # すでに数えた結果（progress / rewind）


class Experience:
    def __init__(self, path: Path | None = None, build: str | None = None) -> None:
        self.path = path
        self.build = build
        # state_key → 操作の label → {"tries", "progress", "rewind"}
        self.ops: dict[str, dict[str, dict[str, int]]] = {}
        self.reached: set[str] = set()  # このランで届いた本文の行・scene・選択肢
        self.new: list[str] = []  # このランで初めて進行につながった操作（レポート用）
        self._loaded: set[tuple[str, str]] = set()  # 読み込んだ時点で進行につながったことのあった (state, 操作)
        if path is not None and path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, ops in (doc.get("progress") or {}).items():
            if not isinstance(ops, dict):
                continue
            for label, c in ops.items():
                if isinstance(c, dict):
                    self.ops.setdefault(key, {})[label] = {k: int(c.get(k, 0) or 0) for k in KINDS}
                    if self.ops[key][label]["progress"]:
                        self._loaded.add((key, label))

    def save(self) -> None:
        """ほかの記録（roles など）は残して、"progress" だけを書き換える。"""
        if self.path is None:
            return
        doc: dict[str, Any] = {}
        if self.path.exists():
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
        doc["progress"] = {k: dict(sorted(v.items())) for k, v in sorted(self.ops.items())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _count(self, key: str, label: str, kind: str) -> None:
        c = self.ops.setdefault(key, {}).setdefault(label, dict.fromkeys(KINDS, 0))
        c[kind] += 1

    def pressed(self, key: str, label: str) -> None:
        self._count(key, label, "tries")

    def reach(self, place: str) -> bool:
        """本文の行・scene・選択肢に届いた。このランで初めてなら True（進行）。"""
        if place in self.reached:
            return False
        self.reached.add(place)
        return True

    def credit(self, presses: list[Press], kind: str) -> list[Press]:
        """進行（progress）か巻き戻し（rewind）を、操作ごとに1回だけ数える。数えたものを返す。"""
        out = []
        for p in presses:
            if kind in p.credited:
                continue
            p.credited.add(kind)
            self._count(p.key, p.label, kind)
            out.append(p)
            if kind == "progress" and (p.key, p.label) not in self._loaded and p.key != "reading":
                # 本文送り以外で初めて進行につながったもの（選択画面の正解など）
                self._loaded.add((p.key, p.label))
                self.new.append(f"状態 `{p.key}` で `{p.label}` を押すと先に進んだので、次から優先する")
        return out

    def factor(self, key: str, label: str, bonus: float, rewind_factor: float) -> float:
        """重みに掛けるもの。進行につながった割合だけ bonus へ、巻き戻した割合だけ rewind_factor へ近づける。"""
        c = (self.ops.get(key) or {}).get(label)
        if not c or not c["tries"]:
            return 1.0
        tries = c["tries"]
        prog = min(c["progress"], tries) / tries
        rew = min(c["rewind"], tries) / tries
        return (1 + (bonus - 1) * prog) * (1 - (1 - rewind_factor) * rew)
