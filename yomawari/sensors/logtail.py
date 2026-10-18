"""Player.log を tail し、[YOMAWARI] 行と Unity のログエントリに分解する。

Player.log には重大度が出ないため、ここでは「メッセージ＋直後のスタック行」を
1エントリにまとめて渡すだけにし、Error/Exception かどうかの判定は検出器
（rules.yaml のパターン）に任せる。
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PREFIX = "[YOMAWARI] "

# スタックトレースの行。Script Only の "Ns.Cls:Method (args) (at Assets/x.cs:12)"、
# Full の "  at Ns.Cls.Method () [0x00000] in <...>:0"、末尾の "(Filename: ... Line: N)" など
_STACK_RE = re.compile(
    r"^(\s+at\s.+"
    r"|\s*[\w.`<>\[\],+|/$-]+[:.][\w.`<>\[\],+|$-]+\s*\(.*\).*"
    r"|\(Filename: .* Line: -?\d+\))$"
)
_TRAILER_RE = re.compile(r"^\(Filename: .* Line: -?\d+\)$")


@dataclass
class YomawariRecord:
    data: dict[str, Any]
    raw: str


@dataclass
class InvalidRecord:
    """[YOMAWARI] 接頭辞はあるが JSON として読めなかった行（ハーネス側の不調として扱う）。"""

    raw: str
    error: str


@dataclass
class UnityEntry:
    message: str
    stack: list[str] = field(default_factory=list)

    @property
    def has_stack(self) -> bool:
        return any(not _TRAILER_RE.match(s) for s in self.stack)


Record = YomawariRecord | InvalidRecord | UnityEntry


def is_stack_line(line: str) -> bool:
    return bool(_STACK_RE.match(line))


class LogParser:
    """バイト列を受け取り、改行単位でレコードに変換する。部分行はバッファする。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self._entry: UnityEntry | None = None
        self._blank = False

    def feed(self, chunk: bytes) -> list[Record]:
        text = self._partial + self._decoder.decode(chunk)
        lines = text.split("\n")
        self._partial = lines.pop()
        out: list[Record] = []
        for line in lines:
            out.extend(self._line(line.rstrip("\r")))
        return out

    def flush(self) -> list[Record]:
        """しばらく新しい行が来ないときに呼び、保留中のエントリを確定させる。"""
        out: list[Record] = []
        if self._entry is not None:
            out.append(self._entry)
            self._entry = None
        self._blank = False
        return out

    def _line(self, line: str) -> list[Record]:
        out: list[Record] = []
        if line.startswith(PREFIX):
            out.extend(self.flush())
            out.append(self._parse_yomawari(line))
            return out
        if self._entry is not None:
            if _TRAILER_RE.match(line.strip()):
                # Unity はスタックと "(Filename: ...)" の間に空行を入れる
                self._entry.stack.append(line.strip())
                self._blank = False
                out.extend(self.flush())
                return out
            if not line.strip():
                self._blank = True
                return out
            if not self._blank and is_stack_line(line):
                self._entry.stack.append(line.strip())
                return out
            out.extend(self.flush())
        if line.strip() and not _TRAILER_RE.match(line.strip()):
            self._entry = UnityEntry(message=line)
        return out

    @staticmethod
    def _parse_yomawari(line: str) -> Record:
        body = line[len(PREFIX):]
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            return InvalidRecord(raw=line, error=str(e))
        if not isinstance(data, dict) or "ev" not in data:
            return InvalidRecord(raw=line, error="ev フィールドがない")
        return YomawariRecord(data=data, raw=line)


async def tail_file(
    path: Path,
    on_records: Callable[[list[Record]], None],
    stop: asyncio.Event,
    poll_interval: float,
    flush_after: float,
) -> None:
    """path を末尾まで追いかける。stop がセットされたら残りを読み切って終わる。

    ファイルがまだ無ければ出現を待ち、サイズが縮んだら（上書き起動）先頭から読み直す。
    """
    parser = LogParser()
    pos = 0
    idle = 0.0
    while True:
        stopping = stop.is_set()
        chunk = b""
        if path.exists():
            size = path.stat().st_size
            if size < pos:
                pos = 0
                parser = LogParser()
            if size > pos:
                with path.open("rb") as fp:
                    fp.seek(pos)
                    chunk = fp.read()
                pos += len(chunk)
        if chunk:
            idle = 0.0
            recs = parser.feed(chunk)
            if recs:
                on_records(recs)
        else:
            idle += poll_interval
            if idle >= flush_after:
                recs = parser.flush()
                if recs:
                    on_records(recs)
        if stopping:
            recs = parser.feed(b"\n") + parser.flush()
            if recs:
                on_records(recs)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except TimeoutError:
            pass
