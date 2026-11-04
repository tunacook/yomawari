"""ラン・ライフサイクル: 準備 → 起動 → 監視 → （クラッシュ時は再起動して継続）→ 終了処理 → 後処理。

フェーズ1ではハーネス本体と同じプロセス内で動かす。別プロセス化はフェーズ6で行う。
fuzz / random モードでは、起動ごとに FuzzSession（fuzz/loop.py）を動かしてゲームを操作する。aging は観測のみ。
"""

from __future__ import annotations

import asyncio
import os
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
from .detect.engine import Engine, Rule, RuleError
from .events import Event, Issue, now_wall
from .fuzz.decider import RandomDecider
from .fuzz.advisor import ChoiceAdvisor
from .fuzz.experience import Experience
from .fuzz.flow import FlowTracker
from .fuzz.guard import Guard
from .fuzz.loop import FuzzRun, FuzzSession
from .fuzz.looks import load_inactive_words
from .fuzz.roles import RoleBook
from .input.base import InputDriver
from .input.pointer import Pointer
from .platform import Platform
from .profile import GameSpec
from .post.report import RunSummary, write_report
from .purpose import Purpose
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
    purpose: Purpose | None = None  # ランの目的。fuzz_cfg にはすでに重ねてある


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
        fuzz_cfg: dict[str, Any] | None = None,
        spec: GameSpec | None = None,
        driver_factory: Callable[[], InputDriver] | None = None,
        roles: RoleBook | None = None,
        judge_cfg: dict[str, Any] | None = None,
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
        self.fuzz_cfg = fuzz_cfg or {}
        self.spec = spec or GameSpec()
        self.judge_cfg = judge_cfg  # [judge]（Ollama）。None なら AI に聞かない
        self.driver_factory = driver_factory
        self.roles = roles or RoleBook()
        self.engine = Engine(rules)
        self.state = GameState()
        self.issues: list[Issue] = []
        self.notes: list[str] = []
        self.launches = 0
        self.restarts = 0
        self._stopping = False
        self._expect_exit = False
        self._exit_after_op = False
        self._restart_req: asyncio.Event | None = None
        self._fuzz: FuzzRun | None = None
        self._session_fuzz: FuzzSession | None = None
        self._driver: InputDriver | None = None
        self._fuzz_task: asyncio.Task[None] | None = None
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
        if self._session_fuzz is not None:
            self._session_fuzz.on_event(ev)
        if ev.kind == "boot" and ev.source == "game":
            d = ev.data
            self.roles.build = "/".join(str(d[k]) for k in ("version", "build_guid") if d.get(k)) or None
        elif ev.kind == "proc_exit" and ev.data["code"] == 0 and ev.data.get("after_op"):
            # 押した直後に正常に終わった。ハーネスが止めたのでなければ、終了を期待する操作と同じく起動し直して続ける
            self.roles.confirm(str(ev.data["after_op"]), "exit", "終了コード 0 で終わった")
            if not ev.data["by_harness"]:
                self._exit_after_op = True
        if ev.kind == "external":
            d = ev.data
            self.notes.append(
                f"外部を開く操作 {d['target']}（起動 #{ev.launch}）: 前面のアプリが"
                f"{'切り替わった' if d['front_changed'] else '切り替わらなかった'}。ゲームを前面に"
                f"{'戻せた' if d['back'] else '戻せなかった'}（開いた先の中身は人が確認する）"
            )
        elif ev.kind == "op_stopped":
            self.notes.append(f"操作を止めました（起動 #{ev.launch}、{ev.data['label']}）: {ev.data['error']}。以降は観測のみ")
        if ev.kind == "proc_exit" and self._restart_req is not None:
            if not ev.data["by_harness"] or self._expect_exit:
                self._restart_req.set()

    def _handle_issues(self, issues: list[Issue]) -> None:
        for i in issues:
            self.issues.append(i)
            self._publish(Event("harness", "issue", i.to_json(), i.launch))
            if i.action == "restart" and self._restart_req is not None:
                req = self._restart_req
                # ゲームは動いているが進まない（S-02 / S-03）なら、再起動する前に抜け出す操作を試す
                if self._session_fuzz is not None and self._session_fuzz.launch == i.launch and \
                        self._session_fuzz.recover(i.rule_id, req.set):
                    continue
                req.set()

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
                "ops": (self._fuzz.stats["normal"] + self._fuzz.stats["irregular"]) if self._fuzz else None,
                "blocked": self._fuzz.blocked_since[0] if self._fuzz and self._fuzz.blocked_since else None,
            }
        )

    def _keep_awake(self) -> subprocess.Popen[bytes] | None:
        """ランのあいだ、画面とシステムのスリープを防ぐ。画面が消える・ロックされると、OS がゲームへの入力を受け付けない。"""
        keep = getattr(self.platform, "keep_awake", None)
        proc = keep(os.getpid()) if keep is not None else None
        if proc is None:
            self.notes.append("画面とシステムのスリープを防げませんでした。画面が消える・ロックされると操作できません")
        return proc

    # --- 1回の起動 ---

    def _recent_press(self) -> str | None:
        # ハーネス自身が止めたとき（_stopping）は、直前に押したボタンのせいにしない
        if self._session_fuzz is None or self._stopping:
            return None
        return self._session_fuzz.recent_press()

    def _on_expect_exit(self, expecting: bool) -> None:
        # 終了を期待するボタンを押す。プロセスの終了はハーネスの終了操作として扱う（C-01 にしない）。
        # ハーネス自身の停止（_stopping）とは分けて持つ（押すのに失敗して取り消すときに、停止中の印まで消さないように）
        self._expect_exit = expecting

    async def _session(self, launch: int, deadline: float, run_dir: Path) -> str:
        """ゲームを1回起動して監視する。戻り値は "restart"、"relaunch"（終了を期待する操作で終わった）か "deadline"。"""
        log_path = run_dir / ("player.log" if launch == 0 else f"player.{launch}.log")
        self._stopping = False
        self._expect_exit = False
        self._exit_after_op = False
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
                    lambda: self._stopping or self._expect_exit,
                    self._recent_press,
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
        if self._fuzz is not None and self._driver is not None:
            pointer = Pointer(self.platform, self._driver, proc.pid, self.state)
            self._session_fuzz = FuzzSession(self._fuzz, self.state, pointer, self._publish, launch, self._on_expect_exit)
            self._fuzz_task = asyncio.create_task(self._session_fuzz.loop(stop))
            tasks.append(self._fuzz_task)
        reason = "deadline"
        cancelled = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._restart_req.wait(), timeout=remaining)
                ended = proc.poll() is not None
                reason = "relaunch" if (self._expect_exit or self._exit_after_op) and ended else "restart"
            except TimeoutError:
                pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._session_fuzz = None
            await self._end_session(proc, stop, tasks, wait_dump=not cancelled and reason == "restart")
        return reason

    async def _end_session(
        self,
        proc: subprocess.Popen[bytes],
        stop: asyncio.Event,
        tasks: list[asyncio.Task[None]],
        wait_dump: bool,
    ) -> None:
        if self._fuzz_task is not None:
            # ゲームを止める前に操作をやめる（止めている最中に押さないように）
            self._fuzz_task.cancel()
            self._fuzz_task = None
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
            if "S-01" not in self.rules:
                raise RuleError(f"モード {opts.mode} には rules.yaml の S-01（応答を待つ時間）が必要です")
            self._fuzz = FuzzRun(
                mode=opts.mode,
                decider=RandomDecider(random.Random(seed), float(self.fuzz_cfg["no_response_factor"])),
                guard=Guard(self.spec.guard_buttons, self.spec.guard_roles, self.roles),
                rng=random.Random(f"{seed}:trigger"),
                cfg=self.fuzz_cfg,
                response_timeout=float(self.rules["S-01"].param("timeout_sec")),
                screen_weights=self.spec.screen_weights,
                flow=FlowTracker(self.spec.flows),
                inactive_words=load_inactive_words(self.rules_path),
                # 進め方の経験は、ボタンの役割と同じファイル（runs/_experience/<ゲーム>.json）に覚える
                experience=Experience(self.roles.path),
            )
            if self.fuzz_cfg["ai_choice"] and self.fuzz_cfg["use_flow"] and self.spec.flows and self.judge_cfg is not None:
                # 流れの手順で選ぶものを、本文の文脈から AI に点数を付けさせる（fuzz/advisor.py）
                self._fuzz.advisor = ChoiceAdvisor.from_judge(self.judge_cfg, float(self.fuzz_cfg["ai_timeout_sec"]))
            if opts.mode == "random" and self.fuzz_cfg["learn"]:
                # 進行につながった操作を優先し、巻き戻した操作を後回しにする。fuzz モードでは掛けない（いつもの道に偏らないように）
                exp, fc = self._fuzz.experience, self.fuzz_cfg
                run = self._fuzz
                # 経験は、目的が「本編を進める」のプレイでだけ使う（「画面を見て回る」では寄り道や巻き戻しも試す）
                self._fuzz.decider.learned = lambda key, op: exp.factor(  # type: ignore[attr-defined]
                    key, op.label, float(fc["progress_bonus"]), float(fc["rewind_factor"])
                ) if run.intent == "progress" else 1.0
            if self.driver_factory is None:
                from .input.mouse_kb import PyAutoGuiDriver

                self.driver_factory = PyAutoGuiDriver
            self._driver = self.driver_factory()
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
        awake = self._keep_awake()
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
                if reason == "deadline":
                    break
                if reason == "relaunch":
                    # 終了を期待する操作で終わった。再起動の上限には数えない
                    launch += 1
                    continue
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
            if awake is not None:
                awake.terminate()
            if backed_up:
                try:
                    self.notes += saves.restore()
                except Exception as e:  # 復元の失敗は必ずレポートに残す
                    self.notes.append(f"セーブ・設定の復元に失敗しました: {e!r}（yomawari restore で再試行できます）")
            if self._fuzz is not None:
                self.notes += self._fuzz.notes
                self.notes += self._fuzz.experience.new
                self.notes += self._fuzz.flow.summary()
                self.notes.append(self._fuzz.summary())
                self.notes += self._fuzz.blocked_notes(time.time())
                if self._fuzz.advisor is not None:
                    self.notes += self._fuzz.advisor.summary()
                    self._fuzz.advisor.close()
                try:
                    self._fuzz.experience.save()
                except OSError as e:
                    self.notes.append(f"進め方の経験を保存できませんでした: {self._fuzz.experience.path} ({e})")
            self.notes += self.roles.new
            try:
                self.roles.save()
            except OSError as e:
                self.notes.append(f"ボタンの役割を保存できませんでした: {self.roles.path} ({e})")
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
                purpose=opts.purpose.name if opts.purpose else None,
                purpose_description=opts.purpose.description if opts.purpose else "",
                focus=list(opts.purpose.focus) if opts.purpose else [],
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
                silent_buttons=self.engine.silent_buttons(),
            )
            report = write_report(summary, self.issues, run_dir)
        return RunResult(run_dir, report, self.issues, interrupted)
