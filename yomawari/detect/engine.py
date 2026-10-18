"""rules.yaml を読み込み、イベントを各ルールの検出器に流して ISSUE を出す。

フェーズ1では C-01〜03・H-01・L-01 を実装する。条件式を汎用に評価するエンジンは
フェーズ2で追加し、ここで定義した検出器はその上に載せ替える。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..events import SEVERITIES, Event, Issue


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    severity: str
    title: str
    description: str = ""
    on_detect: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def param(self, name: str) -> Any:
        if name not in self.params:
            raise RuleError(f"rules.yaml の {self.id} に params.{name} がありません")
        return self.params[name]


def load_rules(path: Path) -> dict[str, Rule]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RuleError(f"rules.yaml を読めません: {path} ({e})") from e
    rules: dict[str, Rule] = {}
    for raw in (doc or {}).get("rules", []):
        try:
            rule = Rule(
                id=raw["id"],
                severity=raw["severity"],
                title=raw["title"],
                description=raw.get("description", ""),
                on_detect=raw.get("on_detect"),
                params=raw.get("params") or {},
            )
        except KeyError as e:
            raise RuleError(f"rules.yaml のルールに {e} がありません: {raw}") from e
        if rule.severity not in SEVERITIES:
            raise RuleError(f"{rule.id}: severity が不正です: {rule.severity}")
        if rule.on_detect not in (None, "restart"):
            raise RuleError(f"{rule.id}: on_detect が不正です: {rule.on_detect}")
        if rule.id in rules:
            raise RuleError(f"ルールIDが重複しています: {rule.id}")
        rules[rule.id] = rule
    return rules


class Detector:
    def __init__(self, rule: Rule) -> None:
        self.rule = rule

    def on_event(self, ev: Event) -> list[Issue]:
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        return []

    def issue(self, detail: str, key: str, launch: int, t: float, wall: str, **evidence: Any) -> Issue:
        return Issue(
            rule_id=self.rule.id,
            severity=self.rule.severity,
            title=self.rule.title,
            detail=detail,
            key=f"{self.rule.id}:{key}",
            launch=launch,
            t=t,
            wall=wall,
            action=self.rule.on_detect,
            evidence=evidence,
        )


class ProcessExitDetector(Detector):
    """C-01"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.ok_codes = set(rule.param("harness_exit_codes"))

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind != "proc_exit":
            return []
        code, by_harness = ev.data["code"], ev.data["by_harness"]
        if by_harness and code in self.ok_codes:
            return []
        why = "ハーネスの終了操作に対して" if by_harness else "ハーネスが終了させていないのに"
        return [
            self.issue(
                f"{why}プロセスが終了しました（終了コード {code}）",
                key=f"exit{code}",
                launch=ev.launch, t=ev.t, wall=ev.wall, exit_code=code,
            )
        ]


class CrashDumpDetector(Detector):
    """C-02"""

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind != "crash_dump":
            return []
        path = ev.data["path"]
        return [
            self.issue(
                f"クラッシュダンプが出現しました: {Path(path).name}",
                key="dump", launch=ev.launch, t=ev.t, wall=ev.wall, dump=path,
            )
        ]


class BootTimeoutDetector(Detector):
    """C-03"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.timeout = float(rule.param("timeout_sec"))
        self.launched_at: float | None = None
        self.launch_wall = ""
        self.fired = False

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind == "launch":
            self.launched_at, self.launch_wall, self.fired = ev.t, ev.wall, False
        elif ev.kind == "boot" or ev.kind == "proc_exit":
            self.launched_at = None
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if self.launched_at is None or self.fired or now - self.launched_at < self.timeout:
            return []
        self.fired = True
        return [
            self.issue(
                f"起動から {self.timeout:g} 秒以内に boot 行が出ませんでした",
                key="noboot", launch=launch, t=now, wall=self.launch_wall,
            )
        ]


class HeartbeatDetector(Detector):
    """H-01"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.timeout = float(rule.param("timeout_sec"))
        self.last: float | None = None
        self.last_wall = ""
        self.cpu: float | None = None
        self.alive = False
        self.fired = False

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind == "launch":
            self.last, self.alive, self.fired, self.cpu = None, True, False, None
        elif ev.kind in ("boot", "heartbeat") and ev.source == "game":
            self.last, self.last_wall = ev.t, ev.wall
        elif ev.kind == "proc_stats":
            self.cpu = ev.data.get("cpu")
        elif ev.kind == "proc_exit":
            self.alive = False
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if not self.alive or self.last is None or self.fired:
            return []
        gap = now - self.last
        if gap < self.timeout:
            return []
        self.fired = True
        cpu = "不明" if self.cpu is None else f"{self.cpu:.0f}%"
        return [
            self.issue(
                f"ハートビートが {gap:.1f} 秒途絶えました（CPU使用率 {cpu}、最終受信 {self.last_wall}）",
                key="hang", launch=launch, t=now, wall=self.last_wall, cpu=self.cpu,
            )
        ]


class LogErrorDetector(Detector):
    """L-01: ゲームが出す log_error イベント（Application.logMessageReceived 由来）で判定する。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.types = set(rule.param("types"))
        self.ignore = [re.compile(p) for p in rule.param("ignore_patterns")]

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.source != "game" or ev.kind != "log_error":
            return []
        kind = str(ev.data.get("type", ""))
        msg = str(ev.data.get("message") or "")
        stack = str(ev.data.get("stack") or "")
        if kind not in self.types or any(p.search(msg) for p in self.ignore):
            return []
        head = msg.splitlines()[0] if msg else ""
        top = next((s for s in stack.splitlines() if s.strip()), "")
        return [
            self.issue(
                f"{kind}: {head[:200]}",
                key=f"{kind}|{head[:80]}|{top}", launch=ev.launch, t=ev.t, wall=ev.wall,
                log="\n".join(x for x in (msg, stack) if x),
            )
        ]


DETECTORS: dict[str, Callable[[Rule], Detector]] = {
    "C-01": ProcessExitDetector,
    "C-02": CrashDumpDetector,
    "C-03": BootTimeoutDetector,
    "H-01": HeartbeatDetector,
    "L-01": LogErrorDetector,
}


class Engine:
    def __init__(self, rules: dict[str, Rule]) -> None:
        self.rules = rules
        self.detectors = [DETECTORS[r.id](r) for r in rules.values() if r.id in DETECTORS]
        self.unimplemented = [r.id for r in rules.values() if r.id not in DETECTORS]

    def feed(self, ev: Event) -> list[Issue]:
        out: list[Issue] = []
        for d in self.detectors:
            out.extend(d.on_event(ev))
        return out

    def tick(self, now: float, launch: int) -> list[Issue]:
        out: list[Issue] = []
        for d in self.detectors:
            out.extend(d.on_tick(now, launch))
        return out
