"""rules.yaml を読み込み、イベントを各ルールの検出器に流して ISSUE を出す。

フェーズ1で C-01〜03・H-01・L-01、フェーズ2で C-04・S-01〜03・S-05 を実装した。
S 系は、ファジングのループ（fuzz/loop.py）が出す op / op_result / exit_expected イベントと、ゲームのイベントから判定する。
R 系（パフォーマンス）は detect/perf.py の PerfMonitor でまとめて判定する（rules.yaml の perf に共通の設定がいる）。
"""

from __future__ import annotations

import math
import re
from collections import deque
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


class SaveFileDetector(Detector):
    """F-01: ゲームがセーブを終えた（save の phase: end）のに、セーブの場所のファイルが更新されていない。
    ファイルはハーネス（sensors/savefile.py）が見て、save_file イベントで知らせる。"""

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind != "save_file" or ev.data.get("updated"):
            return []
        d = ev.data
        newest = d.get("newest")
        hint = (f"いちばん新しいファイルは {newest}（セーブを始める {d.get('newest_before_sec')} 秒前に更新）"
                if newest else "ファイルが1つもない")
        return [
            self.issue(
                f"セーブ {d.get('slot')} を終えたのに、セーブの場所（{d.get('save_dir')}）のファイルが更新されていません。{hint}",
                key=str(d.get("slot")), launch=ev.launch, t=ev.t, wall=ev.wall,
                slot=d.get("slot"), save_dir=d.get("save_dir"), newest=newest,
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


def warning_kind(head: str) -> str:
    """警告の種類（文面の中で変わる名前と数値を伏せたもの）。"""
    return _WARNING_NUMBER.sub("N", _WARNING_QUOTED.sub("'*'", head))


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
        kind = warning_kind(head)
        return [
            self.issue(
                f"Warning: {head[:200]}",
                key=f"{where}|{head[:80]}", launch=ev.launch, t=ev.t, wall=ev.wall,
                kind=kind[:200], where=where, message=head,
            )
        ]


def _log_head(ev: Event) -> str:
    msg = str(ev.data.get("message") or "")
    return msg.splitlines()[0] if msg else ""


class _LogPatternDetector(Detector):
    """ゲームが出すログ（log_error / log_warning）のうち、patterns のどれかに当たるもの。"""

    kinds = ("log_error", "log_warning")

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.patterns = [re.compile(p) for p in rule.param("patterns")]

    def match(self, ev: Event) -> re.Match[str] | None:
        if ev.source != "game" or ev.kind not in self.kinds:
            return None
        head = _log_head(ev)
        return next((m for m in (p.search(head) for p in self.patterns) if m), None)

    def claims(self, ev: Event) -> bool:
        """このログをこのルールで判定する。True なら L-01・L-06 では数えない。"""
        return self.match(ev) is not None


class MissingGlyphDetector(_LogPatternDetector):
    """L-02: TextMeshPro の、フォントにない文字の警告。フォントと文字の組ごとに1件。
    文字ごとの警告は L-06 では数えない（同じ文面の名前と数値だけが違う警告が、文字の数だけ並ぶため）。"""

    kinds = ("log_warning",)

    def on_event(self, ev: Event) -> list[Issue]:
        m = self.match(ev)
        if m is None:
            return []
        g = m.groupdict()
        code, font, obj = g.get("code") or "", g.get("font") or "?", g.get("object")
        try:
            char = chr(int(code, 16))
        except ValueError:
            char = ""
        shown = f"「{char}」（U+{code.upper()}）" if char else f"（{code}）"
        where = f"（テキスト {obj}）" if obj else ""
        return [
            self.issue(
                f"フォント {font} にない文字{shown}を表示しようとしました{where}",
                key=f"{font}|{code.upper()}", launch=ev.launch, t=ev.t, wall=ev.wall,
                font=font, char=char, code=code.upper(), object=obj, message=_log_head(ev),
            )
        ]


class ResourceLoadDetector(_LogPatternDetector):
    """L-03: リソースの読み込みに失敗した（ログの文面のパターン）。読み込めなかったものごとに1件。
    同じログを L-01・L-06 では数えない。"""

    def on_event(self, ev: Event) -> list[Issue]:
        m = self.match(ev)
        if m is None:
            return []
        g = m.groupdict()
        name = g.get("name")
        head = _log_head(ev)
        what = f"{name}" + (f"（{g['type']}）" if g.get("type") else "") if name else head[:200]
        level = "エラー" if ev.kind == "log_error" else "警告"
        return [
            self.issue(
                f"リソースを読み込めませんでした: {what}（{level}）",
                key=name or head[:120], launch=ev.launch, t=ev.t, wall=ev.wall,
                name=name, type=g.get("type"), message=head,
                log="\n".join(x for x in (str(ev.data.get("message") or ""), str(ev.data.get("stack") or "")) if x),
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


class SeqGapDetector(Detector):
    """L-05: [YOMAWARI] 行の seq が飛んだ（行の取りこぼし。ログの書き出しの失敗や、行の形式の崩れ）。seq は起動ごとに 0 から。
    飛ぶたびに出し、レポートでは起動ごとに1件にまとめる（key が起動ごと）。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.last: dict[int, int] = {}

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.source != "game":
            return []
        seq = ev.data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return []
        prev = self.last.get(ev.launch)
        self.last[ev.launch] = seq
        if prev is None or seq <= prev + 1:
            return []
        missing = seq - prev - 1
        return [
            self.issue(
                f"[YOMAWARI] 行の seq が {prev} から {seq} に飛びました（{missing} 行を受け取っていません）",
                key=f"launch{ev.launch}", launch=ev.launch, t=ev.t, wall=ev.wall,
                after=prev, got=seq, missing=missing,
            )
        ]


class WarningFloodDetector(Detector):
    """L-04: 同じ種類の警告（L-06 と同じく、名前と数値を伏せた文面）が、window_sec のうちに count 回以上出た。
    毎フレーム出る警告は、ログを埋めて重くする。種類ごとに起動につき1件。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.window = float(rule.param("window_sec"))
        self.count = int(rule.param("count"))
        self.times: dict[tuple[int, str], deque[float]] = {}
        self.reported: set[tuple[int, str]] = set()

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.source != "game" or ev.kind != "log_warning":
            return []
        msg = str(ev.data.get("message") or "")
        head = msg.splitlines()[0] if msg else ""
        kind = warning_kind(head)
        key = (ev.launch, kind)
        if key in self.reported:
            return []
        q = self.times.setdefault(key, deque())
        q.append(ev.t)
        while q and ev.t - q[0] > self.window:
            q.popleft()
        if len(q) < self.count:
            return []
        self.reported.add(key)
        del self.times[key]
        return [
            self.issue(
                f"同じ警告が {self.window:g} 秒のうちに {self.count} 回以上出ました: {head[:200]}",
                key=kind[:120], launch=ev.launch, t=ev.t, wall=ev.wall, kind=kind[:200], message=head,
            )
        ]


def _offscreen(rect: Any, margin: float) -> bool:
    """矩形（正規化座標 [x0, y0, x1, y1]）の中心が、描画領域の外にある。"""
    try:
        x0, y0, x1, y1 = (float(v) for v in rect)
    except (TypeError, ValueError):
        return False
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return not (-margin <= cx <= 1 + margin and -margin <= cy <= 1 + margin)


class OffscreenDetector(Detector):
    """V-05: ボタン・選択肢の中心が、描画領域の外に min_sec 以上とどまっている。出入りのアニメーションの途中
    （run 2026-11-14_2214: メニューが滑り込むあいだ 0.4 秒ほど外にある）は数えない。スクロールする画面（scroll のある
    menu）と、ゲームがスクロール領域のマスクで隠れていると伝えたボタン（hidden）は、外にあるのが正常なので除く。
    scroll を出さない古いパッケージの menu は判定しない（run 2026-11-03_1658: スクロールする一覧の画面の外のスロットを出した）。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.min_sec = float(rule.param("min_sec"))
        self.margin = float(rule.param("margin"))
        self.since: dict[str, tuple[float, str, Any]] = {}  # label → (外に出た時刻, wall, 矩形)
        self.reported: set[str] = set()
        self.launch = 0

    def _update(self, prefix: str, items: dict[str, Any], ev: Event) -> None:
        for label in [k for k in self.since if k.startswith(prefix) and k not in items]:
            del self.since[label]
        for label, rect in items.items():
            if label not in self.since:
                self.since[label] = (ev.t, ev.wall, rect)

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind == "launch":
            self.since.clear()
            self.launch = ev.launch
        if ev.source != "game":
            return []
        d = ev.data
        if ev.kind == "menu":
            name = str(d.get("name"))
            items: dict[str, Any] = {}
            # scroll を出さない古いパッケージでは、スクロールする画面かが分からないので判定しない
            if "scroll" in d and not d.get("scroll"):
                seen: dict[str, int] = {}
                for b in d.get("buttons") or []:
                    if not isinstance(b, dict):
                        continue
                    bname = str(b.get("name"))
                    nth = seen.get(bname, 0)
                    seen[bname] = nth + 1
                    if not b.get("hidden") and _offscreen(b.get("rect"), self.margin):
                        items[f"{name}/{bname}#{nth}" if nth else f"{name}/{bname}"] = b.get("rect")
            self._update(name + "/", items, ev)
        elif ev.kind == "menu_close":
            self._update(str(d.get("name")) + "/", {}, ev)
        elif ev.kind == "choices":
            items = {f"choice:{c.get('id')}": c.get("rect") for c in d.get("items") or []
                     if isinstance(c, dict) and _offscreen(c.get("rect"), self.margin)}
            self._update("choice:", items, ev)
        elif ev.kind == "choices_done":
            self._update("choice:", {}, ev)
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        out = []
        for label, (since, wall, rect) in list(self.since.items()):
            if now - since < self.min_sec or label in self.reported:
                continue
            self.reported.add(label)
            what = "選択肢" if label.startswith("choice:") else "ボタン"
            out.append(self.issue(
                f"{what} {label} が描画領域の外に {self.min_sec:g} 秒以上あります（矩形 {rect}）",
                key=label, launch=launch, t=now, wall=wall, target=label, rect=rect,
            ))
        return out


def _alive(ev: Event, limit: float) -> bool:
    age = ev.data.get("heartbeat_age")
    return age is not None and float(age) <= limit


class ResolutionMismatchDetector(Detector):
    """V-04: ゲームが出した resolution（描画領域のピクセル数）と、OS から測ったウィンドウ（proc_stats の sys.window と
    ディスプレイの倍率 sys.backing）が合わない状態が min_sec 以上続いた。

    幅はウィンドウの外枠の幅 × 倍率と比べ、高さは外枠からタイトルバーの分（0〜title_max_pt ポイント）を引いた残りと比べる
    （DESIGN.md §8 と同じ見方。合わないとハーネスはボタンの位置を計算できない）。フルスクリーンではゲームが描画を
    引き伸ばすので、modes（Windowed）のときだけ見る。resolution が変わった直後のウィンドウの大きさの変わり途中は、
    min_sec で除く。resolution ごとに1件。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.min_sec = float(rule.param("min_sec"))
        self.tolerance = float(rule.param("tolerance_px"))
        self.title_max = float(rule.param("title_max_pt"))
        self.modes = set(rule.param("modes"))
        self.res: tuple[float, float, str] | None = None
        self.since: tuple[float, str] | None = None
        self.reported: set[tuple[int, float, float]] = set()

    def _mismatch(self, win: list[float], scale: float) -> str | None:
        assert self.res is not None
        rw, rh, _ = self.res
        width_px = win[2] * scale
        content_pt = rh / scale
        title = win[3] - content_pt
        if abs(width_px - rw) <= self.tolerance and -1.0 <= title <= self.title_max:
            return None
        return (f"resolution は {rw:g}x{rh:g} なのに、ウィンドウの外枠は {width_px:.0f}x{win[3] * scale:.0f} ピクセル"
                f"（{win[2]:g}x{win[3]:g} ポイント、倍率 {scale:g}）です")

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind == "launch":
            self.res, self.since = None, None
        elif ev.source == "game" and ev.kind == "resolution":
            try:
                self.res = (float(d["w"]), float(d["h"]), str(d.get("mode")))
            except (KeyError, TypeError, ValueError):
                self.res = None
            self.since = None
        elif ev.kind == "proc_stats":
            sysd = d.get("sys") or {}
            win, scale = sysd.get("window"), sysd.get("backing")
            if (self.res is None or self.res[2] not in self.modes or not isinstance(win, list) or len(win) != 4
                    or not isinstance(scale, (int, float)) or scale <= 0):
                self.since = None
                return []
            why = self._mismatch([float(x) for x in win], float(scale))
            if why is None:
                self.since = None
                return []
            if self.since is None:
                self.since = (ev.t, ev.wall)
                return []
            key = (ev.launch, self.res[0], self.res[1])
            if ev.t - self.since[0] < self.min_sec or key in self.reported:
                return []
            self.reported.add(key)
            return [self.issue(
                f"{why}。{self.min_sec:g} 秒以上合わないままです",
                key=f"{self.res[0]:g}x{self.res[1]:g}", launch=ev.launch, t=ev.t, wall=self.since[1],
                resolution=[self.res[0], self.res[1]], window=win, backing=scale,
            )]
        return []


class WindowLostDetector(Detector):
    """V-03: ハーネスが前面に戻そうとしても、ゲームのウィンドウが画面に出ない（最小化など）か、前面に戻らない状態が
    min_sec 以上続いた。proc_stats の sys（on_screen・front）で続いた時間を測り、そのあいだにハーネスが操作しようとして
    断られた（input_blocked「ゲームを前面にできません」、op_error「画面に出ていません」）ときだけ出す（操作しないモードや、
    すぐ戻ったものは除く）。画面のロック中・画面が消えているあいだは数えない。ほかのアプリが前に出続けた場合も当たる。"""

    BLOCKED = ("ゲームを前面にできません", "画面に出ていません")

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.min_sec = float(rule.param("min_sec"))
        self.since: tuple[float, str] | None = None
        self.state = ""
        self.tried: tuple[float, str] | None = None  # 最後に断られた時刻と理由
        self.reported = False

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind in ("launch", "proc_exit"):
            self.since, self.tried, self.reported = None, None, False
        elif ev.kind in ("input_blocked", "op_error"):
            err = str(d.get("error") or "")
            if any(w in err for w in self.BLOCKED):
                self.tried = (ev.t, err)
        elif ev.kind == "proc_stats":
            sysd = d.get("sys") or {}
            hidden = sysd.get("on_screen") is False
            back = sysd.get("front") is False
            if (not hidden and not back) or sysd.get("locked") or sysd.get("asleep"):
                self.since, self.reported = None, False
                return []
            self.state = "画面に出ていない（最小化など）" if hidden else "前面に戻らない"
            if self.since is None:
                self.since = (ev.t, ev.wall)
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if self.since is None or self.reported or now - self.since[0] < self.min_sec:
            return []
        # 断られたのが、この状態が始まる少し前（proc_stats の間隔）からあと
        if self.tried is None or self.tried[0] < self.since[0] - 5:
            return []
        self.reported = True
        return [self.issue(
            f"ゲームのウィンドウが {self.min_sec:g} 秒以上{self.state}ままです。ハーネスが前面に戻そうとしましたが戻りません"
            f"（{self.tried[1]}）。ほかのアプリが前に出続けた場合も当たるので、レポートの前後の記録で確かめてください",
            key=self.state, launch=launch, t=now, wall=self.since[1], window=self.state, error=self.tried[1],
        )]


class BgmOverlapDetector(Detector):
    """A-01: BGM が 2 つ以上、min_sec 以上重なって鳴り続ける（前の BGM を止めずに次の BGM を鳴らした）。
    パッケージが実際に鳴っている BGM を見て出す bgm_play / bgm_stop で数える。止めて次へ移るフェードの重なりは、
    止めた時点（bgm_stop）で外れるので数えない。重なった組ごとに起動につき 1 件。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.min_sec = float(rule.param("min_sec"))
        self.playing: dict[str, float] = {}  # id → 鳴り始めた時刻
        self.since: tuple[float, str] | None = None  # 2 つ以上になった時刻
        self.reported: set[tuple[int, tuple[str, ...]]] = set()

    def on_event(self, ev: Event) -> list[Issue]:
        if ev.kind in ("launch", "proc_exit"):
            self.playing.clear()
            self.since = None
        elif ev.kind == "bgm_play" and ev.data.get("id"):
            self.playing[str(ev.data["id"])] = ev.t
            if len(self.playing) >= 2 and self.since is None:
                self.since = (ev.t, ev.wall)
        elif ev.kind == "bgm_stop":
            self.playing.pop(str(ev.data.get("id")), None)
            if len(self.playing) < 2:
                self.since = None
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if self.since is None or now - self.since[0] < self.min_sec:
            return []
        ids = tuple(sorted(self.playing))
        if (launch, ids) in self.reported:
            return []
        self.reported.add((launch, ids))
        names = "、".join(f"`{i}`" for i in ids)
        return [self.issue(
            f"BGM が {len(ids)} つ重なったまま {self.min_sec:g} 秒以上鳴っています: {names}。前の BGM を止めずに次の BGM を"
            "鳴らしていないか、スクリプトを確かめてください（意図して重ねているなら、rules.yaml で外す）",
            key="|".join(ids), launch=launch, t=now, wall=self.since[1], bgm=list(ids),
        )]


class TitleVoiceDetector(Detector):
    """A-02: タイトルに戻ったあとも、戻る前に鳴り始めたボイスが min_sec 以上鳴り続ける。
    タイトルは、起動して最初にボタンを持って出た menu（GameState と同じ決め方）。それが閉じたあと、また出たらタイトルに
    戻ったとする。BGM の重なりは A-01 で見る。起動につき 1 件。"""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.min_sec = float(rule.param("min_sec"))
        self._reset()

    def _reset(self) -> None:
        self.title: str | None = None
        self.title_open = False
        self.voice: tuple[str, float] | None = None  # 鳴っているボイスと、鳴り始めた時刻
        self.returned: tuple[float, str] | None = None  # タイトルに戻った時刻
        self.reported = False

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind in ("launch", "proc_exit"):
            self._reset()
        elif ev.kind == "menu" and d.get("name"):
            name = str(d["name"])
            if self.title is None and d.get("buttons"):
                self.title, self.title_open = name, True
            elif name == self.title and not self.title_open:
                self.title_open = True
                self.returned = (ev.t, ev.wall)
        elif ev.kind == "menu_close" and d.get("name") == self.title:
            self.title_open = False
            self.returned = None
        elif ev.kind == "voice" and d.get("id"):
            self.voice = (str(d["id"]), ev.t)
        elif ev.kind == "voice_stop":
            self.voice = None
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if self.reported or self.returned is None or self.voice is None:
            return []
        at, wall = self.returned
        if self.voice[1] >= at or now - at < self.min_sec:
            return []
        self.reported = True
        return [self.issue(
            f"タイトルに戻ってから {self.min_sec:g} 秒たっても、戻る前に鳴り始めたボイス `{self.voice[0]}` が鳴り続けています",
            key=self.title or "", launch=launch, t=now, wall=wall, voice=self.voice[0], title=self.title,
        )]


class VolumeEffectDetector(Detector):
    """A-03: 音量の設定（master_volume・bgm_volume）を変えたのに、出力の大きさ（heartbeat の audio_db）が見込みほど
    変わらない。見込みは 20·log10(後の値 / 前の値) dB（0 は min_volume として数える）。見込みが min_change_db 以上のときだけ、
    変える前の window_sec と、変えて settle_sec たってからの window_sec の中央値を比べ、変わった量が見込みの effect_ratio
    未満なら出す。比べるあいだに BGM が変わった・ボイスが鳴った・ほかの設定を変えた・音が小さすぎる（silent_db 未満）・
    heartbeat が min_samples に足りないときは判定しない。効果音・ボイスの音量は、ほかの音と分けられないので見ない。"""

    KEYS = ("master_volume", "bgm_volume")

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        p = rule.param
        self.min_change = float(p("min_change_db"))
        self.window = float(p("window_sec"))
        self.settle = float(p("settle_sec"))
        self.ratio = float(p("effect_ratio"))
        self.silent = float(p("silent_db"))
        self.min_samples = int(p("min_samples"))
        self.min_volume = float(p("min_volume"))
        self.levels: list[tuple[float, float]] = []  # (時刻, audio_db)
        self.marks: list[float] = []  # BGM・ボイスが変わった時刻（比べるあいだにあれば判定しない）
        self.voice = False
        self.settings: dict[str, float] = {}
        self.changes: list[float] = []  # 設定を変えた時刻
        self.pending: list[tuple[float, str, str, float, float]] = []  # (時刻, wall, key, 前, 後)
        self.reported: set[tuple[int, str]] = set()

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind in ("launch", "proc_exit"):
            self.levels.clear()
            self.marks.clear()
            self.pending.clear()
            self.settings.clear()
            self.voice = False
        elif ev.kind == "heartbeat" and isinstance(d.get("audio_db"), (int, float)):
            self.levels.append((ev.t, float(d["audio_db"])))
            if self.voice:
                self.marks.append(ev.t)
            cut = ev.t - 3 * (self.window + self.settle)
            self.levels = [x for x in self.levels if x[0] >= cut]
        elif ev.kind in ("bgm_play", "bgm_stop", "voice"):
            self.marks.append(ev.t)
            self.voice = self.voice or ev.kind == "voice"
        elif ev.kind == "voice_stop":
            self.marks.append(ev.t)
            self.voice = False
        elif ev.kind == "settings":
            changed = False
            for key in self.KEYS:
                v = d.get(key)
                if not isinstance(v, (int, float)):
                    continue
                old = self.settings.get(key)
                self.settings[key] = float(v)
                if old is not None and old != v:
                    changed = True
                    self.pending.append((ev.t, ev.wall, key, old, float(v)))
            if changed or self.changes:
                self.changes.append(ev.t)  # settings は値が変わったときだけ出る（最初の 1 回を除く）
        return []

    def _expected(self, old: float, new: float) -> float:
        return 20 * math.log10(max(new, self.min_volume) / max(old, self.min_volume))

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        out = []
        for item in list(self.pending):
            t, wall, key, old, new = item
            if now < t + self.settle + self.window:
                continue
            self.pending.remove(item)
            expected = self._expected(old, new)
            lo, hi = t - self.window, t + self.settle + self.window
            if abs(expected) < self.min_change or (launch, key) in self.reported:
                continue
            if any(lo <= m <= hi for m in self.marks) or any(lo <= c <= hi and c != t for c in self.changes):
                continue
            before = [db for at, db in self.levels if lo <= at < t]
            after = [db for at, db in self.levels if t + self.settle <= at <= hi]
            if len(before) < self.min_samples or len(after) < self.min_samples:
                continue
            b, a = _median(before), _median(after)
            if max(a, b) < self.silent:
                continue
            observed = a - b
            if observed / expected >= self.ratio:
                continue
            self.reported.add((launch, key))
            out.append(self.issue(
                f"{key} を {old:g} から {new:g} に変えましたが、出力の大きさが {observed:+.1f} dB しか変わりません"
                f"（見込みは {expected:+.1f} dB。前 {b:.1f} dB、後 {a:.1f} dB）。設定の値が音に効いていない見込みです",
                key=key, launch=launch, t=now, wall=wall, setting=key, old=old, new=new,
                expected_db=round(expected, 1), observed_db=round(observed, 1),
            ))
        return out


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
    """S-02: 本文を送るクリックを続けても、line / choices / scene が来ない。

    ゲームが本文送りの入力を止めているあいだ（パッケージの input の muted に Continue がある。`@processInput` や、
    UI を隠すあいだの入力の止め方など）は、押しても進まないのが仕様なので、本文送りを数えない
    （run 2026-11-16_2241: 入力を止めたオープニングの演出で、コマンドが進んでいるのに検出した）。
    人もこのあいだにクリックすることはあるので、ハーネスは押すのをやめない。
    止めたまま戻し忘れる不具合は、入力が止められていて、スクリプトのコマンド・本文も muted_idle_sec 来ないときに検出する。
    ただし、モーダルな画面が開いているあいだに本文送りを押して、進まないことを確かめたあとは、モーダルがすべて閉じるまで数えない
    （メニューを開いているあいだに本文送りを止めるのは仕様。run 2026-11-17_1204: ロード・設定の画面にいるだけで検出した）。
    モーダルからモーダルへ移るあいだも数えない。すべて閉じたら、そこから数え直す。ハーネスは、モーダルが開いて入力が止められたまま mute_check_sec たつと本文送りを押す
    （fuzz/loop.py の _mute_check）
    操作の結果でモーダルがなくなっても、modal_gap_sec のうちにゲームからモーダルな画面が開いた知らせが来たら、すべて閉じたとは
    みなさない（run 2026-11-18_1622: 一覧から詳細の画面へ移るとき、一覧がモーダルでなくなってから詳細が開くまでの約 0.1 秒に
    操作の結果をとり、数え直した。ハーネスはその一瞬を見ておらず確かめ直さなかったので、120 秒後に検出して再起動した）
    """

    PROGRESS = {"line", "choices", "scene"}
    SCRIPT = PROGRESS | {"command"}

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.clicks = int(rule.param("clicks"))
        self.alive = float(rule.param("heartbeat_alive_sec"))
        self.idle = float(rule.param("muted_idle_sec"))
        self.gap = float(rule.param("modal_gap_sec"))
        self.count = 0
        self.lead: list[str] = []  # 進まなくなる前の操作（本文送りが続くと「直前の操作」から押し出されるため）
        self.muted = False  # 本文送り（Continue）の入力が止められている
        self.script_at: float | None = None  # 入力が止められてから、最後にスクリプトが進んだ時刻
        self.script_wall = ""
        self.heartbeat_at: float | None = None
        self.state: dict | None = None  # 最後の操作のときのゲームの状態
        self.fired = False
        self.progress_at: float | None = None  # 最後に line / choices / scene が来た時刻
        # 入力が止められたモーダルの上で、本文送りを押しても進まないと確かめたモーダルの名前（モーダルがすべて閉じるまで数えない）
        self.checked: str | None = None
        # 確かめたあと、操作の結果でモーダルがなくなった時刻（modal_gap_sec のあいだモーダルが開かなければ、すべて閉じたとする）
        self.closed: tuple[float, str] | None = None

    def _settle_closed(self, now: float) -> None:
        """モーダルがなくなってから modal_gap_sec のあいだ、モーダルな画面が開かなかった: すべて閉じた。閉じたときから数え直す。"""
        if self.closed is None or now - self.closed[0] < self.gap:
            return
        at, wall = self.closed
        self.checked, self.closed = None, None
        if self.muted:
            self.script_at, self.script_wall, self.fired = at, wall, False

    def on_event(self, ev: Event) -> list[Issue]:
        d = ev.data
        if ev.kind == "launch" or ev.kind == "proc_exit":
            self.count, self.muted, self.script_at, self.heartbeat_at, self.fired = 0, False, None, None, False
            self.progress_at, self.checked, self.closed = None, None, None
            return []
        if ev.source == "game":
            if ev.kind == "heartbeat":
                self.heartbeat_at = ev.t
            elif ev.kind == "menu" and d.get("modal"):
                self.closed = None  # モーダルな画面が開いた（画面から画面へ移るあいだの一瞬だった）
            elif ev.kind == "input":
                muted = "Continue" in (d.get("muted") or [])
                if muted != self.muted:
                    self.muted, self.count, self.fired, self.checked, self.closed = muted, 0, False, None, None
                    self.script_at, self.script_wall = (ev.t, ev.wall) if muted else (None, "")
            elif ev.kind in self.SCRIPT:
                if self.muted:
                    self.script_at, self.script_wall, self.fired = ev.t, ev.wall, False
                # 選択肢が消えた知らせ（items が空の choices）は進んだことにしない
                if ev.kind in self.PROGRESS and not (ev.kind == "choices" and not d.get("items")):
                    self.count = 0
                    self.progress_at = ev.t
            return []
        if ev.kind != "op_result":
            return []
        self.state = d.get("state") or self.state
        top = (d.get("state") or {}).get("top_modal")
        self._settle_closed(ev.t)
        if self.checked is not None and top is None:
            # モーダルがすべて閉じたかもしれない: modal_gap_sec のあいだモーダルが開かなければ、閉じたときから数え直す。
            # ロード → 右クリックメニュー → 設定のように、モーダルからモーダルへ移るあいだは数えない（ハーネスの
            # _mute_check も、モーダルがすべて閉じるまで確かめ直さない。run 2026-11-17_1400: 確かめたあとに設定の画面へ
            # 移り、数え直して検出した）
            if self.closed is None:
                self.closed = (ev.t, ev.wall)
        elif top is not None:
            self.closed = None
        if (d.get("kind") == "advance" and self.muted and top is not None and not d.get("interfered")
                and (self.progress_at is None or self.progress_at < ev.t - float(d.get("waited") or 0))):
            self.checked = str(top)  # 入力が止められたモーダルの上で、本文送りを押しても進まなかった
        if d.get("kind") != "advance" or d.get("interfered") or self.muted or not _alive(ev, self.alive):
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

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        self._settle_closed(now)
        if self.checked is not None:
            return []  # モーダルの上で、本文送りが進まないことを確かめた（閉じるまで数えない）
        if not self.muted or self.fired or self.script_at is None or now - self.script_at < self.idle:
            return []
        if self.heartbeat_at is None or now - self.heartbeat_at > self.alive:
            return []  # 止まっている・固まっているのは H-01 などで見る
        self.fired = True
        st = self.state or {}
        return [
            self.issue(
                f"本文送りの入力が止められたまま、{self.idle:g} 秒スクリプトが進みません（行 {st.get('line_id')}）。"
                "入力を止めたあと、戻し忘れていないか",
                key=f"muted|{st.get('line_id')}", launch=launch, t=now, wall=self.script_wall, state=st,
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
    ボタンのある画面（menu）とする。

    起動の引数で画面の大きさ・画面モードを渡した起動（Unity の -screen-width など。yomawari.toml の [builds.<名前>] args）
    では、その値はゲームが保存した値ではなく引数で決まるので、比べない。"""

    # Unity のコマンドライン引数 → その引数で決まる settings の値
    LAUNCH_ARGS = {
        "-screen-width": "resolution", "-screen-height": "resolution",
        "-screen-fullscreen": "screen_mode", "-window-mode": "screen_mode",
        "-popupwindow": "screen_mode", "-screen-quality": "quality",
    }

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.ok_codes = set(rule.param("harness_exit_codes"))
        self.ignore = set(rule.param("ignore_keys"))
        self.by_args: set[str] = set()  # この起動で、引数で決まった settings の値
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
            self.by_args = {self.LAUNCH_ARGS[a] for a in d.get("cmd") or [] if a in self.LAUNCH_ARGS}
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
            if k in self.by_args or k not in values or _same(values[k], want) or _same(values[k], last.get(k)):
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


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


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
            ms_slow = _median(self.samples[slow][: self.min_samples])
            ms_fast = _median(self.samples[fast][: self.min_samples])
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
    "F-01": SaveFileDetector,
    "C-03": BootTimeoutDetector,
    "C-04": ExitTimeoutDetector,
    "H-01": HeartbeatDetector,
    "L-04": WarningFloodDetector,
    "L-05": SeqGapDetector,
    "V-05": OffscreenDetector,
    "S-01": ResponseCheckDetector,
    "S-02": AdvanceStallDetector,
    "S-03": TransitionStuckDetector,
    "S-05": ButtonNoResponseDetector,
    "V-07": DimButtonRespondedDetector,
    "V-03": WindowLostDetector,
    "V-04": ResolutionMismatchDetector,
    "A-01": BgmOverlapDetector,
    "A-02": TitleVoiceDetector,
    "A-03": VolumeEffectDetector,
    "F-03": SettingsPersistDetector,
    "V-08": RevealSpeedDetector,
    "V-09": LocaleTextDetector,
    "L-01": LogErrorDetector,
    "L-06": LogWarningDetector,
    "L-02": MissingGlyphDetector,
    "L-03": ResourceLoadDetector,
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
        # L-02・L-03 で判定するログは、L-01・L-06 では数えない（同じログを2件にしない）
        claimed = ev.kind in ("log_error", "log_warning") and any(
            isinstance(d, _LogPatternDetector) and d.claims(ev) for d in self.detectors)
        for d in self.detectors:
            if claimed and isinstance(d, (LogErrorDetector, LogWarningDetector)):
                continue
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
