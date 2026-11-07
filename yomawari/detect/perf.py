"""パフォーマンスの判定（DESIGN.md §10「パフォーマンス」、R-01〜R-10）。

ゲームの heartbeat（FPS・いちばん長かったフレーム・メモリ）と、ハーネスの proc_stats（ゲームの CPU・GPU・RSS・スレッド数と、
マシンの状態）を1秒ごとのサンプルにまとめ、そのときの場所（scene・本文の行・直前の演出のコマンド）を付けて判定する。

ゲーム以外が原因と思われる時間（ほかのプロセスの負荷、AI への問い合わせ、熱、メモリの逼迫、画面が消えている、ゲームが
前面にない、スリープの前後）は判定から外し、外した時間だけを「ハーネスの記録」に書く。ゲームの CPU・GPU はプロセス単位で
測るので、ほかのプロセスの負荷では外さない。閾値はすべて rules.yaml（ルールの params と、共通の perf）に置く。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..events import Event, Issue

PERF_RULES = ("R-01", "R-02", "R-03", "R-04", "R-05", "R-06", "R-07", "R-08", "R-09", "R-10")
MB = 1024 * 1024

# 判定から外した理由 → レポートでの名前
REASONS = {
    "ai": "AI（Ollama）への問い合わせ中",
    "other_load": "ほかのプロセスの負荷",
    "thermal": "熱による性能低下",
    "pressure": "メモリの逼迫・スワップの増加",
    "asleep": "画面が消えている",
    "hidden": "ゲームが前面にない・隠れている・画面がロックされている",
    "gap": "スリープの前後（計測の間隔が空いた）",
}
# ゲームの CPU・GPU はプロセス単位で測るので、ほかのプロセスの負荷では外さない
CPU_GPU_IGNORES = {"other_load"}
THERMAL = ("nominal", "fair", "serious", "critical")
PRESSURE = ("normal", "warning", "critical")
MEMS = ("rss", "total", "gfx")
MEM_JA = {"rss": "RSS", "total": "Total Used Memory", "gfx": "Gfx Used Memory"}
METRIC_JA = {"fps": "FPS", "cpu": "CPU", "gpu": "GPU", **MEM_JA}


@dataclass
class Sample:
    t: float
    launch: int
    fps: float | None = None
    frame: float | None = None  # その1秒でいちばん長かったフレーム（ms）
    cpu: float | None = None  # 1 コアを 100% とした値
    gpu: float | None = None
    threads: int | None = None
    mem: dict[str, float] = field(default_factory=dict)  # MB
    scene: str | None = None
    line: str | None = None
    transition: bool = False
    settling: bool = False  # 起動・scene・ロードの直後
    out: frozenset[str] = frozenset()  # 判定から外す理由

    def val(self, metric: str) -> float | None:
        """判定に使える値（ゲーム以外が原因の時間なら None）。"""
        if metric in ("cpu", "gpu"):
            if self.out - CPU_GPU_IGNORES:
                return None
            return self.cpu if metric == "cpu" else self.gpu
        if self.out:
            return None
        if metric == "fps":
            return self.fps
        if metric == "frame":
            return self.frame
        if metric == "threads":
            return None if self.threads is None else float(self.threads)
        return self.mem.get(metric)

    @property
    def where(self) -> str:
        return f"scene `{self.scene}`、行 `{self.line}`"


@dataclass
class Cmd:
    """演出のコマンドの実行（R-08）。"""

    t: float
    launch: int
    key: str  # 「@char Kaori」など
    line: str | None
    before: list[Sample]


def _median(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return statistics.median(vals) if vals else None


def _low1(xs: list[float]) -> float | None:
    """下位 1%（FPS の落ち込み）。"""
    if not xs:
        return None
    s = sorted(xs)
    return s[max(0, int(len(s) * 0.01) - 1)] if len(s) >= 100 else s[0]


def _fmt(x: float | None, digits: int = 0, unit: str = "") -> str:
    return "-" if x is None else f"{x:.{digits}f}{unit}"


def load_perf(path: Path) -> dict[str, Any] | None:
    """rules.yaml の perf（パフォーマンスの判定で共通の設定）。なければ None。"""
    import yaml

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    perf = doc.get("perf")
    return perf if isinstance(perf, dict) else None


def load_history(runs_dir: Path, build: str, machine: str, current: Path | None, n: int) -> list[dict[str, Any]]:
    """同じビルド名・同じマシンの、前回までのランの perf.json（新しい順に n 個）。"""
    out: list[dict[str, Any]] = []
    if n <= 0 or not runs_dir.is_dir():
        return out
    for d in sorted((p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith("_")), reverse=True):
        if current is not None and d.resolve() == current.resolve():
            continue
        try:
            doc = json.loads((d / "perf.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("build") == build and doc.get("machine") == machine:
            doc["run"] = d.name
            out.append(doc)
            if len(out) >= n:
                break
    return out


class PerfMonitor:
    """R-01〜R-10 をまとめて判定する（Engine から Detector と同じように呼ぶ）。"""

    def __init__(self, rules: dict[str, Any], perf: dict[str, Any]) -> None:
        self.rules = rules  # rule_id → Rule（PERF_RULES のうち rules.yaml にあるもの）
        self.cfg = perf
        ex = perf["exclude"]
        self.warmup = float(perf["warmup_sec"])
        self.settle = float(perf["settle_sec"])
        self.min_samples = int(perf["min_samples"])
        self.history_runs = int(perf["history_runs"])
        self.other_cpu = float(ex["other_cpu_percent"])
        self.other_gpu = float(ex["other_gpu_percent"])
        self.thermal = THERMAL.index(str(ex["thermal"]))
        self.pressure = PRESSURE.index(str(ex["pressure"]))
        self.swap_mb = float(ex["swap_growth_mb"])
        self.gap = float(ex["gap_sec"])
        self.after_gap = float(ex["after_gap_sec"])
        self.samples: list[Sample] = []
        self.excluded: Counter[str] = Counter()  # 理由 → 外した秒数
        self.history: list[dict[str, Any]] = []  # 前回までのランの perf.json（R-10）
        self.fired: set[str] = set()
        # いまの状態
        self.launch = 0
        self.scene: str | None = None
        self.line: str | None = None
        self.transitions: set[str] = set()
        self.loading = False
        self.boot_t: float | None = None
        self.settle_until = 0.0
        self.stats: dict[str, Any] = {}
        self.stats_t: float | None = None
        self.gap_until = 0.0
        self.swaps: deque[tuple[float, float]] = deque()
        self.reasons: frozenset[str] = frozenset()
        self.last_hb: float | None = None
        # ルールごとの状態
        self.low_run: list[Sample] = []  # R-02
        self.spike: dict[str, list[Sample]] = {"cpu": [], "gpu": []}  # R-04・R-05
        self.base_cache: dict[str, tuple[int, float | None]] = {}
        self.mem_win: dict[str, deque[tuple[float, float]]] = {m: deque() for m in MEMS}  # R-06
        self.mem_jump: dict[str, tuple[float, float, float, Sample] | None] = {m: None for m in MEMS}
        self.hitches: Counter[str] = Counter()  # R-07
        self.recent_cmds: deque[tuple[float, str]] = deque()
        self.pending: list[Cmd] = []  # R-08
        self.cmd_bad: Counter[tuple[str, str]] = Counter()
        self.cmd_stats: dict[str, dict[str, list[float]]] = {}
        self.cmd_with: dict[str, set[str]] = {}
        self.last_check = 0.0

    # --- 共通 ---

    def has(self, rule_id: str) -> bool:
        return rule_id in self.rules

    def p(self, rule_id: str, name: str) -> Any:
        return self.rules[rule_id].param(name)

    def _issue(self, rule_id: str, detail: str, key: str, s: Sample | None, t: float, wall: str = "",
               **evidence: Any) -> list[Issue]:
        if rule_id not in self.rules or f"{rule_id}:{key}" in self.fired:
            return []
        self.fired.add(f"{rule_id}:{key}")
        rule = self.rules[rule_id]
        if s is not None:
            evidence.setdefault("state", {"scene": s.scene, "line_id": s.line, "menus": [], "top_modal": None})
        return [Issue(rule_id=rule_id, severity=rule.severity, title=rule.title, detail=detail,
                      key=f"{rule_id}:{key}", launch=s.launch if s else self.launch, t=t, wall=wall,
                      action=rule.on_detect, evidence=evidence)]

    def _ok(self, s: Sample) -> bool:
        """遷移中・起動や scene の直後でない（演出の負荷を、ふだんの値と比べないように）。"""
        return not s.transition and not s.settling

    # --- イベント ---

    def on_event(self, ev: Event) -> list[Issue]:
        k, d = ev.kind, ev.data
        if k == "launch":
            self._reset_launch(ev)
        elif k == "proc_stats":
            self._on_stats(ev)
        elif ev.source != "game":
            return []
        elif k == "boot":
            self.boot_t = ev.t
            self.settle_until = ev.t + self.warmup
        elif k == "scene":
            self.scene = str(d.get("name"))
            self.settle_until = max(self.settle_until, ev.t + self.settle)
        elif k == "line":
            self.line = d.get("line_id")
        elif k == "transition":
            name = str(d.get("name"))
            if d.get("phase") == "start":
                self.transitions.add(name)
            else:
                self.transitions.discard(name)
                self.settle_until = max(self.settle_until, ev.t + self.settle)
        elif k == "load":
            self.loading = d.get("phase") == "start"
            if not self.loading:
                self.settle_until = max(self.settle_until, ev.t + self.settle)
        elif k == "command":
            return self._on_command(ev)
        elif k == "heartbeat":
            return self._on_heartbeat(ev)
        return []

    def on_tick(self, now: float, launch: int) -> list[Issue]:
        if now - self.last_check < 60:
            return []
        self.last_check = now
        return self._trend(now) + self._threads(now)

    def _reset_launch(self, ev: Event) -> None:
        self.launch = ev.launch
        self.scene = self.line = None
        self.transitions.clear()
        self.loading = False
        self.boot_t = None
        self.settle_until = float("inf")  # boot までは計らない
        self.stats, self.stats_t = {}, None
        self.swaps.clear()
        self.reasons = frozenset()
        self.last_hb = None
        self.low_run = []
        self.spike = {"cpu": [], "gpu": []}
        self.mem_win = {m: deque() for m in MEMS}
        self.mem_jump = {m: None for m in MEMS}
        self.pending = []
        self.recent_cmds.clear()

    def _on_stats(self, ev: Event) -> None:
        d = ev.data
        if self.stats_t is not None and ev.t - self.stats_t >= self.gap:
            self.gap_until = ev.t + self.after_gap
        self.stats, self.stats_t = d, ev.t
        sysd = d.get("sys") or {}
        out: set[str] = set()
        if sysd.get("ai"):
            out.add("ai")
        ncpu = float(sysd.get("ncpu") or 1)
        if sysd.get("cpu") is not None and d.get("cpu") is not None:
            if float(sysd["cpu"]) - float(d["cpu"]) / ncpu >= self.other_cpu:
                out.add("other_load")
        if sysd.get("gpu_device") is not None and float(sysd["gpu_device"]) - float(d.get("gpu") or 0) >= self.other_gpu:
            out.add("other_load")
        if sysd.get("thermal") in THERMAL and THERMAL.index(sysd["thermal"]) >= self.thermal:
            out.add("thermal")
        if sysd.get("pressure") in PRESSURE and PRESSURE.index(sysd["pressure"]) >= self.pressure:
            out.add("pressure")
        if sysd.get("swap") is not None:
            self.swaps.append((ev.t, float(sysd["swap"]) / MB))
            while self.swaps and ev.t - self.swaps[0][0] > 60:
                self.swaps.popleft()
            if self.swaps[-1][1] - min(v for _, v in self.swaps) >= self.swap_mb:
                out.add("pressure")
        if sysd.get("asleep"):
            out.add("asleep")
        if sysd.get("locked") or sysd.get("front") is False or sysd.get("on_screen") is False:
            out.add("hidden")
        if ev.t < self.gap_until:
            out.add("gap")
        self.reasons = frozenset(out)

    def _on_heartbeat(self, ev: Event) -> list[Issue]:
        d = ev.data
        st = self.stats if self.stats_t is not None and ev.t - self.stats_t <= self.gap else {}
        reasons = self.reasons if st else frozenset()
        if ev.t < self.gap_until:
            reasons = reasons | {"gap"}
        mem: dict[str, float] = {}
        if st.get("rss") is not None:
            mem["rss"] = float(st["rss"]) / MB
        for src, dst in (("mem_total", "total"), ("mem_gfx", "gfx")):
            if d.get(src) is not None:
                mem[dst] = float(d[src]) / MB
        s = Sample(
            t=ev.t, launch=ev.launch, fps=_num(d.get("fps")), frame=_num(d.get("frame_max")),
            cpu=_num(st.get("cpu")), gpu=_num(st.get("gpu")), threads=st.get("threads"), mem=mem,
            scene=self.scene, line=self.line, transition=bool(self.transitions) or self.loading,
            settling=ev.t < self.settle_until, out=reasons,
        )
        dt = 0.0 if self.last_hb is None else min(ev.t - self.last_hb, self.gap)
        self.last_hb = ev.t
        if self.boot_t is None or ev.t < self.boot_t + self.warmup:
            return []  # 起動直後（読み込み・シェーダーのコンパイル）は計らない
        for r in reasons:
            self.excluded[r] += dt
        self.samples.append(s)
        out: list[Issue] = []
        out += self._low_fps(s, ev.wall)
        out += self._spikes(s, ev.wall)
        out += self._mem_jumps(s, ev.wall)
        out += self._hitch(s, ev.wall)
        out += self._commands_after(s, ev.wall)
        return out

    def _on_command(self, ev: Event) -> list[Issue]:
        d = ev.data
        name = d.get("name")
        if not name:
            return []
        target = str(d.get("target") or "").strip()
        key = f"@{name} {target}".strip()
        self.recent_cmds.append((ev.t, key))
        win = float(self.p("R-08", "window_sec")) if self.has("R-08") else 3.0
        before = [s for s in self.samples[-int(win) - 2:] if s.launch == ev.launch and ev.t - win <= s.t <= ev.t]
        self.pending.append(Cmd(ev.t, ev.launch, key, d.get("line_id"), before))
        return []

    # --- R-02: FPS が低いまま ---

    def _low_fps(self, s: Sample, wall: str) -> list[Issue]:
        if not self.has("R-02"):
            return []
        fps = s.val("fps")
        if fps is None or not self._ok(s) or fps >= float(self.p("R-02", "min_fps")):
            self.low_run = []
            return []
        self.low_run.append(s)
        first = self.low_run[0]
        sec = s.t - first.t + 1
        if sec < float(self.p("R-02", "sec")):
            return []
        lo = min(x.fps or 0 for x in self.low_run)
        return self._issue("R-02", f"FPS が {self.p('R-02', 'min_fps')} 未満のまま {sec:.0f} 秒続きました"
                           f"（最低 {lo:.1f}、{first.where}）", key=f"{first.scene}|{first.line}", s=first,
                           t=s.t, wall=wall)

    # --- R-04・R-05: CPU・GPU が急に上がる ---

    def _baseline(self, metric: str) -> float | None:
        n = len(self.samples)
        cached = self.base_cache.get(metric)
        if cached is not None and n - cached[0] < 30:
            return cached[1]
        vals = [v for x in self.samples if self._ok(x) and (v := x.val(metric)) is not None]
        base = statistics.median(vals) if len(vals) >= self.min_samples else None
        self.base_cache[metric] = (n, base)
        return base

    def _spikes(self, s: Sample, wall: str) -> list[Issue]:
        out: list[Issue] = []
        for metric, rule in (("cpu", "R-04"), ("gpu", "R-05")):
            if not self.has(rule):
                continue
            v, base = s.val(metric), self._baseline(metric)
            run = self.spike[metric]
            add, ratio = float(self.p(rule, "add")), float(self.p(rule, "ratio"))
            if v is None or base is None or not self._ok(s) or v < base + add or v < base * ratio:
                run.clear()
                continue
            run.append(s)
            sec = s.t - run[0].t + 1
            if sec < float(self.p(rule, "sec")):
                continue
            first = run[0]
            peak = max(x.val(metric) or 0 for x in run)
            cmds = self._cmds_near(first.t, float(self.p(rule, "sec")))
            out += self._issue(rule, f"ゲームの {METRIC_JA[metric]} 使用率が、ふだん（中央値 {base:.0f}%）より上がったまま "
                               f"{sec:.0f} 秒続きました（最大 {peak:.0f}%、{first.where}"
                               f"{'、直前の演出 ' + ', '.join(f'`{c}`' for c in cmds) if cmds else ''}）",
                               key=f"{first.scene}|{first.line}", s=first, t=s.t, wall=wall)
        return out

    # --- R-06: メモリが急に増えて戻らない ---

    def _mem_jumps(self, s: Sample, wall: str) -> list[Issue]:
        if not self.has("R-06"):
            return []
        jump, win = float(self.p("R-06", "jump_mb")), float(self.p("R-06", "window_sec"))
        hold, keep = float(self.p("R-06", "hold_sec")), float(self.p("R-06", "keep_ratio"))
        out: list[Issue] = []
        for m in MEMS:
            v = s.val(m)
            if v is None:
                continue
            q = self.mem_win[m]
            q.append((s.t, v))
            while q and s.t - q[0][0] > win:
                q.popleft()
            pend = self.mem_jump[m]
            if pend is None:
                low = min(x for _, x in q)
                if v - low >= jump:
                    cmds = self._cmds_near(s.t, win)
                    self.mem_jump[m] = (s.t, low, v, s)
                    self.cmd_with[f"mem:{m}:{s.t}"] = set(cmds)
                continue
            t0, base, peak, at = pend
            peak = max(peak, v)
            self.mem_jump[m] = (t0, base, peak, at)
            if s.t - t0 < hold:
                continue
            self.mem_jump[m] = None
            q.clear()
            if v - base < (peak - base) * keep:
                continue
            cmds = self.cmd_with.pop(f"mem:{m}:{t0}", set())
            out += self._issue("R-06", f"{MEM_JA[m]} が {win:.0f} 秒で {peak - base:.0f}MB 増え（{base:.0f} → {peak:.0f}MB）、"
                               f"{hold:.0f} 秒たっても {v:.0f}MB のままでした（{at.where}"
                               f"{'、直前の演出 ' + ', '.join(f'`{c}`' for c in sorted(cmds)) if cmds else ''}）",
                               key=f"{m}|{at.scene}|{at.line}", s=at, t=s.t, wall=wall)
        return out

    # --- R-07: 重いフレームが同じところで出る ---

    def _hitch(self, s: Sample, wall: str) -> list[Issue]:
        if not self.has("R-07"):
            return []
        ms = s.val("frame")
        limit = float(self.p("R-07", "frame_ms"))
        if ms is None or ms < limit or not self._ok(s):
            return []
        out: list[Issue] = []
        keys = [f"行 `{s.line}`"] if s.line else []
        keys += [f"`{c}` のあと" for c in self._cmds_near(s.t, float(self.p("R-07", "attribute_sec")))]
        for key in keys:
            self.hitches[key] += 1
            if self.hitches[key] >= int(self.p("R-07", "repeats")):
                out += self._issue("R-07", f"{key}に、{limit:.0f}ms 以上のフレームが {self.hitches[key]} 回出ました"
                                   f"（今回 {ms:.0f}ms、{s.where}）", key=key, s=s, t=s.t, wall=wall)
        return out

    def _cmds_near(self, t: float, sec: float) -> list[str]:
        while self.recent_cmds and t - self.recent_cmds[0][0] > 30:
            self.recent_cmds.popleft()
        return list(dict.fromkeys(k for ct, k in self.recent_cmds if t - sec <= ct <= t))

    # --- R-08: 演出のコマンドの前後 ---

    def _commands_after(self, s: Sample, wall: str) -> list[Issue]:
        if not self.pending:
            return []
        win = float(self.p("R-08", "window_sec")) if self.has("R-08") else 3.0
        out: list[Issue] = []
        keep: list[Cmd] = []
        for c in self.pending:
            if c.launch != s.launch:
                continue
            if s.t < c.t + win:
                keep.append(c)
                continue
            out += self._judge_cmd(c, win, s, wall)
        self.pending = keep
        return out

    def _judge_cmd(self, c: Cmd, win: float, now: Sample, wall: str) -> list[Issue]:
        after = [x for x in self.samples[-int(win) - 3:] if x.launch == c.launch and c.t < x.t <= c.t + win]
        need = int(self.p("R-08", "min_samples")) if self.has("R-08") else 2
        if len(c.before) < need or len(after) < need:
            return []
        effects: dict[str, float] = {}
        fb, fa = _median(x.val("fps") for x in c.before), _median(x.val("fps") for x in after)
        if fb and fa is not None:
            effects["fps"] = (fb - fa) / fb  # 落ちた割合
        for m in ("cpu", "gpu"):
            b, a = _median(x.val(m) for x in c.before), _median(x.val(m) for x in after)
            if b is not None and a is not None:
                effects[m] = a - b
        mem_up = []
        for m in MEMS:
            b = _median(x.val(m) for x in c.before)
            a = max((v for x in after if (v := x.val(m)) is not None), default=None)
            if b is not None and a is not None:
                mem_up.append(a - b)
        if mem_up:
            effects["mem"] = max(mem_up)
        if not effects:
            return []
        st = self.cmd_stats.setdefault(c.key, {})
        for k, v in effects.items():
            st.setdefault(k, []).append(v)
        self.cmd_with.setdefault(c.key, set()).update(k for t, k in self.recent_cmds
                                                      if abs(t - c.t) <= win and k != c.key)
        if not self.has("R-08"):
            return []
        limits = {"fps": float(self.p("R-08", "fps_drop_ratio")), "cpu": float(self.p("R-08", "cpu_add")),
                  "gpu": float(self.p("R-08", "gpu_add")), "mem": float(self.p("R-08", "mem_mb"))}
        out: list[Issue] = []
        for k, v in effects.items():
            if v < limits[k]:
                continue
            self.cmd_bad[(c.key, k)] += 1
            n = self.cmd_bad[(c.key, k)]
            if n < int(self.p("R-08", "repeats")):
                continue
            vals = st[k]
            what = {"fps": f"FPS が平均 {statistics.mean(vals) * 100:.0f}% 落ちた",
                    "cpu": f"ゲームの CPU 使用率が平均 +{statistics.mean(vals):.0f} ポイント上がった",
                    "gpu": f"ゲームの GPU 使用率が平均 +{statistics.mean(vals):.0f} ポイント上がった",
                    "mem": f"メモリが最大 +{max(vals):.0f}MB 増えた"}[k]
            others = sorted(self.cmd_with.get(c.key, set()))
            with_ = f"。同じころに実行したコマンド: {', '.join(f'`{o}`' for o in others[:5])}" if others else ""
            out += self._issue("R-08", f"`{c.key}` の実行後 {win:.0f} 秒で、実行前より{what}（{n} 回、行 `{c.line}`）{with_}",
                               key=f"{c.key}|{k}", s=now, t=now.t, wall=wall)
        return out

    # --- R-01: メモリが増え続ける / R-03: スレッド数が増え続ける（定期的に見る） ---

    def _trend(self, now: float) -> list[Issue]:
        if not self.has("R-01"):
            return []
        span_min = float(self.p("R-01", "min_span_sec"))
        limit = float(self.p("R-01", "mb_per_hour"))
        out: list[Issue] = []
        xs = [s for s in self.samples if s.launch == self.launch]
        for m in self.p("R-01", "metrics"):
            pts = [(s.t, v) for s in xs if (v := s.val(m)) is not None]
            if len(pts) < self.min_samples or pts[-1][0] - pts[0][0] < span_min:
                continue
            slope = _slope(pts) * 3600
            if slope < limit:
                continue
            out += self._issue("R-01", f"起動 #{self.launch} で、{MEM_JA[m]} が 1 時間あたり {slope:.0f}MB のペースで"
                               f"増え続けています（{(pts[-1][0] - pts[0][0]) / 60:.0f} 分間で {pts[0][1]:.0f} → "
                               f"{pts[-1][1]:.0f}MB）", key=f"{self.launch}|{m}", s=xs[-1], t=now)
        return out

    def _threads(self, now: float) -> list[Issue]:
        if not self.has("R-03"):
            return []
        win = float(self.p("R-03", "window_sec"))
        pts = [(s.t, s.threads) for s in self.samples if s.launch == self.launch and s.threads is not None
               and now - s.t <= win]
        if len(pts) < self.min_samples or pts[-1][0] - pts[0][0] < win * 0.9:
            return []
        top, drop = pts[0][1], 0
        for _, n in pts:
            top = max(top, n)
            drop = max(drop, top - n)
        if pts[-1][1] - pts[0][1] < int(self.p("R-03", "min_increase")) or drop > int(self.p("R-03", "max_drop")):
            return []
        return self._issue("R-03", f"起動 #{self.launch} で、スレッド数が {win / 60:.0f} 分間増え続けています"
                           f"（{pts[0][1]} → {pts[-1][1]}）", key=str(self.launch), s=None, t=now)

    # --- ランの終わり: R-09（同じランの中）・R-10（前回まで） ---

    def scene_stats(self) -> dict[str, dict[str, Any]]:
        by: dict[str, list[Sample]] = {}
        for s in self.samples:
            if s.scene and self._ok(s):
                by.setdefault(s.scene, []).append(s)
        out: dict[str, dict[str, Any]] = {}
        for scene, xs in by.items():
            fps = [v for x in xs if (v := x.val("fps")) is not None]
            row: dict[str, Any] = {"n": len(fps), "fps": _median(fps), "fps_low": _low1(fps)}
            for m in ("cpu", "gpu"):
                vals = [v for x in xs if (v := x.val(m)) is not None]
                row[m], row[f"{m}_max"] = _median(vals), max(vals, default=None)
            for m in MEMS:
                vals = [v for x in xs if (v := x.val(m)) is not None]
                row[m], row[f"{m}_max"] = _median(vals), max(vals, default=None)
            out[scene] = row
        return out

    def command_stats(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, st in self.cmd_stats.items():
            row: dict[str, Any] = {"n": max(len(v) for v in st.values())}
            for k, vals in st.items():
                row[k] = max(vals) if k == "mem" else statistics.mean(vals)
            out[key] = row
        return out

    def finish(self, now: float, wall: str = "") -> list[Issue]:
        return self._by_scene(now, wall) + self._vs_history(now, wall)

    def _by_scene(self, now: float, wall: str) -> list[Issue]:
        if not self.has("R-09"):
            return []
        need = int(self.p("R-09", "min_samples"))
        run = {m: _median(v for s in self.samples if self._ok(s) and (v := s.val(m)) is not None)
               for m in ("fps", "cpu", "gpu")}
        out: list[Issue] = []
        for scene, row in self.scene_stats().items():
            if row["n"] < need:
                continue
            bad = []
            if run["fps"] and row["fps"] is not None and row["fps"] <= run["fps"] * (1 - float(self.p("R-09", "fps_drop_ratio"))):
                bad.append(f"FPS の中央値 {row['fps']:.1f}（ラン全体 {run['fps']:.1f}）")
            for m in ("cpu", "gpu"):
                if run[m] is not None and row[m] is not None and row[m] >= run[m] + float(self.p("R-09", f"{m}_add")):
                    bad.append(f"{METRIC_JA[m]} 使用率の中央値 {row[m]:.0f}%（ラン全体 {run[m]:.0f}%）")
            if bad:
                out += self._issue("R-09", f"scene `{scene}` が、ほかの場所より重いです: {'、'.join(bad)}"
                                   f"（{row['n']} 秒分のサンプル）", key=scene, s=None, t=now, wall=wall)
        return out

    def _vs_history(self, now: float, wall: str) -> list[Issue]:
        if not self.has("R-10") or not self.history:
            return []
        ratio = float(self.p("R-10", "ratio"))
        adds = {"cpu": float(self.p("R-10", "cpu_add")), "gpu": float(self.p("R-10", "gpu_add"))}
        mem_mb = float(self.p("R-10", "mem_mb"))
        need = int(self.p("R-10", "min_samples"))
        runs = ", ".join(h["run"] for h in self.history)
        out: list[Issue] = []
        for scene, row in self.scene_stats().items():
            past = [h["scenes"][scene] for h in self.history if scene in h.get("scenes", {})
                    and h["scenes"][scene].get("n", 0) >= need]
            if row["n"] < need or not past:
                continue
            bad = []
            pf = _median(p.get("fps") for p in past)
            if pf and row["fps"] is not None and row["fps"] <= pf * (1 - ratio):
                bad.append(f"FPS の中央値 {pf:.1f} → {row['fps']:.1f}")
            for m in ("cpu", "gpu"):
                pv = _median(p.get(m) for p in past)
                if pv is not None and row[m] is not None and row[m] >= pv * (1 + ratio) and row[m] - pv >= adds[m]:
                    bad.append(f"{METRIC_JA[m]} 使用率の中央値 {pv:.0f}% → {row[m]:.0f}%")
            for m in MEMS:
                pv = _median(p.get(f"{m}_max") for p in past)
                cur = row.get(f"{m}_max")
                if pv is not None and cur is not None and cur >= pv * (1 + ratio) and cur - pv >= mem_mb:
                    bad.append(f"{MEM_JA[m]} の最大 {pv:.0f} → {cur:.0f}MB")
            if bad:
                out += self._issue("R-10", f"scene `{scene}` が、前回までのラン（{runs}）より悪くなりました: {'、'.join(bad)}",
                                   key=f"scene|{scene}", s=None, t=now, wall=wall)
        cur_cmds = self.command_stats()
        for key, row in cur_cmds.items():
            past = [h["commands"][key] for h in self.history if key in h.get("commands", {})]
            if not past:
                continue
            bad = []
            pf = _median(p.get("fps") for p in past)
            if pf is not None and row.get("fps") is not None and row["fps"] - pf >= ratio:
                bad.append(f"FPS の落ち方 {pf * 100:.0f}% → {row['fps'] * 100:.0f}%")
            for m in ("cpu", "gpu"):
                pv = _median(p.get(m) for p in past)
                if pv is not None and row.get(m) is not None and row[m] - pv >= adds[m]:
                    bad.append(f"{METRIC_JA[m]} の上がり方 +{pv:.0f} → +{row[m]:.0f} ポイント")
            pv = _median(p.get("mem") for p in past)
            if pv is not None and row.get("mem") is not None and row["mem"] - pv >= mem_mb:
                bad.append(f"メモリの増え方 +{pv:.0f} → +{row['mem']:.0f}MB")
            if bad:
                out += self._issue("R-10", f"`{key}` の実行前後の変化が、前回までのラン（{runs}）より悪くなりました: "
                                   f"{'、'.join(bad)}", key=f"cmd|{key}", s=None, t=now, wall=wall)
        return out

    # --- 保存とレポート ---

    def summary_json(self, build: str, machine: str, build_id: str | None) -> dict[str, Any]:
        return {"version": 1, "build": build, "machine": machine, "build_id": build_id,
                "scenes": self.scene_stats(), "commands": self.command_stats()}

    def excluded_note(self) -> str | None:
        parts = [f"{REASONS[r]} {sec / 60:.1f} 分" for r, sec in self.excluded.most_common() if sec >= 1]
        if not parts:
            return None
        return f"パフォーマンスの判定から、ゲーム以外が原因と見なして外した時間: {'、'.join(parts)}"

    def report_lines(self) -> list[str]:
        if not self.samples:
            return []
        out = ["## パフォーマンス", "",
               "遷移中・起動や scene の直後と、ゲーム以外が原因と見なした時間（「ハーネスの記録」）を除いた値。"
               "CPU・GPU はゲームのプロセスの使用率（CPU は 1 コアを 100% とした値）。メモリは MB。", ""]
        scenes = self.scene_stats()
        past: dict[str, float | None] = {}
        for scene in scenes:
            past[scene] = _median(h["scenes"][scene].get("fps") for h in self.history if scene in h.get("scenes", {}))
        out += ["### scene ごと", "",
                "| scene | 秒 | FPS 中央値 | FPS 下位1% | CPU 中央値 / 最大 | GPU 中央値 / 最大 | RSS 最大 | Total 最大 | Gfx 最大 | 前回までの FPS 中央値 |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for scene, r in sorted(scenes.items(), key=lambda kv: (kv[1]["fps"] is None, kv[1]["fps"] or 0)):
            out.append(f"| `{scene}` | {r['n']} | {_fmt(r['fps'], 1)} | {_fmt(r['fps_low'], 1)} | "
                       f"{_fmt(r['cpu'])} / {_fmt(r['cpu_max'])} | {_fmt(r['gpu'])} / {_fmt(r['gpu_max'])} | "
                       f"{_fmt(r['rss_max'])} | {_fmt(r['total_max'])} | {_fmt(r['gfx_max'])} | {_fmt(past[scene], 1)} |")
        out.append("")
        cmds = self.command_stats()
        if cmds:
            lim = ({"fps": float(self.p("R-08", "fps_drop_ratio")), "cpu": float(self.p("R-08", "cpu_add")),
                    "gpu": float(self.p("R-08", "gpu_add")), "mem": float(self.p("R-08", "mem_mb"))}
                   if self.has("R-08") else {"fps": 0.3, "cpu": 50.0, "gpu": 30.0, "mem": 100.0})

            def score(r: dict[str, Any]) -> float:
                return max((r.get(k) or 0) / v for k, v in lim.items() if v)

            top = sorted(cmds.items(), key=lambda kv: score(kv[1]), reverse=True)[:10]
            out += ["### 演出（コマンド）ごと: 実行前後の差が大きいもの", "",
                    "実行前と実行後（それぞれ数秒）の差の平均。メモリは最大。", "",
                    "| コマンド | 回数 | FPS の落ち方 | CPU | GPU | メモリ |", "|---|---|---|---|---|---|"]
            for key, r in top:
                out.append(f"| `{key}` | {r['n']} | {_fmt(None if r.get('fps') is None else r['fps'] * 100, 0, '%')} | "
                           f"{_fmt(r.get('cpu'), 0)} | {_fmt(r.get('gpu'), 0)} | {_fmt(r.get('mem'), 0)} |")
            out.append("")
        return out


def _num(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _slope(pts: list[tuple[float, float]]) -> float:
    """最小二乗法の傾き（値 / 秒）。"""
    n = len(pts)
    mx = sum(t for t, _ in pts) / n
    my = sum(v for _, v in pts) / n
    den = sum((t - mx) ** 2 for t, _ in pts)
    return 0.0 if den == 0 else sum((t - mx) * (v - my) for t, v in pts) / den
