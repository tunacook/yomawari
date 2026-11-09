"""rules.yaml を読み込み、イベントを各ルールの検出器に流して ISSUE を出す。

フェーズ1で C-01〜03・H-01・L-01、フェーズ2で C-04・S-01〜03・S-05 を実装した。
S 系は、ファジングのループ（fuzz/loop.py）が出す op / op_result / exit_expected イベントと、ゲームのイベントから判定する。
R 系（パフォーマンス）は detect/perf.py の PerfMonitor でまとめて判定する（rules.yaml の perf に共通の設定がいる）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..events import SEVERITIES, Event, Issue
from .perf import PERF_RULES, PerfMonitor


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
        if code == 0 and ev.data.get("after_op"):
            # ハーネスがボタンを押した直後に、正常に終わった。そのボタンを exit と確定する（DESIGN.md §9「役割の推定と判定」）
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


# 警告の種類をまとめるときに、文面の中で変わる部分（名前と数値）を伏せる
_WARNING_QUOTED = re.compile(r"'[^']*'")
_WARNING_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class LogWarningDetector(Detector):
    """L-06: ゲームが出す log_warning イベント。場所と文面の組ごとに検出し、レポートでは種類（evidence の kind）ごとにまとめる。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.ignore = [re.compile(p) for p in rule.param("ignore_patterns")]
        self.spots = [re.compile(p) for p in rule.param("spot_patterns")]
        self.last_line: str | None = None

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.source != "game":
            return []
        if ev.kind in ("line", "command") and ev.data.get("line_id"):
            self.last_line = str(ev.data["line_id"])
        if ev.kind != "log_warning":
            return []
        msg = str(ev.data.get("message") or "")
        if any(p.search(msg) for p in self.ignore):
            return []
        head = msg.splitlines()[0] if msg else ""
        where = self.last_line
        for p in self.spots:
            m = p.match(head)
            if m:
                where, head = f"{m['script']}:{m['line']}", m["message"]
                break
        kind = _WARNING_NUMBER.sub("N", _WARNING_QUOTED.sub("'*'", head))
        return [
            self.issue(
                f"Warning: {head[:200]}",
                key=f"{where}|{head[:80]}", launch=ev.launch, t=ev.t, wall=ev.wall,
                kind=kind[:200], where=where, message=head,
            )
        ]


class ExitTimeoutDetector(Detector):
    """C-04: 終了を期待する操作（[guard.buttons] expect_exit）から、一定時間たってもプロセスが残る。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.timeout = float(rule.param("timeout_sec"))
        self.since: float | None = None
        self.wall = ""
        self.target = ""

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind == "exit_expected":
            self.since, self.wall, self.target = ev.t, ev.wall, str(ev.data.get("target"))
        elif ev.kind in ("proc_exit", "launch"):
            self.since = None
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if self.since is None or now - self.since < self.timeout:
            return []
        self.since = None
        return [
            self.issue(
                f"{self.target} を押してから {self.timeout:g} 秒たってもプロセスが終了しません",
                key=self.target, launch=launch, t=now, wall=self.wall, target=self.target,
            )
        ]


def _alive(ev: Event, limit: float) -> bool:
    age = ev.data.get("heartbeat_age")
    return age is not None and float(age) <= limit


class ResponseCheckDetector(Detector):
    """S-01: イレギュラー操作の次の通常の操作（応答確認）に、画面が反応しない（ハートビートは生存）。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.alive = float(rule.param("heartbeat_alive_sec"))

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind != "op_result" or not d.get("verify") or d.get("responded") or d.get("interfered"):
            return []
        if not _alive(ev, self.alive):
            return []
        recent = list(d.get("recent") or [])
        cause = recent[-2] if len(recent) >= 2 else "（不明）"
        where = (d.get("state") or {}).get("top_modal") or (d.get("state") or {}).get("scene")
        return [
            self.issue(
                f"イレギュラー操作「{cause}」のあと、{d.get('label')} に {d.get('waited')} 秒反応がありません",
                key=f"{cause}|{d.get('label')}|{where}", launch=ev.launch, t=ev.t, wall=ev.wall,
                recent=recent, state=d.get("state"),
            )
        ]


