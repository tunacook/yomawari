"""ラン・ライフサイクル: 準備 → 起動 → 監視 → （クラッシュ時は再起動して継続）→ 終了処理 → 後処理。

フェーズ1ではハーネス本体と同じプロセス内で動かす。別プロセス化はフェーズ6で行う。
"""

from __future__ import annotations

import asyncio
import platform as pyplatform
import random
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, launcher
from .bus import EventBus
from .config import BuildConfig
from .detect.engine import Engine, Rule
from .events import Event, Issue, now_wall
from .platform import Platform
from .post.report import RunSummary, write_report
from .savestate import SaveState, resolve_target
from .state import GameState
from .sensors.crash import watch_crash_dir
from .sensors.logtail import InvalidRecord, Record, UnityEntry, YomawariRecord, tail_file
from .sensors.process import watch_process

StatusCallback = Callable[[dict[str, Any]], None]


@dataclass
class RunOptions:
    hours: float
    mode: str
    seed: str


@dataclass
class RunResult:
    run_dir: Path
    report_path: Path
    issues: list[Issue] = field(default_factory=list)
    interrupted: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if any(i.severity == "critical" for i in self.issues) else 0


def new_run_dir(runs_dir: Path, build_name: str) -> Path:
    base = f"{datetime.now():%Y-%m-%d_%H%M}_{build_name}"
    p, n = runs_dir / base, 1
    while p.exists():
        n += 1
        p = runs_dir / f"{base}_{n}"
    p.mkdir(parents=True)
    return p


