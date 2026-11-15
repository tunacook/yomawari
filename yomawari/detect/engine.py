"""rules.yaml を読み込み、イベントを各ルールの検出器に流して ISSUE を出す。

フェーズ1で C-01〜03・H-01・L-01、フェーズ2で C-04・S-01〜03・S-05 を実装した。
S 系は、ファジングのループ（fuzz/loop.py）が出す op / op_result / exit_expected イベントと、ゲームのイベントから判定する。
R 系（パフォーマンス）は detect/perf.py の PerfMonitor でまとめて判定する（rules.yaml の perf に共通の設定がいる）。
"""

from __future__ import annotations

import re
import statistics
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
    """S-01: イレギュラー操作の次の通常の操作（応答確認）に、画面が反応しない（ハートビートは生存）。

    反応がなく、前に反応した別の操作でもう一度確かめるとき（recheck）は出さず、そちらの結果で判定する。

    待っているあいだにスクリプトのコマンドが進んでいれば（commands_ran）、ゲームは止まっていないので出さない
    （run 2026-11-09_2259: 最後の本文のあと、背景の切り替えと待ちだけが続く演出の途中で本文送りが効かず、S-01 として再起動した）。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.alive = float(rule.param("heartbeat_alive_sec"))

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind != "op_result" or not d.get("verify") or d.get("responded") or d.get("interfered"):
            return []
        if d.get("commands_ran") or d.get("recheck"):
            return []
        if not _alive(ev, self.alive):
            return []
        recent = list(d.get("recent") or [])
        tried = [str(x) for x in d.get("verify_tried") or []]
        back = 2 + len(tried)  # 先に押して反応がなかった応答確認の操作の、さらに前がイレギュラー操作
        cause = recent[-back] if len(recent) >= back else "（不明）"
        where = (d.get("state") or {}).get("top_modal") or (d.get("state") or {}).get("scene")
        also = f"（その前に {'、'.join(tried)} にも反応がありませんでした）" if tried else ""
        return [
            self.issue(
                f"イレギュラー操作「{cause}」のあと、{d.get('label')} に {d.get('waited')} 秒反応がありません{also}",
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
        if d.get("expected_silent") and not d.get("responded"):
            # 反応しなくて正しい押下（プロファイルの silent、選ばれている設定の値を押した）。作用しないことを確かめた
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


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-6
    return bool(a == b)


class SettingsPersistDetector(Detector):
    """F-03: 設定を変えて起動し直したら、値が戻っている（DESIGN.md §10、§9「設定を変えに行く」）。

    前の起動で最後に設定の画面を閉じたときの値（設定の画面が開いていなかったあいだは、そのときの値）と、次の起動の
    最初の settings を比べる。設定を保存するのは画面を閉じたときというゲームが多いので、開いたまま終わった起動では、
    閉じたときの値と最後の値のどちらかに一致すればよい。前の起動が落ちた（C-01）・固まって再起動した（on_detect: restart
    の問題が出た）ときは比べない（保存の前に止まったのかもしれない）。設定の画面は、押して settings の値だけが変わった
    ボタンのある画面（menu）とする。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.ok_codes = set(rule.param("harness_exit_codes"))
        self.ignore = set(rule.param("ignore_keys"))
        self.menus: set[str] = set()  # 押して設定の値だけが変わったボタンのある画面
        self.open: dict[str, dict[str, Any] | None] = {}  # 開いている画面 → 開いたときの値
        self.current: dict[str, Any] | None = None
        self.committed: dict[str, Any] | None = None  # 設定の画面が閉じていたときの値
        self.launch = 0
        self.broken = False  # この起動で、再起動する問題が出た
        self.prev: tuple[dict[str, Any], dict[str, Any], int] | None = None  # 前の起動の (閉じたときの値, 最後の値, 起動)
        self.waiting = False  # 起動したあと、まだ最初の settings を見ていない

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind == "launch":
            self.launch, self.waiting, self.broken = ev.launch, True, False
            self.current = self.committed = None
            self.open = {}
        elif ev.kind == "settings":
            values = {k: v for k, v in d.items() if k not in self.ignore and k not in ("seq", "t", "f", "ev")}
            out = self._compare(values, ev) if self.waiting else []
            self.waiting = False
            self.current = values
            if not self.open.keys() & self.menus:
                self.committed = values
            return out
        elif ev.kind == "op_result":
            target = d.get("target")
            if d.get("settings_changed") and isinstance(target, str) and not d.get("screen_changed"):
                menu = target.split("/", 1)[0]
                if menu not in self.menus and menu in self.open:
                    # 初めて設定の画面と分かった。開いてから変わった値は、まだ保存されていないかもしれない
                    self.committed = self.open[menu]
                self.menus.add(menu)
        elif ev.kind == "menu":
            self.open.setdefault(str(d.get("name")), self.current)
        elif ev.kind == "menu_close":
            name = str(d.get("name"))
            if name in self.menus and name in self.open and self.current is not None:
                self.committed = self.current
            self.open.pop(name, None)
        elif ev.kind == "issue":
            if d.get("action") == "restart" and ev.launch == self.launch:
                self.broken = True
        elif ev.kind == "proc_exit":
            code = d.get("code")
            normal = code == 0 or (bool(d.get("by_harness")) and code in self.ok_codes)
            ok = normal and not self.broken and self.committed is not None and self.current is not None
            self.prev = (self.committed, self.current, ev.launch) if ok else None  # type: ignore[assignment]
        return []

    def _compare(self, values: dict[str, Any], ev: Event) -> list[Issue]:
        if self.prev is None:
            return []
        committed, last, launch = self.prev
        self.prev = None
        out = []
        for k, want in committed.items():
            if k not in values or _same(values[k], want) or _same(values[k], last.get(k)):
                continue
            out.append(self.issue(
                f"起動 #{launch} の終わりに {k} = {want} だった設定が、起動し直したら {values[k]} になっていました",
                key=k, launch=ev.launch, t=ev.t, wall=ev.wall,
                setting=k, before=want, after=values[k], previous_launch=launch,
            ))
        return out


