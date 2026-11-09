"""続きから遊ぶ・セーブ済みのスロット（DESIGN.md §9「続きから遊ぶ」）。

人は、ゲームを閉じて開き直したら、タイトルで「続きから」を選び、ロード画面でいちばん新しいセーブを選んで続きを遊ぶ。
見えているスロットがどれも空なら、ページを送って探す。セーブが1つもなければ、はじめから遊ぶ。
本編を進めているあいだは、古いセーブをロードして前の場面に戻ることはしない。

ハーネスはこれを、ボタンの文字（画面から読んだもの）と名前で見る。キーワードは rules.yaml の save_hints に置く:
- stamp: セーブ済みのスロットの文字（日時）。正規表現
- resume: 続きから遊ぶボタン
- next_page: ページを送るボタン
- save: セーブするボタン（終了ボタンを試す前にセーブする。DESIGN.md §9「終了を試す前にセーブする」）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..detect.engine import RuleError
from ..state import Target
from .roles import _name_tokens, _text_match, _tokens_match


@dataclass
class SaveHints:
    stamp: list[str] = field(default_factory=list)  # 正規表現
    resume_text: list[str] = field(default_factory=list)
    resume_name: list[str] = field(default_factory=list)
    next_text: list[str] = field(default_factory=list)
    next_name: list[str] = field(default_factory=list)
    save_text: list[str] = field(default_factory=list)
    save_name: list[str] = field(default_factory=list)

    def saved(self, target: Target) -> bool:
        """セーブ済みのスロット（文字に日時がある）か。"""
        return (target.kind == "button" and bool(target.text)
                and any(re.search(p, target.text or "") for p in self.stamp))

    def resume(self, target: Target) -> bool:
        """続きから遊ぶボタンか。"""
        return target.kind == "button" and _hit(target, self.resume_text, self.resume_name)

    def save(self, target: Target) -> bool:
        """セーブするボタンか（セーブ済みのスロットは除く）。"""
        return target.kind == "button" and not self.saved(target) and _hit(target, self.save_text, self.save_name)

    def next_page(self, target: Target) -> bool:
        """ページを送るボタンか。"""
        return target.kind == "button" and _hit(target, self.next_text, self.next_name)


def _hit(target: Target, texts: list[str], names: list[str]) -> bool:
    if target.text and any(_text_match(w, target.text) for w in texts):
        return True
    tokens = _name_tokens(target.name)
    return any(_tokens_match(w, tokens) for w in names)


def stamp_key(text: str | None) -> tuple[int, ...] | None:
    """スロットの文字の日時を、新しいほど大きくなる並びにする。読めた分だけ使う（日付は OCR で崩れやすいので、時刻だけでも。比べ方は newest_slots）。

    "16. 2026-11-06 10:46:59" → (2026, 11, 6, 10, 46, 59)。日付が読めなければ (0, 0, 0, 10, 46, 59)。"""
    if not text:
        return None
    t = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?", text)
    if t is None:
        return None
    d = re.search(r"(20\d\d)\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text[:t.start()])
    date = tuple(int(x) for x in d.groups()) if d else (0, 0, 0)
    if not (1 <= date[1] <= 12 and 1 <= date[2] <= 31):
        # 日付としてありえない（OCR で崩れた。run 2026-11-06_2157: 「2026-14-06」）。読めなかったものとする
        date = (0, 0, 0)
    return (*date, int(t.group(1)), int(t.group(2)), int(t.group(3) or 0))


DAY_SEC = 24 * 60 * 60


def ago_sec(key: tuple[int, ...], wall: float) -> float:
    """スロットの時刻（stamp_key の時・分・秒）が、時刻 wall（time.time()）の何秒前か。日付は見ず、24 時間で一周する
    （0 以上 DAY_SEC 未満。前日 23:29 のセーブは、当日 01:01 から見て 1 時間 32 分前）。"""
    t = time.localtime(wall)
    now = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    return (now - (key[3] * 3600 + key[4] * 60 + key[5])) % DAY_SEC


def older_than(key: tuple[int, ...], wall: float, slack: float) -> bool:
    """スロットの日時（stamp_key）が、時刻 wall（time.time()）より slack 秒以上前か。日付が読めなければ、時刻が何秒前か
    （ago_sec）で比べる。wall より少しあと（slack 秒のうち）の時刻は古いとしない。"""
    if key[:3] != (0, 0, 0):
        t = time.localtime(wall - slack)
        return key < (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    return slack < ago_sec(key, wall) < DAY_SEC - slack


def newest_slots(texts: dict[str, str | None], now: float | None = None) -> set[str]:
    """スロットの label → 文字 から、いちばん新しいセーブのスロット。日時が読めないものは除く。

    日付が読めないスロットが1つでもあれば、どれも時刻だけで、今（now。既定は time.time()）から何秒前か（ago_sec）で比べる
    （日付の読めたものが、読めなかったものよりいつも新しくなってしまうため。run 2026-11-06_1418: 「8.2026 1F06 14:51:52」より
    「9. 2026-1-06 12:58:58」を選び、前の章に戻った）。時刻の大小で比べると、日付をまたいだランで前日のセーブを選ぶ
    （run 2026-11-09_2259: 日付が崩れた前日 23:29 のセーブを、当日 00:49〜01:00 のセーブより新しいとして 3 回ロードした）。"""
    keys = {label: k for label, text in texts.items() if (k := stamp_key(text)) is not None}
    if not keys:
        return set()
    if any(k[:3] == (0, 0, 0) for k in keys.values()):
        wall = time.time() if now is None else now
        keys = {label: (-ago_sec(k, wall),) for label, k in keys.items()}
    top = max(keys.values())
    return {label for label, k in keys.items() if k == top}


def load_save_hints(path: Path) -> SaveHints:
    """rules.yaml の save_hints。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = doc.get("save_hints") or {}

    def words(*keys: str) -> list[str]:
        v = raw
        for k in keys:
            v = (v or {}).get(k) if isinstance(v, dict) else None
        if v is None:
            return []
        if not isinstance(v, list) or not all(isinstance(w, str) and w for w in v):
            raise RuleError(f"rules.yaml の save_hints.{'.'.join(keys)} は文字列の配列にしてください")
        return list(v)

    stamp = words("stamp")
    for p in stamp:
        try:
            re.compile(p)
        except re.error as e:
            raise RuleError(f"rules.yaml の save_hints.stamp の正規表現が正しくありません: {p}（{e}）") from e
    return SaveHints(stamp, words("resume", "text"), words("resume", "name"),
                     words("next_page", "text"), words("next_page", "name"),
                     words("save", "text"), words("save", "name"))