class AdvanceStallDetector(Detector):
    """S-02: 本文を送るクリックを続けても、line / choices / scene が来ない。"""

    PROGRESS = {"line", "choices", "scene"}

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.clicks = int(rule.param("clicks"))
        self.alive = float(rule.param("heartbeat_alive_sec"))
        self.count = 0
        self.lead: list[str] = []  # 進まなくなる前の操作（本文送りが続くと「直前の操作」から押し出されるため）

    def on_event(self, ev: Event) -> list[Issue]:
        # 選択肢が消えた知らせ（items が空の choices）は進んだことにしない
        progress = ev.kind in self.PROGRESS and not (ev.kind == "choices" and not ev.data.get("items"))
        if ev.kind == "launch" or (ev.source == "game" and progress):
            self.count = 0
            return []
        d = ev.data
        if ev.kind != "op_result" or d.get("kind") != "advance" or d.get("interfered") or not _alive(ev, self.alive):
            return []
        self.count += 1
        if self.count == 1:
            self.lead = [str(x) for x in (d.get("recent") or [])[:-1]][-5:]
        if self.count < self.clicks:
            return []
        self.count = 0
        st = d.get("state") or {}
        lead = f"。本文送りを続ける前の操作（古い順）: {' → '.join(f'`{x}`' for x in self.lead)}" if self.lead else ""
        return [
            self.issue(
                f"本文を {self.clicks} 回送っても進みません（行 {st.get('line_id')}）{lead}",
                key=str(st.get("line_id")), launch=ev.launch, t=ev.t, wall=ev.wall,
                recent=d.get("recent"), state=st,
            )
        ]