_TAG = re.compile(r"<[^>]*>")
# 文字の種類（V-09）。言語ごとの見込みは rules.yaml の V-09 scripts
_SCRIPTS = {
    "kana": re.compile(r"[\u3040-\u30ff]"),
    "han": re.compile(r"[\u4e00-\u9fff]"),
    "hangul": re.compile(r"[\uac00-\ud7af]"),
    "latin": re.compile(r"[A-Za-z\u00c0-\u024f]"),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
}


def _plain(text: Any) -> str:
    """本文からタグ（<ruby=...> など）と空白を除いたもの。"""
    return re.sub(r"\s", "", _TAG.sub("", str(text or "")))


def dominant_script(text: str, min_letters: int) -> str | None:
    """本文でいちばん多い文字の種類。文字が min_letters に満たなければ None。"""
    counts = {name: len(p.findall(text)) for name, p in _SCRIPTS.items()}
    if sum(counts.values()) < min_letters:
        return None
    return max(counts, key=lambda k: counts[k])


class RevealSpeedDetector(Detector):
    """V-08: 文字の速さ（settings の reveal_speed）が、本文の表示に効いていない（DESIGN.md §9「設定を変えに行く」）。

    本文の行が出て（line）表示し終わる（line_done）までの時間を、文字数で割って、そのときの reveal_speed ごとに集める。
    あいだにハーネスが操作した行（クリックで表示を飛ばした行）と、短い行は数えない。値が大きいほど速い（Naninovel の
    reveal speed）ので、2つの値の両方に min_samples 行たまったら、大きいほうの中央値が小さいほうより margin 以上
    速くなっているかを見る。小さいほうがもともと速い（min_ms 未満。スキップ中の行が多いなど）ときは判定しない。
    値の組ごとに1件。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.min_samples = int(rule.param("min_samples"))
        self.min_chars = int(rule.param("min_chars"))
        self.margin = float(rule.param("margin"))
        self.min_ms = float(rule.param("min_ms"))
        self.speed: float | None = None
        self.ops = 0
        self.pending: tuple[str, float, int, float, int] | None = None  # (line_id, t, 文字数, 速さ, 操作の数)
        self.samples: dict[float, list[float]] = {}
        self.judged: set[tuple[float, float]] = set()

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind == "launch":
            self.pending = None
        elif ev.kind == "op":
            self.ops += 1
        elif ev.source != "game":
            return []
        elif ev.kind == "settings":
            v = d.get("reveal_speed")
            self.speed = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        elif ev.kind == "line":
            n = len(_plain(d.get("text")))
            self.pending = None
            if self.speed is not None and n >= self.min_chars:
                self.pending = (str(d.get("line_id")), ev.t, n, self.speed, self.ops)
        elif ev.kind == "line_done" and self.pending is not None:
            line_id, t, n, speed, ops = self.pending
            self.pending = None
            if str(d.get("line_id")) == line_id and ops == self.ops and speed == self.speed:
                self.samples.setdefault(speed, []).append((ev.t - t) / n * 1000)
                return self._judge(speed, ev)
        return []

    def _judge(self, speed: float, ev: Event) -> list[Issue]:
        if len(self.samples[speed]) != self.min_samples:
            return []  # たまったときに1回だけ、ほかの値と比べる
        out = []
        for other, values in self.samples.items():
            if other == speed or len(values) < self.min_samples:
                continue
            slow, fast = (other, speed) if other < speed else (speed, other)
            if (slow, fast) in self.judged:
                continue
            self.judged.add((slow, fast))
            ms_slow = statistics.median(self.samples[slow][: self.min_samples])
            ms_fast = statistics.median(self.samples[fast][: self.min_samples])
            if ms_slow < self.min_ms or ms_fast <= ms_slow * (1 - self.margin):
                continue
            out.append(self.issue(
                f"文字の速さ（reveal_speed）を {slow:g} から {fast:g} に上げても、本文の表示が速くなっていません"
                f"（1文字あたりの中央値: {slow:g} で {ms_slow:.1f} ms、{fast:g} で {ms_fast:.1f} ms。各 {self.min_samples} 行）",
                key=f"{slow:g}<{fast:g}", launch=ev.launch, t=ev.t, wall=ev.wall,
                slow=slow, fast=fast, ms_slow=round(ms_slow, 2), ms_fast=round(ms_fast, 2),
            ))
        return out


class LocaleTextDetector(Detector):
    """V-09: 言語（settings の locale）を切り替えたのに、そのあとの本文がその言語の文字で出ない。

    切り替えたあとの本文のうち、文字が min_letters 以上ある最初の lines 行を見て、いちばん多い文字の種類が、
    その言語の見込み（scripts。言語タグの最初の部分ごと）に当たらない行が mismatch 行以上なら出す。見込みのない
    言語は判定しない。切り替えるたびに数え直し、言語ごとに1件。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.lines = int(rule.param("lines"))
        self.mismatch = int(rule.param("mismatch"))
        self.min_letters = int(rule.param("min_letters"))
        self.scripts = {str(k): set(v) for k, v in (rule.param("scripts") or {}).items()}
        self.locale: str | None = None
        self.seen: list[tuple[str, str]] = []  # 切り替えたあとの本文 (文字の種類, 本文)
        self.reported: set[str] = set()

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.source != "game":
            return []
        d = ev.data
        if ev.kind == "settings":
            loc = d.get("locale")
            if isinstance(loc, str) and loc != self.locale:
                self.locale, self.seen = loc, []
            return []
        if ev.kind != "line" or self.locale is None or self.locale in self.reported:
            return []
        want = self.scripts.get(self.locale.split("-", 1)[0].lower())
        if not want or len(self.seen) >= self.lines:
            return []
        text = _plain(d.get("text"))
        script = dominant_script(text, self.min_letters)
        if script is None:
            return []
        self.seen.append((script, text))
        if len(self.seen) < self.lines:
            return []
        wrong = [(s, t) for s, t in self.seen if s not in want]
        if len(wrong) < self.mismatch:
            return []
        self.reported.add(self.locale)
        return [self.issue(
            f"言語を {self.locale} に切り替えたのに、そのあとの本文 {self.lines} 行のうち {len(wrong)} 行が"
            f"その言語の文字（{'・'.join(sorted(want))}）で出ていません（例: {wrong[0][1][:40]}）",
            key=self.locale, launch=ev.launch, t=ev.t, wall=ev.wall,
            locale=self.locale, scripts=[s for s, _ in self.seen], expected=sorted(want),
        )]


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
    "F-03": SettingsPersistDetector,
    "V-08": RevealSpeedDetector,
    "V-09": LocaleTextDetector,
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
