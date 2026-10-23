"""ファジングのループ（DESIGN.md §9）。

1. 通常の操作を選んで押し、画面の状態（GameState.signature）が変わるまで待つ。結果を op_result として出す
2. fuzz モードでは、ゲームのイベント（暗転の開始、本文、選択肢、セーブなど）を受けてから 0〜trigger_delay_max_sec 後に
   イレギュラー操作を差し込む。時間ベースでも irregular_prob の確率で差し込む
3. イレギュラー操作の次の通常の操作は「応答確認」として扱う（反応がなければ S-01）。
   応答確認には、同じ状態で前に反応があった操作だけを使う（空のセーブスロットなど、普段から反応しないボタンで誤検知しないように）。
   そういう操作がなければ、その回は応答確認をしない

判定（S-01 / S-02 / S-05 / C-04）は detect/engine.py が op / op_result / exit_expected イベントから行う。
ここは操作と、反応があったかどうかの記録だけをする。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..events import Event
from ..input.pointer import Pointer, PointerError
from ..state import GameState
from .decider import Decider
from .guard import Guard
from .ops import Op, escape_candidates, irregular_candidates, normal_candidates, state_key

# イレギュラー操作を差し込むきっかけにするゲームのイベント
TRIGGERS = {"transition", "line", "choices", "save", "load", "scene"}


@dataclass
class FuzzRun:
    """ラン全体で持ち越すもの（試行回数、external の使用済み、統計）。起動し直しても引き継ぐ。"""

    mode: str  # fuzz | random
    decider: Decider
    guard: Guard
    rng: random.Random
    cfg: dict[str, Any]
    response_timeout: float
    started: float = field(default_factory=time.monotonic)
    last_exit_t: float | None = None
    stats: Counter[str] = field(default_factory=Counter)
    labels: set[str] = field(default_factory=set)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    responsive: set[tuple[str, str]] = field(default_factory=set)  # (状態, 操作) のうち、反応があったもの
    stopped_reason: str | None = None

    def summary(self) -> str:
        s = self.stats
        return (
            f"操作: 通常 {s['normal']} 回（反応なし {s['no_response']} 回）、イレギュラー {s['irregular']} 回、"
            f"終了を期待する操作 {s['expect_exit']} 回、外部を開く操作 {s['external']} 回、"
            f"押せなかった {s['error']} 回。押したボタン・選択肢 {len(self.labels)} 種類"
        )


class FuzzSession:
    """1回の起動ぶんの操作。"""

    def __init__(
        self,
        run: FuzzRun,
        state: GameState,
        pointer: Pointer,
        publish: Callable[[Event], None],
        launch: int,
        on_expect_exit: Callable[[bool], None],
    ) -> None:
        self.run = run
        self.state = state
        self.pointer = pointer
        self.publish = publish
        self.launch = launch
        self.on_expect_exit = on_expect_exit
        self.cfg = run.cfg
        self._lock = asyncio.Lock()
        self._verify_next = False
        self._irregular_t = -1.0
        self._sig: Any = None
        self._sig_t = 0.0
        self._modal_since: float | None = None
        self._trigger: asyncio.Task[None] | None = None
        self._exiting = False

    # --- イベント（バスから同期で呼ばれる） ---

    def on_event(self, ev: Event) -> None:
        if self.run.mode != "fuzz" or ev.source != "game" or ev.kind not in TRIGGERS or ev.launch != self.launch:
            return
        if ev.kind in ("transition", "save", "load") and ev.data.get("phase") != "start":
            return
        if self._trigger is not None and not self._trigger.done():
            return
        if self.run.rng.random() >= float(self.cfg["trigger_prob"]):
            return
        delay = self.run.rng.uniform(0, float(self.cfg["trigger_delay_max_sec"]))
        self._trigger = asyncio.get_running_loop().create_task(self._delayed_irregular(delay, ev.kind))

    async def _delayed_irregular(self, delay: float, trigger: str) -> None:
        await asyncio.sleep(delay)
        await self._irregular(trigger)

    # --- ループ ---

    async def loop(self, stop: asyncio.Event) -> None:
        interval = float(self.cfg["op_interval_sec"])
        try:
            while not stop.is_set() and not self._exiting and self.run.stopped_reason is None:
                await self._step()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
        finally:
            if self._trigger is not None:
                self._trigger.cancel()

    def _ready(self) -> bool:
        return self.state.booted and self.state.resolution is not None

    async def _step(self) -> None:
        if not self._ready():
            return
        now = time.monotonic()
        sig = self.state.signature()
        if sig != self._sig:
            # 画面が変わった直後は、アニメーションが落ち着くまで待つ
            self._sig, self._sig_t = sig, now
            return
        if now - self._sig_t < float(self.cfg["settle_sec"]):
            return
        if self.run.mode == "fuzz" and self.run.rng.random() < float(self.cfg["irregular_prob"]):
            await self._irregular("random")
            return

        allow_exit = now - (self.run.last_exit_t or self.run.started) >= float(self.cfg["exit_min_interval_sec"])
        cands = normal_candidates(self.state, self.run.guard, self.cfg["weights"], allow_exit)
        top = self.state.top_modal()
        if top is not None and not top.buttons and not self.state.busy:
            self._modal_since = self._modal_since or now
            if now - self._modal_since >= float(self.cfg["stuck_modal_sec"]):
                cands = escape_candidates()
        else:
            self._modal_since = None
        key = state_key(self.state)
        verify = False
        if self._verify_next:
            known = [op for op in cands if (key, op.label) in self.run.responsive]
            if known:
                cands, verify = known, True
        op = self.run.decider.choose(key, cands)
        if op is None:
            return  # 暗転中などで押せない。応答確認は次に押せるときまで持ち越す
        self._verify_next = False
        if op.kind == "expect_exit":
            await self._expect_exit(op)
        elif op.kind == "external":
            await self._external(op)
        else:
            await self._normal(op, key, verify)

    async def _normal(self, op: Op, key: str, verify: bool) -> None:
        before = self.state.signature()
        started = time.monotonic()
        if not await self._do(op):
            return
        timeout = self.run.response_timeout
        while time.monotonic() - started < timeout:
            if self.state.signature() != before:
                break
            await asyncio.sleep(0.05)
        waited = time.monotonic() - started
        responded = self.state.signature() != before
        self.run.stats["normal"] += 1
        self.run.decider.feedback(key, op, responded)
        if responded:
            self.run.responsive.add((key, op.label))
        else:
            self.run.stats["no_response"] += 1
        hb_age = time.monotonic() - self.state.last_game_t if self.state.last_game_t is not None else None
        self.publish(
            Event(
                "harness", "op_result",
                {
                    **op.to_json(),
                    "target": op.target.label if op.target else None,  # 同じ名前のボタンは #n で分けて数える
                    "responded": responded,
                    "waited": round(waited, 3),
                    "verify": verify,
                    "interfered": self._irregular_t >= started,
                    "heartbeat_age": hb_age,
                    "recent": list(self.run.recent),
                    "state": self.state.snapshot(),
                },
                self.launch,
            )
        )

    async def _irregular(self, trigger: str) -> None:
        if not self._ready() or self._exiting:
            return
        key = "irregular:" + state_key(self.state)
        op = self.run.decider.choose(key, irregular_candidates(self.state, self.run.guard, int(self.cfg["rapid_count"])))
        if op is None:
            return
        self._irregular_t = time.monotonic()
        if await self._do(op, trigger=trigger):
            self.run.stats["irregular"] += 1
            self._verify_next = True

    async def _expect_exit(self, op: Op) -> None:
        # 押す前に知らせる（押した直後に終わっても、ハーネスの終了操作として扱われるように）
        self.on_expect_exit(True)
        self._exiting = True
        if await self._do(op):
            self.run.stats["expect_exit"] += 1
            self.run.last_exit_t = time.monotonic()
            self.publish(Event("harness", "exit_expected", {"target": op.target.key if op.target else None}, self.launch))
        else:
            self.on_expect_exit(False)
            self._exiting = False

    async def _external(self, op: Op) -> None:
        assert op.target is not None
        self.run.guard.use_external(op.target.key)
        if not await self._do(op):
            return
        self.run.stats["external"] += 1
        await asyncio.sleep(float(self.cfg["external_wait_sec"]))
        front = await asyncio.to_thread(self.pointer.platform.frontmost_pid)
        changed = front is not None and front != self.pointer.pid
        try:
            await asyncio.to_thread(self.pointer.ensure_front)
            back = True
        except PointerError:
            back = False
        self.publish(
            Event("harness", "external", {"target": op.target.key, "front_changed": changed, "back": back}, self.launch)
        )
        self._verify_next = True

    async def _do(self, op: Op, trigger: str | None = None) -> bool:
        """op を実行して op イベントを出す。押せなければ op_error を出して False。"""
        async with self._lock:
            try:
                pos = await asyncio.to_thread(self._input, op)
            except PointerError as e:
                self.run.stats["error"] += 1
                self.publish(Event("harness", "op_error", {**op.to_json(), "error": str(e)}, self.launch))
                return False
            except Exception as e:  # pyautogui の FailSafe（人がマウスを画面の隅に動かした）など
                self.run.stopped_reason = f"{type(e).__name__}: {e}"
                self.publish(Event("harness", "op_stopped", {**op.to_json(), "error": self.run.stopped_reason}, self.launch))
                return False
        self.run.recent.append(op.label)
        if op.target is not None:
            self.run.labels.add(op.target.label)
        self.publish(Event("harness", "op", {**op.to_json(), "trigger": trigger, "pos": pos}, self.launch))
        return True

    def _input(self, op: Op) -> tuple[float, float] | None:
        p = self.pointer
        ax, ay = (float(v) for v in self.cfg["advance_point"])
        k = op.kind
        if k in ("click", "expect_exit", "external"):
            assert op.target is not None
            return p.click(op.target)
        if k == "double_click":
            assert op.target is not None
            return p.click(op.target, clicks=2)
        if k == "advance":
            return p.click_at(ax, ay)
        if k == "rapid_click":
            return p.click_at(ax, ay, clicks=int(op.arg or 1))
        if k == "right_click":
            return p.click_at(ax, ay, button="right")
        if k == "wheel_up":
            return p.scroll_at(ax, ay, int(op.arg or 1))
        if k == "wheel_down":
            return p.scroll_at(ax, ay, -int(op.arg or 1))
        if k == "key":
            p.key(str(op.arg))
            return None
        if k == "rapid_key":
            p.key(str(op.arg), times=int(self.cfg["rapid_count"]))
            return None
        raise ValueError(f"未知の操作です: {k}")