class Harness:
    def __init__(
        self,
        build: BuildConfig,
        exe: Path,
        platform: Platform,
        rules: dict[str, Rule],
        rules_path: Path,
        runs_dir: Path,
        run_cfg: dict[str, Any],
        sensor_cfg: dict[str, Any],
        on_status: StatusCallback | None = None,
    ) -> None:
        self.build = build
        self.exe = exe
        self.platform = platform
        self.rules = rules
        self.rules_path = rules_path
        self.runs_dir = runs_dir
        self.run_cfg = run_cfg
        self.sensor_cfg = sensor_cfg
        self.on_status = on_status
        self.engine = Engine(rules)
        self.state = GameState()
        self.issues: list[Issue] = []
        self.notes: list[str] = []
        self.launches = 0
        self.restarts = 0
        self._stopping = False
        self._restart_req: asyncio.Event | None = None
        self._crash_seen: set[Path] = set()
        self._bus: EventBus | None = None
        self._started = 0.0
        self._last_status = 0.0

    # --- イベント処理 ---

    def _publish(self, ev: Event) -> None:
        assert self._bus is not None
        self._bus.publish(ev)

    def _on_event(self, ev: Event) -> None:
        if ev.kind == "issue":
            return
        self.state.feed(ev)
        self._handle_issues(self.engine.feed(ev))
        if ev.kind == "proc_exit" and not ev.data["by_harness"] and self._restart_req is not None:
            self._restart_req.set()

    def _handle_issues(self, issues: list[Issue]) -> None:
        for i in issues:
            self.issues.append(i)
            self._publish(Event("harness", "issue", i.to_json(), i.launch))
            if i.action == "restart" and self._restart_req is not None:
                self._restart_req.set()

    def _on_records(self, recs: list[Record], launch: int) -> None:
        for r in recs:
            if isinstance(r, YomawariRecord):
                self._publish(Event("game", str(r.data["ev"]), r.data, launch))
            elif isinstance(r, UnityEntry):
                data = {"message": r.message, "stack": r.stack, "has_stack": r.has_stack}
                self._publish(Event("log", "unity_log", data, launch))
            elif isinstance(r, InvalidRecord):
                self.notes.append(f"[YOMAWARI] 行を解釈できません（起動 #{launch}）: {r.error}: {r.raw[:120]}")
                self._publish(Event("harness", "invalid_line", {"raw": r.raw, "error": r.error}, launch))

    async def _ticker(self, launch: int, stop: asyncio.Event) -> None:
        interval = float(self.sensor_cfg["tick_sec"])
        while not stop.is_set():
            now = time.monotonic()
            self._handle_issues(self.engine.tick(now, launch))
            self._status(now)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    def _status(self, now: float, force: bool = False) -> None:
        if self.on_status is None or (not force and now - self._last_status < 1.0):
            return
        self._last_status = now
        counts = {s: sum(1 for i in self.issues if i.severity == s) for s in ("critical", "major", "minor")}
        last = self.issues[-1] if self.issues else None
        self.on_status(
            {
                "elapsed": now - self._started,
                "build": self.build.name,
                "launches": self.launches,
                "restarts": self.restarts,
                "counts": counts,
                "last": f"{last.rule_id} {last.detail}" if last else "",
                "scene": self.state.scene,
            }
        )

    # --- 1回の起動 ---

    async def _session(self, launch: int, deadline: float, run_dir: Path) -> str:
        """ゲームを1回起動して監視する。戻り値は "restart" か "deadline"。"""
        log_path = run_dir / ("player.log" if launch == 0 else f"player.{launch}.log")
        self._stopping = False
        self._restart_req = asyncio.Event()
        stop = asyncio.Event()
        since = time.time() - 1.0
        proc = launcher.launch(self.exe, self.build, log_path)
        self.launches += 1
        self._publish(
            Event(
                "harness", "launch",
                {"pid": proc.pid, "cmd": launcher.build_command(self.exe, self.build, log_path)},
                launch,
            )
        )
        s = self.sensor_cfg
        tasks = [
            asyncio.create_task(
                tail_file(
                    log_path, lambda recs: self._on_records(recs, launch), stop,
                    float(s["log_poll_sec"]), float(s["log_flush_sec"]),
                )
            ),
            asyncio.create_task(
                watch_process(
                    proc, self._publish, launch, stop, float(s["process_poll_sec"]),
                    lambda: self._stopping,
                )
            ),
            asyncio.create_task(
                watch_crash_dir(
                    self.platform.crash_dir(), self.exe.name, since, self.platform.crash_suffixes(),
                    self._publish, launch, stop, float(s["crash_poll_sec"]), self._crash_seen,
                )
            ),
            asyncio.create_task(self._ticker(launch, stop)),
        ]
        reason = "deadline"
        cancelled = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._restart_req.wait(), timeout=remaining)
                reason = "restart"
            except TimeoutError:
                pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            await self._end_session(proc, stop, tasks, wait_dump=not cancelled and reason == "restart")
        return reason

    async def _end_session(
        self,
        proc: subprocess.Popen[bytes],
        stop: asyncio.Event,
        tasks: list[asyncio.Task[None]],
        wait_dump: bool,
    ) -> None:
        if proc.poll() is None:
            self._stopping = True
            await asyncio.to_thread(launcher.stop, proc, float(self.run_cfg["quit_timeout_sec"]))
        elif wait_dump and "C-02" in self.rules:
            # 落ちた直後はダンプがまだ書かれていないことがあるので少し待つ
            await asyncio.sleep(float(self.rules["C-02"].param("wait_after_exit_sec")))
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- ラン全体 ---

    async def run(self, opts: RunOptions) -> RunResult:
        run_dir = new_run_dir(self.runs_dir, self.build.name)
        self._bus = EventBus(run_dir / "events.jsonl")
        self._bus.subscribe(self._on_event)
        self._started = time.monotonic()
        started_wall = now_wall()
        seed = str(random.randrange(2**31)) if opts.seed == "auto" else opts.seed
        if opts.mode != "aging":
            self.notes.append(f"モード {opts.mode} の操作はフェーズ2以降で実装予定。今回は観測のみ（aging 相当）")
        if self.engine.unimplemented:
            self.notes.append(f"未実装のため評価していないルール: {', '.join(self.engine.unimplemented)}")

        saves = SaveState(self.runs_dir, self.platform)
        interrupted = False
        backed_up = False
        # SIGTERM（kill や launchd の停止）も Ctrl+C と同じ終了処理に流す。
        # 既定動作のまま死ぬと、別セッションで起動したゲームが残りセーブも戻らない
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
        assert main_task is not None
        loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
        try:
            if saves.pending():
                self.notes += ["前回のランの未復元の退避を見つけたため復元しました"] + saves.restore()
            self.notes += saves.backup(run_dir, resolve_target(self.build, self.platform), self.build.initial_save)
            backed_up = True
            deadline = self._started + opts.hours * 3600
            launch = 0
            max_restarts = int(self.run_cfg["max_restarts"])
            while time.monotonic() < deadline:
                reason = await self._session(launch, deadline, run_dir)
                if reason != "restart":
                    break
                if self.restarts >= max_restarts:
                    self.notes.append(f"再起動回数が上限（{max_restarts}）に達したため打ち切りました")
                    break
                self.restarts += 1
                launch += 1
        except asyncio.CancelledError:
            interrupted = True
            self.notes.append("中断の指示（Ctrl+C / SIGTERM）を受けて終了しました")
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            if backed_up:
                try:
                    self.notes += saves.restore()
                except Exception as e:  # 復元の失敗は必ずレポートに残す
                    self.notes.append(f"セーブ・設定の復元に失敗しました: {e!r}（yomawari restore で再試行できます）")
            self._status(time.monotonic(), force=True)
            self._bus.close()
            summary = RunSummary(
                run_id=run_dir.name,
                build_name=self.build.name,
                build_path=str(self.build.path),
                started=started_wall,
                ended=now_wall(),
                duration_sec=time.monotonic() - self._started,
                mode=opts.mode,
                seed=seed,
                launches=self.launches,
                restarts=self.restarts,
                interrupted=interrupted,
                rules_path=str(self.rules_path),
                env={
                    "yomawari": __version__,
                    "Python": f"{sys.version.split()[0]} ({pyplatform.machine()})",
                    "OS": f"{self.platform.name} {pyplatform.mac_ver()[0] or pyplatform.release()}",
                },
                harness_notes=self.notes,
            )
            report = write_report(summary, self.issues, run_dir)
        return RunResult(run_dir, report, self.issues, interrupted)