class TransitionStuckDetector(Detector):
    """S-03: transition の開始から一定時間たっても終了が来ない。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.timeout = float(rule.param("timeout_sec"))
        self.open: dict[str, tuple[float, str]] = {}

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind == "launch":
            self.open.clear()
        elif ev.source == "game" and ev.kind == "transition":
            name = str(ev.data.get("name"))
            if ev.data.get("phase") == "start":
                self.open[name] = (ev.t, ev.wall)
            else:
                self.open.pop(name, None)
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        out = []
        for name, (t, wall) in list(self.open.items()):
            if now - t >= self.timeout:
                del self.open[name]
                out.append(
                    self.issue(
                        f"暗転・遷移 {name} が {self.timeout:g} 秒たっても終わりません",
                        key=name, launch=launch, t=now, wall=wall,
                    )
                )
        return out


class ButtonNoResponseDetector(Detector):
    """S-05: 前に反応したボタンを押しても、画面の反応がない回が続く。

    一度も反応したことのないボタン（ロック中のスロットなど、押しても何も起きないのが仕様のもの）は数えず、
    silent に押した回数を残す（レポートの「一度も反応しなかったボタン」）。
    """

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.consecutive = int(rule.param("consecutive"))
        self.alive = float(rule.param("heartbeat_alive_sec"))
        self.ignore = [re.compile(p) for p in rule.param("ignore_patterns")]
        self.ignore_types = set(rule.param("ignore_types"))
        self.misses: dict[str, int] = {}
        self.responded: set[str] = set()
        self.silent: dict[str, int] = {}

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind != "op_result" or d.get("kind") != "click" or d.get("irregular") or d.get("interfered"):
            return []
        target = str(d.get("target"))
        if d.get("type") in self.ignore_types or any(p.search(target) for p in self.ignore):
            return []
        if d.get("quiet") and not d.get("responded"):
            # 押した時点で暗い・空に見えていた（反応しないのが仕様と見込んだ）ボタン。セーブ画面では反応した
            # 空のスロットが、ロード画面で反応しないのは正常なので、数えない
            return []
        if d.get("responded"):
            self.misses.pop(target, None)
            self.silent.pop(target, None)
            self.responded.add(target)
            return []
        if not _alive(ev, self.alive):
            return []
        if target not in self.responded:
            self.silent[target] = self.silent.get(target, 0) + 1
            return []
        self.misses[target] = self.misses.get(target, 0) + 1
        if self.misses[target] < self.consecutive:
            return []
        del self.misses[target]
        return [
            self.issue(
                f"{target} を {self.consecutive} 回押しても、画面が反応しません",
                key=target, launch=ev.launch, t=ev.t, wall=ev.wall,
                recent=d.get("recent"), state=d.get("state"),
            )
        ]


class DimButtonRespondedDetector(Detector):
    """V-07: ほかの同じ種類のボタンより暗い（今は押せないように見える）ボタンを押したら、別の画面が出た。

    暗いのに別の画面に進めるのは、見た目と動作が食い違っている（アンロックされていないのに回想が開ける、など）。
    押せないことを知らせる演出（震える、ボタンのない通知が出る）で画面の状態が変わっただけでは出さない。
    押してセーブが始まったときも出さない（空きのセーブスロットはサムネイルがなく暗く写るが、セーブするのが正しい操作）。
    明るさの差だけ（絵柄・サムネイルが暗いだけのことがある）では出さない。押せないときの一般的な見せ方（半透明・灰色）か
    伏せ字で押せないと見たものだけを対象にする。ファジングのループが押す前に画面で決め、op_result の disabled_look で、
    別の画面が出たかは screen_changed で伝える（fuzz/looks.py、GameState.screens）。
    """

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.alive = float(rule.param("heartbeat_alive_sec"))
        self.seen: set[str] = set()

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind != "op_result" or d.get("kind") != "click" or not d.get("disabled_look"):
            return []
        if not d.get("screen_changed") or d.get("interfered") or not _alive(ev, self.alive):
            return []
        if d.get("saved"):
            # 空きのセーブスロットは、サムネイルがなくてほかより暗く写るが、押してセーブするのが正しい操作
            return []
        target = str(d.get("target"))
        if target in self.seen:
            return []
        self.seen.add(target)
        return [
            self.issue(
                f"{target} はほかの同じ種類のボタンより暗い（押せないように見える）のに、押すと別の画面が出ました"
                f"（{', '.join((d.get('state') or {}).get('menus') or []) or 'menu なし'}）",
                key=target, launch=ev.launch, t=ev.t, wall=ev.wall,
                recent=d.get("recent"), state=d.get("state"),
            )
        ]


DETECTORS: dict[str, Callable[[Rule], Detector]] = {
    "C-01": ProcessExitDetector,
    "C-02": CrashDumpDetector,
    "C-03": BootTimeoutDetector,
    "C-04": ExitTimeoutDetector,
    "H-01": HeartbeatDetector,
    "S-01": ResponseCheckDetector,
    "S-02": AdvanceStallDetector,
    "S-03": TransitionStuckDetector,
    "S-05": ButtonNoResponseDetector,
    "V-07": DimButtonRespondedDetector,
    "L-01": LogErrorDetector,
    "L-06": LogWarningDetector,
}


class Engine:
    def __init__(self, rules: dict[str, Rule], perf: dict[str, Any] | None = None) -> None:
        """perf: rules.yaml の perf（パフォーマンスの判定で共通の設定）。なければ R 系は評価しない。"""
        self.rules = rules
        self.detectors: list[Any] = [DETECTORS[r.id](r) for r in rules.values() if r.id in DETECTORS]
        perf_rules = {r.id: r for r in rules.values() if r.id in PERF_RULES}
        self.perf = PerfMonitor(perf_rules, perf) if perf_rules and perf is not None else None
        if self.perf is not None:
            self.detectors.append(self.perf)
        self.unimplemented = [r.id for r in rules.values() if r.id not in DETECTORS
                              and not (self.perf is not None and r.id in perf_rules)]

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

    def finish(self, now: float, wall: str = "") -> list[Issue]:
        """ランの終わりに判定するもの（R-09・R-10）。"""
        return self.perf.finish(now, wall) if self.perf is not None else []

    def silent_buttons(self) -> dict[str, int]:
        """S-05 が見た、一度も反応しなかったボタンと押した回数。"""
        out: dict[str, int] = {}
        for d in self.detectors:
            if isinstance(d, ButtonNoResponseDetector):
                out.update(d.silent)
        return out
