"""ラン・ライフサイクル: 準備 → 起動 → 監視 → （クラッシュ時は再起動して継続）→ 終了処理 → 後処理。

フェーズ1ではハーネス本体と同じプロセス内で動かす。別プロセス化はフェーズ6で行う。
fuzz / random モードでは、起動ごとに FuzzSession（fuzz/loop.py）を動かしてゲームを操作する。aging は観測のみ。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import platform as pyplatform
import random
import signal
import subprocess
import sys
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, launcher, snapshot
from .bus import EventBus
from .config import FUZZ_DEFAULTS, BuildConfig
from .detect.engine import Engine, Rule, RuleError
from .detect.perf import load_history, load_perf
from .judge import ollama
from .events import Event, Issue, now_wall
from .fuzz.decider import RandomDecider
from .fuzz.advisor import ChoiceAdvisor
from .fuzz.settings_tour import SCREEN_KEYS, SettingsTour, VolumeHold
from .fuzz.experience import Experience
from .fuzz.flow import FlowTracker
from .fuzz.guard import Guard
from .fuzz.keyboard import InputStyle
from .fuzz.loop import FuzzRun, FuzzSession
from .fuzz.looks import load_inactive_words, load_masked_chars
from .fuzz.resume import load_save_hints
from .post.rebuild import save_summary
from .fuzz.roles import RoleBook
from .input.base import InputDriver
from .input.pointer import Pointer
from .platform import Platform
from .profile import GameSpec
from .post.report import RunSummary, write_report
from .purpose import Purpose
from .savestate import SaveState, SaveTarget, learned_save_dir, previous_run_save, remember_save_dir, resolve_target
from .screen import Screen
from .sensors.savefile import SaveFileWatch
from .state import GameState
from .sensors.crash import watch_crash_dir
from .sensors.logtail import InvalidRecord, Record, UnityEntry, YomawariRecord, tail_file
from .sensors.process import watch_process
from .supervisor import child_state_path

StatusCallback = Callable[[dict[str, Any]], None]


_LOCK_WORDS = {"immediate": "すぐ", "off": "ロックしない"}


@dataclass
class RunOptions:
    hours: float
    mode: str
    seed: str
    purpose: Purpose | None = None  # ランの目的。fuzz_cfg にはすでに重ねてある
    display_off: bool = False  # ゲームが起動したあと、画面を消す
    display_off_delay_sec: float = 10.0
    continue_previous: bool = False  # ランの最初の起動から、前のランのセーブの続きを遊ぶ
    screen: Screen | None = None  # ランの画面の前提（--screen）。決めた大きさのウィンドウで起動し、ランのあいだは変えさせない


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


def _optional(platform: Platform, name: str) -> Callable[..., Any] | None:
    """OS によってない処理（Windows のパフォーマンスの計測など）は None。"""
    try:
        return getattr(platform, name, None)
    except NotImplementedError:
        return None


# 再起動の前に残す状況（snapshot.py）に入れる、直前のイベントの数
SNAPSHOT_EVENTS = 80
# 直前のイベントに入れない種類（毎秒来るもの）
_SNAPSHOT_SKIP = ("heartbeat", "proc_stats", "issue")


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
        self.on_run_dir: Callable[[Path], None] | None = None  # ランのディレクトリを作ったときに呼ぶ
        self.roles = roles or RoleBook()
        self.engine = Engine(rules, load_perf(rules_path))
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
        self._save_watch: SaveFileWatch | None = None  # F-01: セーブしたらファイルが更新されたかを見る
        self._save_target: SaveTarget | None = None  # 退避したセーブの場所と PlayerPrefs のドメイン
        # ゲームが boot で教えたセーブの場所が、退避した場所と違った。セッションを終え、退避し直してから起動し直す
        self._save_moved: Path | None = None
        self._first_launch = 0  # ランの最初の起動とみなす起動（セーブの場所が違って起動し直したら、その起動）
        self._save_resolved = False
        self._run_dir: Path | None = None
        # 再起動の前に残す状況のための、直前のイベント（ハートビートなど毎秒来るものは除く）
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=int(SNAPSHOT_EVENTS))  # 退避し直した（1 ランで 1 回だけ。場所が変わり続けても起動し直し続けない）
        self._screen: Screen | None = None  # ランの画面の前提（--screen）
        self._screen_seen: set[int] = set()  # 画面の大きさを一度でも見た起動
        self._screen_abort = ""  # 決めた画面で起動できなかった（ランを止める理由）
        self._fuzz_error = ""  # 操作のループが例外で止まった（ランを止める理由）
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
        if ev.kind not in _SNAPSHOT_SKIP:
            self._recent_events.append({"wall": ev.wall, "source": ev.source, "kind": ev.kind, "data": ev.data,
                                        "launch": ev.launch})
        self.state.feed(ev)
        self._handle_issues(self.engine.feed(ev))
        if self._session_fuzz is not None:
            self._session_fuzz.on_event(ev)
        if self._save_watch is not None:
            self._save_watch.on_event(ev)
        if ev.kind == "resolution" and ev.source == "game" and self._screen is not None:
            self._check_screen(ev)
        if ev.kind == "boot" and ev.source == "game":
            d = ev.data
            self.roles.build = "/".join(str(d[k]) for k in ("version", "build_guid") if d.get(k)) or None
            self._check_save_dir(d.get("data_path"))
        elif ev.kind == "proc_exit" and ev.data["code"] == 0 and ev.data.get("after_op"):
            # 押した直後に正常に終わった
            key = str(ev.data["after_op"])
            if ev.data["by_harness"]:
                # 終わると見込んで押した（終了の推定を確かめた）とおりに終わった
                self.roles.confirm(key, "exit", "終了コード 0 で終わった")
            else:
                # 見込んでいない終了は、人がゲームを閉じたのかもしれない。1回では確定せず、
                # 前に押して終わらなかったボタンなら終了ボタンとはみなさない。終了を期待する操作と同じく起動し直して続ける
                self._exit_after_op = True
                if self._fuzz is not None and key in self._fuzz.survived:
                    self.notes.append(
                        f"`{key}` を押した直後にゲームが終了コード 0 で終わったが、前に押したときは終わらなかったので、"
                        f"終了ボタンとはみなさなかった（起動 #{ev.launch}。人がゲームを閉じたのかもしれない）"
                    )
                else:
                    self.roles.suspect(key, "exit", "終了コード 0 で終わった")
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

    def _check_screen(self, ev: Event) -> None:
        """--screen: ゲームの画面の大きさ・画面モードが決めたものと違えば、起動し直す。起動したときから違えば、
        起動し直しても同じなので、ランを止める（画面より大きいウィンドウは開けない、など）。"""
        assert self._screen is not None
        d = ev.data
        first = ev.launch not in self._screen_seen
        self._screen_seen.add(ev.launch)
        if self._screen.matches(d.get("w"), d.get("h"), d.get("mode")) or self._restart_req is None:
            return
        got = f"{d.get('w')}x{d.get('h')}（{d.get('mode')}）"
        if first:
            self._screen_abort = (f"決めた画面（{self._screen}）で起動できなかったため、ランを止めました（起動 #{ev.launch}。"
                                  f"ゲームの画面は {got}。ディスプレイより大きいと開けないことがあります）")
        else:
            self.notes.append(f"ランのあいだに画面が {got}に変わったため、決めた画面（{self._screen}）で起動し直しました"
                              f"（起動 #{ev.launch}）")
        self._restart_req.set()

    def _take_snapshot(self, i: Issue) -> None:
        """再起動する検出の前に、画面の写しと状態をランのフォルダに残し、検出の証拠に場所を書く（snapshot.py）。
        抜け出す操作を試す前に撮る（試すと画面が変わる）。撮れなくても検出は続ける。
        rules.yaml の snapshot が before_press のルール（V-07）は、押したあとの画面ではなく、押す前に見た目を測ったときの
        画面を残す（押すと別の画面に変わっていて、押せないように見えたボタンが写らない）。"""
        if self._run_dir is None:
            return
        shot = None
        fuzz = self._session_fuzz
        rule = self.rules.get(i.rule_id)
        before = rule is not None and rule.params.get("snapshot") == "before_press"
        if fuzz is not None and fuzz.launch == i.launch and before:
            shot = fuzz.look_shot
            i.evidence["snapshot_before_press"] = True
        elif fuzz is not None and fuzz.launch == i.launch:
            try:
                shot = self.platform.capture(fuzz.pointer.area().rect)
            except Exception as e:  # ウィンドウが見えないなど。状態だけ残す
                i.evidence["snapshot_error"] = repr(e)
        name = f"{i.wall[11:19].replace(':', '')}_{i.rule_id}_{i.launch}"
        info = {"rule_id": i.rule_id, "title": i.title, "detail": i.detail, "wall": i.wall, "launch": i.launch,
                "recent_ops": i.evidence.get("recent"), "recent_events": list(self._recent_events)}
        try:
            i.evidence["snapshot"] = snapshot.take(self._run_dir, name, info, self.state, shot)
        except Exception as e:
            self.notes.append(f"{i.rule_id} の再起動の前に状況を残せませんでした: {e!r}（起動 #{i.launch}）")

    def _handle_issues(self, issues: list[Issue]) -> None:
        for i in issues:
            rule = self.rules.get(i.rule_id)
            if i.action == "restart" or (rule is not None and rule.params.get("snapshot")):
                self._take_snapshot(i)
            if self._fuzz is not None and self._fuzz.burst is not None:
                # 区間（ガチャプレイ・ボタンの網羅）の間に見つけたもの
                spot = self._fuzz.burst[0]
                i.detail += f"（区間 {self._fuzz.burst_plan.mode if self._fuzz.burst_plan else ''} の間: {spot}）"
                i.evidence["burst"] = spot
            self.issues.append(i)
            self._publish(Event("harness", "issue", i.to_json(), i.launch))
            if i.action == "restart" and self._restart_req is not None:
                req = self._restart_req
                # ゲームは動いているが進まない（S-02 / S-03 / S-06）なら、再起動する前に抜け出す操作を試す
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

    def _save_summary(self, summary: RunSummary, run_dir: Path) -> None:
        try:
            save_summary(summary, run_dir)
        except OSError as e:
            self.notes.append(f"ランの情報を保存できませんでした（yomawari report で作り直せません）: {e}")

    def _report_cfg(self) -> dict[str, Any]:
        """レポートの設定（[fuzz] の report_* と stall_report_sec）。操作しないモード（aging）では、進めなかった区間を出さない
        （操作しないので、進まないのが正常）。"""
        cfg = {k: v for k, v in self.fuzz_cfg.items() if k.startswith("report_") or k == "stall_report_sec"}
        if self._fuzz is None:
            cfg["stall_report_sec"] = 0.0
        return cfg

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

    async def _display_off(self, delay: float) -> None:
        """ゲームが起動して delay 秒たったら、画面を消す（夜間に画面を点けたままにしないため）。

        macOS は、画面が消えたあとロックされると、ゲームへの入力を受け付けない（ロックまでの時間はシステム設定しだい）。
        そうなったら「ハーネスの記録」の「操作できなかった」に出る。"""
        while not self.state.booted:
            await asyncio.sleep(0.5)
        await asyncio.sleep(delay)
        off = getattr(self.platform, "display_off", None)
        if off is not None and await asyncio.to_thread(off):
            lock = getattr(self.platform, "screen_lock_delay", None)
            when = await asyncio.to_thread(lock) if lock is not None else None
            self.notes.append(f"ゲームが起動して {delay:.0f} 秒後に、画面を消した（画面が消えてからロックされるまで: "
                              f"{_LOCK_WORDS.get(when or '', when or '不明')}）")
            self._publish(Event("harness", "display_off", {"lock": when}, max(self.launches - 1, 0)))
        else:
            self.notes.append("画面を消せなかった（この OS では未対応、または pmset が失敗した）")

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

    def _check_save_dir(self, data_path: object) -> None:
        """ゲームが boot で教えたセーブの場所（persistentDataPath）を覚え、退避した場所と違えば、セーブが書かれる前に
        セッションを終える（ランの続きで、退避し直してから起動し直す）。設定（builds.<name>.save_dir）で決めたビルドは変えない。"""
        if not isinstance(data_path, str) or not data_path or self._save_target is None:
            return
        reported = Path(data_path)
        remember_save_dir(self.runs_dir, self.build.name, reported)
        if reported == self._save_target.save_dir:
            return
        if self.build.save_dir is not None:
            note = (f"ゲームが教えたセーブの場所（{reported}）が、設定（builds.{self.build.name}.save_dir）の"
                    f"{self.build.save_dir} と違う。設定の場所を退避して見ている")
            if note not in self.notes:
                self.notes.append(note)
            return
        if self._save_resolved:
            note = (f"ゲームが教えたセーブの場所（{reported}）が、退避し直した場所（{self._save_target.save_dir}）とも違った。"
                    f"起動し直し続けないように、このランではこれ以上退避し直さない")
            if note not in self.notes:
                self.notes.append(note)
            return
        if self._save_moved is None and self._restart_req is not None:
            self._save_moved = reported
            self._restart_req.set()

    async def _session(self, launch: int, deadline: float, run_dir: Path) -> str:
        """ゲームを1回起動して監視する。戻り値は "restart"、"relaunch"（終了を期待する操作で終わった）、"save_dir"（セーブの
        場所が退避した場所と違った）か "deadline"。"""
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
                    perf_stats=_optional(self.platform, "perf_stats"),
                    ai_busy_since=ollama.busy_since,
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
            pointer = Pointer(self.platform, self._driver, proc.pid, self.state,
                              fit_wait=float(self.fuzz_cfg["window_fit_wait_sec"]),
                              fit_tries=int(self.fuzz_cfg["window_fit_tries"]))
            self._session_fuzz = FuzzSession(self._fuzz, self.state, pointer, self._publish, launch, self._on_expect_exit,
                                             first=launch == self._first_launch)
            self._fuzz_task = asyncio.create_task(self._session_fuzz.loop(stop))
            self._fuzz_task.add_done_callback(lambda task: self._on_fuzz_done(task, launch))
            tasks.append(self._fuzz_task)
        reason = "deadline"
        cancelled = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._restart_req.wait(), timeout=remaining)
                ended = proc.poll() is not None
                reason = "relaunch" if (self._expect_exit or self._exit_after_op) and ended else "restart"
                if self._save_moved is not None:
                    reason = "save_dir"
            except TimeoutError:
                pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._session_fuzz = None
            await self._end_session(proc, stop, tasks, wait_dump=not cancelled and reason == "restart")
        return reason

    def _on_fuzz_done(self, task: asyncio.Task[None], launch: int) -> None:
        """操作のループが例外で終わったら、記録してランを止める。

        ゲームは動いたままなので、止めずにおくとハーネスは何も押さないまま待ち、S-02・Y-01 で再起動を繰り返す。
        ハーネスのコードの不具合は起動し直しても同じところで起きることが多いので、再起動せずにランを止める
        （run 2026-11-27: 見た目の測定の例外で、再起動の上限まで同じことを繰り返し、原因はレポートに残らなかった）。"""
        if task.cancelled() or task.exception() is None:
            return
        exc = task.exception()
        assert exc is not None
        frames = traceback.extract_tb(exc.__traceback__)
        where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "不明"
        error = f"{type(exc).__name__}: {exc}"
        self._publish(Event("harness", "harness_error", {
            "where": "fuzz", "error": error, "at": where,
            "traceback": "".join(traceback.format_exception(exc)),
        }, launch))
        self._fuzz_error = (f"ハーネスの操作のループが例外で止まったため、ランを止めました（起動 #{launch}、{where}、{error}）。"
                            "yomawari の不具合なので、events.jsonl の harness_error のスタックトレースを添えて yomawari 側に渡す")
        if self._restart_req is not None:
            self._restart_req.set()

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
        self._run_dir = run_dir
        if self.on_run_dir is not None:
            self.on_run_dir(run_dir)  # 見張り役に、どのランを見張るかを知らせる（supervisor.py）
        # --continue: 前のランが終わったときのセーブを置いて、その続きから遊ぶ（DESIGN.md §9「続きから遊ぶ」）。
        # 見つからなければ、はじめから遊ぶ（そのときにあるのは人のセーブで、前のランの続きではない）
        prev_save = previous_run_save(self.runs_dir, self.build.name, run_dir) if opts.continue_previous else None
        if opts.continue_previous and prev_save is None:
            self.notes.append("--continue: 前のランのセーブが見つからないため、はじめから遊びました")
        machine = ""
        if self.engine.perf is not None:
            # パフォーマンスを、同じビルド名・同じマシンの前回までのランと比べる（R-10）
            get_machine = _optional(self.platform, "machine")
            machine = get_machine() if get_machine is not None else ""
            self.engine.perf.history = load_history(self.runs_dir, self.build.name, machine, run_dir,
                                                    self.engine.perf.history_runs)
        self._bus = EventBus(run_dir / "events.jsonl")
        self._bus.subscribe(self._on_event)
        self._started = time.monotonic()
        started_wall = now_wall()
        seed = str(random.randrange(2**31)) if opts.seed == "auto" else opts.seed
        if opts.screen is not None:
            # ビルドの args の画面の指定を置き換えて起動する（DESIGN.md §9「画面の前提」）
            self._screen = opts.screen
            self.build = dataclasses.replace(self.build, args=opts.screen.args(self.build.args))
            self.notes.append(f"画面を「{opts.screen}」に決めて起動した（--screen・--fullscreen）。ランのあいだは、解像度・画面モードを変える"
                              "ボタンを押さず、設定を変えに行くときも変えない")
        if opts.mode != "aging":
            if "S-01" not in self.rules:
                raise RuleError(f"モード {opts.mode} には rules.yaml の S-01（応答を待つ時間）が必要です")
            self._fuzz = FuzzRun(
                mode=opts.mode,
                decider=RandomDecider(random.Random(seed), float(self.fuzz_cfg["no_response_factor"])),
                # --screen では、画面モード・解像度などを変えるボタン（役割 system_setting）を押さない
                guard=Guard(self.spec.guard_buttons,
                            opts.screen.guard_roles(self.spec.guard_roles) if opts.screen else self.spec.guard_roles,
                            self.roles),
                rng=random.Random(f"{seed}:trigger"),
                cfg=self.fuzz_cfg,
                response_timeout=float(self.rules["S-01"].param("timeout_sec")),
                response_checks=int(self.rules["S-01"].params.get("checks", 1)),
                response_min_hits=int(self.rules["S-01"].params.get("probe_min_hits", 1)),
                run_sec=opts.hours * 3600,
                screen_weights=self.spec.screen_weights,
                silent_spec=set(self.spec.silent_buttons),
                flow=FlowTracker(self.spec.flows),
                locale=self.spec.locale,
                inactive_words=load_inactive_words(self.rules_path),
                masked_chars=load_masked_chars(self.rules_path),
                save_hints=load_save_hints(self.rules_path),
                continue_previous=prev_save is not None,
                # 進め方の経験は、ボタンの役割と同じファイル（runs/_experience/<ゲーム>.json）に覚える
                experience=Experience(self.roles.path),
                tour=SettingsTour(float(self.fuzz_cfg["settings_tour_interval_sec"]),
                                  float(self.fuzz_cfg["settings_tour_timeout_sec"]),
                                  float(self.fuzz_cfg["settings_tour_factor"]),
                                  offered=(frozenset(self.spec.settings_offered)
                                           if self.spec.settings_offered is not None else None),
                                  fixed=SCREEN_KEYS if opts.screen else frozenset()),
            )
            self._fuzz.input_style = InputStyle.from_cfg(self.fuzz_cfg)
            if "A-03" in self.rules:
                a03 = self.rules["A-03"]
                window = float(a03.param("window_sec"))
                self._fuzz.volume_hold = VolumeHold(before=window, after=float(a03.param("settle_sec")) + window)
            # ゲームの流れで外れ・正解と分かった組み合わせを、ランをまたいで覚える
            self._fuzz.flow.remember(self._fuzz.experience.flow_wrong, self._fuzz.experience.flow_right)
            if self.fuzz_cfg["ai_choice"] and self.fuzz_cfg["use_flow"] and self.spec.flows and self.judge_cfg is not None:
                # 流れの手順で選ぶものを、本文の文脈から AI に点数を付けさせる（fuzz/advisor.py）
                self._fuzz.advisor = ChoiceAdvisor.from_judge(self.judge_cfg, float(self.fuzz_cfg["ai_timeout_sec"]))
            burst = opts.purpose.burst if opts.purpose is not None else None
            if burst is not None and opts.mode != "aging":
                # 通常のプレイの途中で、初めて来た画面・scene でときどき区間（ガチャプレイ・ボタンの網羅）に切り替える
                self._fuzz.burst_plan = burst
                self._fuzz.burst_cfg = burst.apply(self.fuzz_cfg)
            if opts.mode == "random" and self.fuzz_cfg["learn"]:
                # 進行につながった操作を優先し、巻き戻した操作を後回しにする。fuzz モードでは掛けない（いつもの道に偏らないように）
                exp = self._fuzz.experience
                run = self._fuzz
                # 経験は、目的が「本編を進める」のプレイでだけ使う（「画面を見て回る」では寄り道や巻き戻しも試す）。
                # 区間（ガチャプレイなど）の間は、区間の設定の learn に従う
                self._fuzz.decider.learned = lambda key, op: exp.factor(  # type: ignore[attr-defined]
                    key, op.label, float(run.cfg["progress_bonus"]), float(run.cfg["rewind_factor"])
                ) if run.intent == "progress" and run.mode == "random" and run.cfg["learn"] else 1.0
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
        # 既定動作のまま死ぬと、別セッションで起動したゲームが残りセーブも戻らない。
        # 2 回目以降は受け流す。終了処理を途中で打ち切ると、レポートが出ないまま終わる
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
        assert main_task is not None
        stopping = False
        told_waiting = False
        stop_reason = ""

        def request_stop(reason: str) -> None:
            nonlocal stopping, told_waiting, stop_reason
            if not stopping:
                stopping = True
                stop_reason = reason
                main_task.cancel()
            elif not told_waiting:
                told_waiting = True
                after = ("見張り役がセーブを戻し、記録からレポートを作り直します" if child_state_path() is not None
                         else "レポートは出ません。セーブは yomawari restore で戻せます")
                print("終了処理中です（ゲームの終了・セーブの復元・レポートの出力）。待てないときは Ctrl+\\ で強制終了できます"
                      f"（{after}）", file=sys.stderr)

        loop.add_signal_handler(signal.SIGINT, request_stop, "手動で中断した（Ctrl+C）")
        loop.add_signal_handler(signal.SIGTERM, request_stop, "外から止められた（SIGTERM。kill や launchd の停止など）")
        base = dict(
            run_id=run_dir.name,
            build_name=self.build.name,
            build_path=str(self.build.path),
            started=started_wall,
            mode=opts.mode,
            seed=seed,
            purpose=opts.purpose.name if opts.purpose else None,
            purpose_description=opts.purpose.description if opts.purpose else "",
            focus=list(opts.purpose.focus) if opts.purpose else [],
            planned_hours=opts.hours,
            rules_path=str(self.rules_path),
            settings_offered=self.spec.settings_offered,
            env={
                "yomawari": __version__,
                "Python": f"{sys.version.split()[0]} ({pyplatform.machine()})",
                "OS": f"{self.platform.name} {pyplatform.mac_ver()[0] or pyplatform.release()}",
            },
            suspect_error_window_sec=float(self.fuzz_cfg.get(  # 操作しないモード（aging）では fuzz の設定がない
                "suspect_error_window_sec", FUZZ_DEFAULTS["suspect_error_window_sec"])),
        )
        # 終了処理まで進まなくても（強制終了など）、yomawari report で作り直せるように、始めにも書いておく
        self._save_summary(RunSummary(**base, ended="", duration_sec=0.0, launches=0, restarts=0, interrupted=False), run_dir)
        awake = self._keep_awake()
        dark = asyncio.create_task(self._display_off(opts.display_off_delay_sec)) if opts.display_off else None
        try:
            if saves.pending():
                self.notes += ["前回のランの未復元の退避を見つけたため復元しました"] + saves.restore()
            target = resolve_target(self.build, self.platform, learned_save_dir(self.runs_dir, self.build.name))

            def backup(target: SaveTarget, where: Path = run_dir) -> None:
                if prev_save is not None:
                    self.notes.extend(saves.backup(where, target, prev_save, "前のランが終わったときのセーブ（--continue）"))
                else:
                    self.notes.extend(saves.backup(where, target, self.build.initial_save))
                self._save_target = target
                if self._save_watch is not None:
                    self._save_watch.close()
                    self._save_watch = None
                if "F-01" in self.rules:
                    if target.save_dir is not None:
                        f01 = self.rules["F-01"]
                        self._save_watch = SaveFileWatch(target.save_dir, self._publish, float(f01.param("check_after_sec")),
                                                         float(f01.param("slack_sec")))
                    else:
                        self.notes.append("セーブの場所が分からないため、F-01（セーブしたのにファイルが更新されない）を"
                                          "評価していません（builds.<name>.save_dir で指定できます）")

            backup(target)
            backed_up = True
            deadline = self._started + opts.hours * 3600
            launch = 0
            max_restarts = int(self.run_cfg["max_restarts"])
            while time.monotonic() < deadline:
                reason = await self._session(launch, deadline, run_dir)
                if self._screen_abort:
                    self.notes.append(self._screen_abort)
                    interrupted, stop_reason = True, self._screen_abort
                    break
                if self._fuzz_error:
                    self.notes.append(self._fuzz_error)
                    interrupted, stop_reason = True, self._fuzz_error
                    break
                if reason == "deadline":
                    break
                if reason == "relaunch":
                    # 終了を期待する操作で終わった。再起動の上限には数えない
                    launch += 1
                    continue
                if reason == "save_dir" and self._save_moved is not None:
                    # ゲームが教えたセーブの場所が、退避した場所と違った。退避した場所を戻してから、教わった場所を退避し直し、
                    # 起動し直す（再起動の上限には数えない）。次のランからは、教わった場所を起動前に退避する
                    moved, self._save_moved = self._save_moved, None
                    self._save_resolved = True
                    old = self._save_target.save_dir if self._save_target is not None else None
                    self.notes.append(f"ゲームが教えたセーブの場所（{moved}）が、退避した場所（{old}）と違ったので、"
                                      f"ゲームを止めて退避し直し、起動し直した（起動 #{launch}）")
                    self.notes.extend(saves.restore())
                    prefs = self._save_target.prefs_domain if self._save_target is not None else None
                    # 最初の退避（run_dir/backup）とは別の場所に退避する（ラン後の状態も、それぞれの退避の下に残る）
                    backup(SaveTarget(moved, prefs), run_dir / "save_dir_moved")
                    launch += 1
                    self._first_launch = launch  # ゲームはまだ何も進めていないので、最初の起動と同じく遊ぶ
                    continue
                if self.restarts >= max_restarts:
                    # 予定の時間より前に終わるので、途中で止めたランとしてレポートの「終わり方」に書く
                    # （run 2026-11-27: 3 時間の予定が 0.9 時間で打ち切られたのに、まとめには「予定どおり終わった」と出た）
                    limit = f"再起動回数が上限（{max_restarts}）に達したため打ち切りました"
                    self.notes.append(limit)
                    interrupted, stop_reason = True, limit
                    break
                self.restarts += 1
                launch += 1
        except asyncio.CancelledError:
            interrupted = True
            self.notes.append(f"中断の指示を受けて終了しました: {stop_reason or '理由は不明'}")
        finally:
            stopping = True  # 時間どおりに終わるときも、ここからの中断の指示は受け流す
            if dark is not None:
                dark.cancel()
            if awake is not None:
                awake.terminate()
            if self._save_watch is not None:
                self._save_watch.close()
                self._save_watch = None
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
                self.notes += self._fuzz.burst_notes()
                self.notes += self._fuzz.edge_notes()
                self.notes += self._fuzz.settings_notes()
                self.notes += self._fuzz.looks_notes()
                self.notes += self._fuzz.input_style.notes()
                self.notes += self._fuzz.locale_notes(time.monotonic())
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
            perf_lines: list[str] = []
            if self.engine.perf is not None:
                self._handle_issues(self.engine.finish(time.monotonic(), now_wall()))
                perf = self.engine.perf
                try:
                    (run_dir / "perf.json").write_text(
                        json.dumps(perf.summary_json(self.build.name, machine, self.roles.build), ensure_ascii=False,
                                   indent=1), encoding="utf-8")
                except OSError as e:
                    self.notes.append(f"パフォーマンスの集計を保存できませんでした: {e}")
                note = perf.excluded_note()
                if note:
                    self.notes.append(note)
                perf_lines = perf.report_lines()
            self._status(time.monotonic(), force=True)
            self._bus.close()
            summary = RunSummary(
                **base,
                ended=now_wall(),
                duration_sec=time.monotonic() - self._started,
                launches=self.launches,
                restarts=self.restarts,
                interrupted=interrupted,
                stop_reason=stop_reason,
                harness_notes=self.notes,
                silent_buttons=self.engine.silent_buttons(),
                report_cfg=self._report_cfg(),
                perf=perf_lines,
            )
            self._save_summary(summary, run_dir)
            report = write_report(summary, self.issues, run_dir)
            loop.remove_signal_handler(signal.SIGINT)
            loop.remove_signal_handler(signal.SIGTERM)
        return RunResult(run_dir, report, self.issues, interrupted)
