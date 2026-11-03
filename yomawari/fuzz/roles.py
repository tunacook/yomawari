"""ボタンの役割の推定と確定（DESIGN.md §9 ガード「役割の推定と判定」）。

押す前に、ボタンの文字（menu の text）と名前から、rules.yaml の role_hints のキーワードで役割を推定する。
押したあとに観測した事実（終了コード 0 の終了、前面のアプリの切り替わり、タイトル画面の再表示、解像度の変化）で確定する。
推定が外れていたら（exit と推定したのに終わらなかったなど）取り消す。
覚えたことは runs/_experience/<ゲーム>.json に保存し、ランをまたいで使う。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..detect.engine import RuleError
from ..state import Target

ROLES = ("exit", "external", "to_title", "system_setting", "new_game", "deny")
# 押した結果で確定できる役割（deny は押さないので確定しない、new_game は観測で区別できない）
CONFIRMABLE = ("exit", "external", "to_title", "system_setting")


class RoleHintError(RuleError):
    pass


@dataclass
class RoleHints:
    """役割ごとのキーワード。text はボタンの文字、name はボタンの名前（GameObject 名）と照らし合わせる。"""

    text: dict[str, list[str]] = field(default_factory=dict)
    name: dict[str, list[str]] = field(default_factory=dict)

    def estimate(self, target: Target) -> tuple[str, str] | None:
        """(役割, 根拠) を返す。文字で決まればそれ、なければ名前。複数の役割に当てはまれば推定しない。"""
        if target.text:
            roles = {r for r, words in self.text.items() if any(_text_match(w, target.text) for w in words)}
            if len(roles) == 1:
                return roles.pop(), f"文字「{target.text}」"
            if roles:
                return None
        tokens = _name_tokens(target.name)
        roles = {r for r, words in self.name.items() if any(_tokens_match(w, tokens) for w in words)}
        if len(roles) == 1:
            return roles.pop(), f"名前「{target.name}」"
        return None


def load_role_hints(path: Path) -> RoleHints:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as e:
        raise RoleHintError(f"rules.yaml を読めません: {path} ({e})") from e
    raw = doc.get("role_hints") or {}
    hints = RoleHints()
    for role, spec in raw.items():
        if role not in ROLES:
            raise RoleHintError(f"rules.yaml の role_hints の役割が正しくありません: {role}（{', '.join(ROLES)}）")
        for kind in ("text", "name"):
            words = (spec or {}).get(kind, [])
            if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
                raise RoleHintError(f"rules.yaml の role_hints.{role}.{kind} は文字列の配列にしてください")
            getattr(hints, kind)[role] = list(words)
    return hints


def _text_match(word: str, text: str) -> bool:
    # 英数字の語は単語として（"Title" が "Subtitle" に当たらないように）、日本語などは部分一致で
    if word.isascii():
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return word in text


def _name_tokens(name: str) -> list[str]:
    """"ToExitButton (1)" → ["to", "exit", "button", "1"]。CamelCase、_、-、空白、数字で区切る。"""
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[^\x00-\x7f]+", name)
    return [p.lower() for p in parts]


def _tokens_match(word: str, tokens: list[str]) -> bool:
    # "screen mode" のような複数語は、続けて並んでいるときだけ当てはめる
    w = _name_tokens(word)
    return any(tokens[i:i + len(w)] == w for i in range(len(tokens) - len(w) + 1)) if w else False


@dataclass
class RoleEntry:
    role: str
    status: str  # estimated | confirmed | cancelled
    evidence: str
    build: str | None = None


class RoleBook:
    """ボタン（Target.label。同じ名前が並ぶときは "<menu>/<name>#<n>"）ごとの役割。推定は押す前、確定と取り消しは押したあと。

    同じ名前のボタン（スロットなど）を名前でまとめて覚えると、1つで観測したことが並び全体に効いてしまうので、1つずつ覚える。
    """

    def __init__(self, hints: RoleHints | None = None, path: Path | None = None, build: str | None = None) -> None:
        self.hints = hints or RoleHints()
        self.path = path
        self.build = build
        self.entries: dict[str, RoleEntry] = {}
        self.new: list[str] = []  # このランで新しく推定・確定・取り消したもの（レポート用）
        if path is not None and path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # 壊れていたら捨てて覚え直す
        for key, e in (doc.get("roles") or {}).items():
            if isinstance(e, dict) and e.get("role") in ROLES and e.get("status") in ("estimated", "confirmed", "cancelled"):
                self.entries[key] = RoleEntry(e["role"], e["status"], str(e.get("evidence", "")), e.get("build"))

    def save(self) -> None:
        if self.path is None:
            return
        doc: dict[str, Any] = {}
        if self.path.exists():
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
        doc["roles"] = {k: vars(e) for k, e in sorted(self.entries.items())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def role_of(self, target: Target) -> str | None:
        """確定 > ① エンジンの標準部品（menu の role）> 推定、の順。取り消した推定は使わない。"""
        e = self.entries.get(target.label)
        if e is not None and e.status == "confirmed":
            return e.role
        if target.role:
            return target.role
        if e is None:
            guess = self.hints.estimate(target)
            if guess is None:
                return None
            role, why = guess
            e = self.entries[target.label] = RoleEntry(role, "estimated", why, self.build)
            self.new.append(f"`{target.label}` は{why}から `{role}` と推定した")
        return e.role if e.status == "estimated" else None

    def is_estimate(self, target: Target) -> bool:
        e = self.entries.get(target.label)
        return target.role is None and e is not None and e.status == "estimated"

    def confirm(self, key: str, role: str, observed: str) -> bool:
        """観測で確定する。新しく確定したら True。"""
        e = self.entries.get(key)
        if e is not None and e.status == "confirmed" and e.role == role:
            return False
        why = f"{e.evidence}から `{e.role}` と推定し、" if e is not None and e.status == "estimated" else ""
        self.entries[key] = RoleEntry(role, "confirmed", observed, self.build)
        self.new.append(f"`{key}` は{why}押すと{observed}ので `{role}` と確定した")
        return True

    def cancel(self, key: str, observed: str) -> None:
        """推定が外れていた。以降は推定を使わない（同じキーワードでまた推定し直さない）。"""
        e = self.entries.get(key)
        if e is None or e.status != "estimated":
            return
        e.status = "cancelled"
        self.new.append(f"`{key}` は{e.evidence}から `{e.role}` と推定したが、押すと{observed}ので取り消した")
