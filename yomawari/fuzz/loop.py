"""ファジングのループ（DESIGN.md §9）。

1. 通常の操作を選んで押し、画面の状態（GameState.signature）が変わるまで待つ。結果を op_result として出す
2. fuzz モードでは、ゲームのイベント（暗転の開始、本文、選択肢、セーブなど）を受けてから 0〜trigger_delay_max_sec 後に
   イレギュラー操作を差し込む。時間ベースでも irregular_prob の確率で差し込む
3. イレギュラー操作の次の通常の操作は「応答確認」として扱う（反応がなければ S-01）。
   応答確認には、同じ状態で前に反応があった操作だけを使う（空のセーブスロットなど、普段から反応しないボタンで誤検知しないように）。
   そういう操作がなければ、その回は応答確認をしない

判定（S-01 / S-02 / S-05 / C-04）は detect/engine.py が op / op_result / exit_expected イベントから行う。
ここは操作と、反応があったかどうかの記録だけをする。

ボタンの役割（DESIGN.md §9「役割の推定と判定」）は、押す前の推定を Guard（RoleBook）が持ち、押したあとの観測で確定する。
ここで確定するのは、前面のアプリの切り替わり（external）、タイトル画面の再表示（to_title）、解像度の変化（system_setting）。
終了（exit）はプロセスの終了を見ているウォッチドッグが確定する。推定だけの exit は、押して role_window_sec のうちに
終わらなければ推定を取り消す（C-04 にしない）。
"""

from __future__ import annotations

import asyncio
import dataclasses
from fnmatch import fnmatchcase
import random
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..events import Event
from ..input.pointer import InputBlocked, Pointer, PointerError
from ..sensors.screen import Shot
from ..state import GameState, Rect
from .decider import Decider
from .experience import Experience, Press
from .guard import Guard
from .looks import Values, alike, brightest, dark, groups, inactive, measure
from .reading import assign, unread
from .ops import (
    Op,
    escape_candidates,
    irregular_candidates,
    normal_candidates,
    state_key,
)
from .flow import FlowTracker, matches
from .search import REVEAL, Lead, blind_candidates, known_leads, leads, search_candidates, searchable_menus, stuck_rows

# プレイの目的（random モード）→ レポートでの呼び方
INTENTS = {"progress": "本編を進める", "explore": "画面を見て回る"}
# 再起動する前に、抜け出す操作を試すルール（ゲームは動いているが進まない）
RECOVERABLE = ("S-02", "S-03")

# イレギュラー操作を差し込むきっかけにするゲームのイベント
TRIGGERS = {"transition", "line", "choices", "save", "load", "scene"}
# drag の向き → 描画領域の正規化座標での移動の向き（上へドラッグすると、下にある項目が出てくる）
DRAG = {"up": (0.0, -1.0), "down": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}


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
    last_press: tuple[str, float] | None = None  # 最後に押したボタンの label と時刻（役割の確定に使う）
    notes: list[str] = field(default_factory=list)  # report.md の「ハーネスの記録」に載せること
    inactive_words: list[str] = field(default_factory=list)  # rules.yaml の inactive_hints.text
    # 見た目（fuzz/looks.py）。ランのあいだ覚えておく
    dim_seen: set[str] = field(default_factory=set)  # 暗いボタンがあると記録した並び
    bright_noted: set[str] = field(default_factory=set)  # 新しく明るくなったとして先に押したボタン（ランで1回だけ）
    dim_history: dict[str, bool] = field(default_factory=dict)  # ボタン（label）→ 前に測ったとき暗かったか
    newly_bright: set[str] = field(default_factory=set)  # 前は暗かったのに明るくなり、まだ押していないボタン
    screen_weights: dict[str, float] = field(default_factory=dict)  # プロファイルの [screens.<menu>] weight
    flow: FlowTracker = field(default_factory=lambda: FlowTracker([]))  # ゲームの流れ（プロファイルの [[flow]]）
    # 画面の外を探す（fuzz/search.py）。ランのあいだ覚えておく
    seen: set[str] = field(default_factory=set)  # 一度でも見えたボタンの label
    silent: set[str] = field(default_factory=set)  # 最後に押したとき反応しなかったボタンの label
    answered: set[str] = field(default_factory=set)  # 押して反応があったことのあるボタンの label
    search_miss: Counter[str] = field(default_factory=Counter)  # "<menu>/<name>:<向き>" → 動かしても続けて反応しなかった回数
    # menu → それを開いたボタンの label（ハーネスがボタンを押して開いた寄り道の画面）。ここにない menu はゲームが開いたもの
    opened_by: dict[str, str] = field(default_factory=dict)
    # 並び（looks の並びのキー）→ 押しても反応しなかったボタンの明るさ。同じような明るさのボタンも押せないとみなす
    locked_look: dict[str, tuple[float, float]] = field(default_factory=dict)
    locked_noted: set[str] = field(default_factory=set)
    # ボタン（label）→ 画面が手前にあったときに測った (位置, 明るさ)。後ろに下がったかを判定するのに使う
    look_base: dict[str, tuple[Rect, tuple[float, float]]] = field(default_factory=dict)
    behind_noted: set[str] = field(default_factory=set)  # 「後ろに下がった」と記録に書いた menu  # 「同じ見た目も押せないとみなした」と記録に書いた並び
    search_dead: Counter[tuple[str, str]] = field(default_factory=Counter)  # (menu, 探す操作) → 続けて反応しなかった回数
    blind_tries: Counter[str] = field(default_factory=Counter)  # menu → 向きを決めずに探した回数（開き直すと 0 に戻す）
    search_noted: set[str] = field(default_factory=set)  # 「ハーネスの記録」に書いた探し方（menu ごと）
    # 進め方の経験（fuzz/experience.py）
    experience: Experience = field(default_factory=Experience)
    presses: deque[Press] = field(default_factory=lambda: deque(maxlen=20))  # 最近押した操作
    opened_press: dict[str, Press] = field(default_factory=dict)  # 寄り道の画面 → それを開いた操作
    # プレイの目的（random モード）。起動したとき・タイトルに戻ったときに選び直す
    intent: str | None = None
    plays: int = 0
    stopped_reason: str | None = None
    # OS が入力を受け付けなかった（画面のロック・ゲームを前面にできない）。いま続いているもの (理由, 始まりの時刻)
    blocked_since: tuple[str, float] | None = None
    # 画面の文字を読む（fuzz/reading.py）
    read_done: set[str] = field(default_factory=set)  # 画面から文字を読んだボタン（見た目が変わったら読み直す）
    read_look: dict[str, tuple[float, float]] = field(default_factory=dict)  # 読んだときのボタンの明るさ
    read_off: bool = False  # この OS では読めない
    read_noted: bool = False
    blocked: list[tuple[str, float, float]] = field(default_factory=list)  # 終わったもの (理由, 始まり, 終わり)。時刻は time.time()

    def block(self, reason: str, now: float) -> bool:
        """入力を受け付けなかった。新しく始まったら True。"""
        if self.blocked_since is not None and self.blocked_since[0] == reason:
            return False
        self.unblock(now)
        self.blocked_since = (reason, now)
        return True

    def unblock(self, now: float) -> tuple[str, float] | None:
        """入力を受け付けるようになった。続いていたものがあれば (理由, 秒数)。"""
        if self.blocked_since is None:
            return None
        reason, since = self.blocked_since
        self.blocked.append((reason, since, now))
        self.blocked_since = None
        return reason, now - since

    def blocked_notes(self, now: float) -> list[str]:
        spans = self.blocked + ([(*self.blocked_since, now)] if self.blocked_since else [])
        # 外部を開く操作のあとなど、すぐ戻ったものは載せない
        spans = [x for x in spans if x[2] - x[1] >= float(self.cfg["blocked_note_sec"])]
        if not spans:
            return []
        total = sum(e - s for _r, s, e in spans)
        detail = "、".join(
            f"{time.strftime('%H:%M:%S', time.localtime(s))}〜{time.strftime('%H:%M:%S', time.localtime(e))}（{r}）"
            for r, s, e in spans[-5:]
        )
        return [f"OS がゲームへの入力を受け付けず、計 {_duration(total)} 操作できなかった（{len(spans)} 回: {detail}"
                f"{' ほか' if len(spans) > 5 else ''}）。このあいだはゲームの不具合を探せていない。画面をロック・スリープ"
                "させずに回す（画面を暗くするだけにする）"]

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
        self._looked: Any = None  # 見た目を比べたときの signature
        self._dim: set[str] = set()
        self._varied: set[str] = set()  # 明るさに差のあった並び（looks の並びのキー）
        self._values: dict[str, Values] = {}  # 最後に撮った画像での、並びごとのボタンの明るさ
        # 探す・戻る操作の label → それが向かう ("<unseen|known>:<menu>/<name>:<向き>", menu)
        self._search: dict[str, list[tuple[str, str]]] = {}
        self._blind: dict[str, list[str]] = {}  # 向きを決めずに探す操作の label → その menu
        self._title_returns = state.title_returns
        self._resolution_changes = state.resolution_changes
        self._play_start = 0  # このプレイを始めたときに届いていた本文・scene・選択肢の数
        # 再起動の前に抜け出す操作を試している間: {"rule", "left", "since", "ops", "on_fail"}
        self._recovery: dict[str, Any] | None = None
        self._choose_intent("起動した")

    # --- イベント（バスから同期で呼ばれる） ---

    def recent_press(self, now: float | None = None) -> str | None:
        """role_window_sec のうちに押したボタンの "<menu>/<name>"。なければ None。"""
        lp = self.run.last_press
        now = time.monotonic() if now is None else now
        if lp is None or now - lp[1] > float(self.cfg["role_window_sec"]):
            return None
        return lp[0]

    def _observe(self, ev: Event) -> None:
        """タイトル画面の再表示と解像度の変化を、直前に押したボタンの役割として確定する。"""
        if ev.launch != self.launch:
            return
        if self.state.title_returns != self._title_returns:
            self._title_returns = self.state.title_returns
            key = self.recent_press()
            if key is not None:
                self.run.guard.book.confirm(key, "to_title", f"タイトル画面（{self.state.title_menu}）が再び出た")
            self._credit("rewind")
            self.run.flow.title_returned()
            self._end_play()
            self._choose_intent("タイトルに戻った")
        if self.state.resolution_changes != self._resolution_changes:
            self._resolution_changes = self.state.resolution_changes
            key = self.recent_press()
            if key is not None:
                self.run.guard.book.confirm(key, "system_setting", f"解像度・画面モードが {self.state.resolution} に変わった")

    def _progress(self, ev: Event) -> None:
        """まだ見ていない本文・scene・選択肢に届いたら進行、ロードが始まったら巻き戻しとして、直前の操作に数える。"""
        if ev.launch != self.launch:
            return
        d = ev.data
        place = None
        if ev.kind == "line" and d.get("line_id") is not None:
            place = f"line:{d.get('line_id')}"
        elif ev.kind == "scene" and d.get("name"):
            place = f"scene:{d.get('name')}"
        elif ev.kind == "choices" and d.get("items"):
            # 選択肢の id は表示のたびに変わるので、本文で表す
            place = "choices:" + "|".join(str(c.get("text")) for c in d["items"] if isinstance(c, dict))
        if place is not None and self.run.experience.reach(place):
            self._credit("progress")
        elif ev.kind == "load" and d.get("phase") == "start":
            self._credit("rewind")

    def _credit(self, kind: str) -> None:
        """progress_window_sec のうちに押した最後の操作と、いま開いている寄り道の画面を開いた操作に数える。"""
        run = self.run
        now = time.monotonic()
        window = float(self.cfg["progress_window_sec"])
        presses = [p for p in run.presses if now - p.t <= window][-1:]
        presses += [p for name, p in run.opened_press.items() if name in self.state.menus and p not in presses]
        run.experience.credit(presses, kind)

    def on_event(self, ev: Event) -> None:
        if ev.source == "game":
            self._observe(ev)
            self._progress(ev)
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
            self._end_play()

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
        if sig != self._looked:
            self._looked = sig
            await self._look()
        if self._recovery is not None:
            await self._recover_step()
            return
        if self.run.mode == "fuzz" and self.run.rng.random() < float(self.cfg["irregular_prob"]):
            await self._irregular("random")
            return

        allow_exit = now - (self.run.last_exit_t or self.run.started) >= float(self.cfg["exit_min_interval_sec"])
        cands = normal_candidates(
            self.state, self.run.guard, self.cfg["weights"], allow_exit, self.run.screen_weights,
            self._factors(),
        )
        if cands:
            search = self._search_candidates()
            if self._search:
                # 向きの決まった探し方・戻り方があるので、向きを決めずに動かす操作は使わない（探す向きと逆に動かして打ち消す）
                cands = [op for op in cands if op.group != "move" or op.label == "key enter"]
            cands += search
        cands = self._apply_intent(cands)
        cands = self._apply_flow(cands, now)
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
            # 画面を動かす操作（方向キー・Enter・ドラッグ・ホイール。一覧を探す操作も）は、フォーカスやスクロール位置で
            # 結果が変わるので使わない
            known = [op for op in cands if op.group not in ("move", "search", "return") and (key, op.label) in self.run.responsive]
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

    # --- プレイの目的 ---

    def _choose_intent(self, why: str) -> None:
        """プレイの目的を選ぶ（random モード）。ランの最初のプレイは、「本編を進める」の重みが 0 でなければそれにする。"""
        run = self.run
        if run.mode != "random":
            run.intent = None
            return
        if run.plays == 0 and float(self.cfg["intent_progress"]) > 0:
            intent = "progress"
        else:
            names = list(INTENTS)
            intent = run.rng.choices(names, weights=[float(self.cfg[f"intent_{n}"]) for n in names])[0]
        run.plays += 1
        run.intent = intent
        self._play_start = len(run.experience.reached)
        self.publish(Event("harness", "intent", {"intent": intent, "play": run.plays, "why": why}, self.launch))

    def _end_play(self) -> None:
        """プレイの目的と、そのあいだに新しく届いた本文・scene・選択肢の数を「ハーネスの記録」に書く。"""
        run = self.run
        if run.intent is None:
            return
        reached = len(run.experience.reached) - self._play_start
        run.notes.append(
            f"プレイ {run.plays}（起動 #{self.launch}）: 目的「{INTENTS[run.intent]}」。"
            f"新しく届いた本文・scene・選択肢 {reached} 件"
        )
        run.intent = None

    def _apply_flow(self, cands: list[Op], now: float) -> list[Op]:
        """ゲームの流れ（プロファイルの [[flow]]）に沿って、いまの手順の操作を優先し、流れから外れる操作を後回しにする。

        「画面を見て回る」プレイと、use_flow が false の目的（buttons / gacha）では使わない。押しても反応しないと
        見込んでいるボタン（暗い・ロック中と同じ見た目）は、手順のボタンでも優先しない（一覧から開示済みを探すため）。
        """
        run, cfg = self.run, self.cfg
        tracker = run.flow
        if not tracker.flows or not cfg["use_flow"] or run.intent == "explore":
            return cands
        before = (tracker.active.name if tracker.active else None, tracker.index)
        step = tracker.update(self.state, run.opened_by, now, float(cfg["flow_idle_sec"]))
        after = (tracker.active.name if tracker.active else None, tracker.index)
        if after != before:
            self._note_flow(before, after)
        flow = tracker.active
        if flow is None:
            return cands
        boost, low = float(cfg["flow_factor"]), float(cfg["flow_avoid_factor"])
        quiet = self._quiet() | run.silent
        done = tracker.done_steps()

        def option(t: Any) -> str | None:
            """いまの手順で選べるもの（ボタンの label・選択肢の本文）。当てはまらなければ None。"""
            if step is None or t is None:
                return None
            if t.kind == "choice" and step.choose is not None:
                ok = not step.choose or any(fnmatchcase(t.text or "", p) for p in step.choose)
                return (t.text or t.label) if ok else None
            if (t.kind == "button" and t.label not in quiet and step.when.screen == t.menu
                    and (matches(t.label, t.name, step.press) if step.press
                         else not (step.advance or step.choose is not None))):
                return t.label
            return None

        options = {op.label: o for op in cands if (o := option(op.target)) is not None}
        tracker.offer(sorted(set(options.values())))
        # 試行錯誤: すべて試し終えた枝（外れだった組み合わせ）は後回し。いまの枝で試しかけのものがあれば、そちらを先に
        fresh = {o for o in options.values() if not tracker.exhausted(o)}
        if options and not fresh:
            tracker.note_exhausted()
            fresh = set(options.values())  # 試せる組み合わせをすべて試した。ほかの道もないので、どれも同じに戻す
        going = {o for o in fresh if tracker.tried(o)}
        out = []
        for op in cands:
            t = op.target
            f = 1.0
            o = options.get(op.label)
            if t is not None and flow.avoid and matches(t.label, t.name, flow.avoid):
                f = low
            elif t is not None and t.kind == "button" and any(
                    s.when.screen == t.menu and s.press and matches(t.label, t.name, s.press) for s in done):
                f = low  # 同じ選び方で一度済ませた手順のボタン（読んだ思い出をもう一度読む）
            elif step is not None and op.kind == "advance" and step.advance:
                f = boost
            elif o is not None:
                if o not in fresh:
                    f = low
                elif not going or o in going:
                    f = boost
            out.append(dataclasses.replace(op, weight=op.weight * f) if f != 1.0 else op)
        return out

    def _note_flow(self, before: tuple[str | None, int], after: tuple[str | None, int]) -> None:
        """流れを始めた・手順を進めたことを events.jsonl に残す（「ハーネスの記録」には、ランの終わりにまとめて書く）。"""
        tracker = self.run.flow
        step = tracker.step
        self.publish(Event("harness", "flow", {
            "flow": after[0], "step": after[1] + 1 if after[0] else None,
            "note": step.note if step else None, "from": {"flow": before[0], "step": before[1] + 1},
        }, self.launch))

    def _apply_intent(self, cands: list[Op]) -> list[Op]:
        """寄り道を始める操作（本文を読んでいるときの右クリック・ホイール上、本文中に出ている操作のボタン）の重みを、
        目的に合わせて変える。「本編を進める」なら減らし、「画面を見て回る」なら増やす。"""
        intent = self.run.intent
        if intent is None or self.state.top_modal() is not None:
            return cands
        f = float(self.cfg[f"{intent}_detour_factor"])
        hud = self.state.reading_with_buttons()

        def detour(op: Op) -> bool:
            if op.kind in ("right_click", "wheel_up"):
                return True
            return hud and op.target is not None and op.target.kind == "button"

        return [dataclasses.replace(op, weight=op.weight * f) if detour(op) else op for op in cands]

    # --- 再起動の前に抜け出す ---

    def recover(self, rule: str, on_fail: Callable[[], None]) -> bool:
        """S-02 / S-03 のとき、再起動する前に、抜け出す操作を recover_ops 回まで試す。試せないときは False。"""
        if rule not in RECOVERABLE or int(self.cfg["recover_ops"]) <= 0 or self._exiting:
            return False
        if self._recovery is None:
            self._recovery = {
                "rule": rule, "left": int(self.cfg["recover_ops"]), "since": self.state.last_progress_t,
                "ops": [], "on_fail": on_fail,
            }
        return True

    async def _recover_step(self) -> None:
        r = self._recovery
        assert r is not None
        if self.state.last_progress_t != r["since"]:
            # 本文・scene・選択肢が来た。抜け出せたので再起動しない
            self._recovery = None
            ops = " → ".join(f"`{o}`" for o in r["ops"]) or "（操作なし）"
            self.run.notes.append(f"{r['rule']} のあと、{ops} で抜け出せたので、再起動しなかった（起動 #{self.launch}）")
            self.publish(Event("harness", "recovered", {"rule": r["rule"], "ops": r["ops"]}, self.launch))
            return
        if r["left"] <= 0:
            self._recovery = None
            ops = " → ".join(f"`{o}`" for o in r["ops"])
            self.run.notes.append(f"{r['rule']} のあと、{ops} を試しても抜け出せなかったので、再起動した（起動 #{self.launch}）")
            r["on_fail"]()
            return
        # 押せるもの（ガードに従う）と、Esc・右クリック・本文送りを、同じ重みで順に試す
        allow_exit = False
        cands = normal_candidates(self.state, self.run.guard, self.cfg["weights"], allow_exit, None, None)
        cands += [Op("key", arg="esc"), Op("right_click"), Op("advance")]
        seen: dict[str, Op] = {}
        for op in cands:
            if op.kind in ("click", "key", "right_click", "advance", "drag", "wheel_down", "wheel_up"):
                seen.setdefault(op.label, dataclasses.replace(op, weight=1.0, novelty=True, group=None))
        key = "recover:" + state_key(self.state)
        op = self.run.decider.choose(key, list(seen.values()))
        if op is None:
            r["left"] = 0
            return
        r["left"] -= 1
        r["ops"].append(op.label)
        await self._normal(op, key, False)

    def _search_candidates(self) -> list[Op]:
        """まだ見ていないボタンが画面の外にある向きへ動かす操作（fuzz/search.py）。"""
        run = self.run
        for m in searchable_menus(self.state):
            run.seen |= {b.label for b in m.visible_buttons}
        for name in [n for n in run.opened_by if n not in self.state.menus]:
            del run.opened_by[name]  # 閉じた
            run.opened_press.pop(name, None)
        self._search, self._blind = {}, {}
        found = self._choose_leads()
        stuck = self._quiet() | run.silent
        if not found:
            # どの向きに項目があるか分からない（画面の外の矩形が来ない、ページ送りの一覧など）、または一通り見終わった。
            # 見えている並びがどれも押せそうにないときに、すべての向きへ動かして押せるものを探すのは、ゲームが開いた画面
            # （本文の途中で選ばせる画面など。押せるものを見つけないと先に進めない）だけ。ハーネスがボタンで開いた寄り道の
            # 画面（右クリックメニューから開いた回想一覧など）は、押せるものがないのが正常なこともあるので探さない
            ops = []
            for menu, _name, point in stuck_rows(self.state, stuck):
                if menu in run.opened_by:
                    self._note_search(menu, f"`{menu}` は `{run.opened_by[menu]}` で開いた寄り道の画面なので、見ていない項目を"
                                            "一通り見たら、押せるものを探すのをやめた（押せるものがないのが正常なこともあるため）")
                    continue
                if run.blind_tries[menu] >= int(self.cfg["blind_search_limit"]):
                    self._note_search(menu, f"`{menu}` はゲームが開いた画面で、見えている項目がどれも押せず、すべての向きへ"
                                            f"{run.blind_tries[menu]} 回動かしても押せるものが見つからなかった（進めない可能性がある）")
                    continue
                self._note_search(menu, f"`{menu}` はゲームが開いた画面で、見えている項目がどれも押せないので、すべての向きへ"
                                        "動かして押せるものを探した", final=False)
                w = (float(self.cfg["weights"]["click"]) * run.screen_weights.get(menu, 1.0)
                     * float(self.cfg["search_factor"]) * float(self.cfg["explore_factor"]))
                for op in blind_candidates(point, w):
                    ops.append(op)
                    self._blind.setdefault(op.label, []).append(menu)
            return ops
        return self._lead_ops(found, stuck)

    def _choose_leads(self) -> list[Lead]:
        """画面の外へ動かす向き。まだ認識していない項目を探す（unseen）のと、すでに認識している開示済みの項目へ戻る
        （known）のは別の行動として、menu ごとにどちらか一方だけを選ぶ（両方を混ぜると、逆の向きへ動かして打ち消し合う）。
        見当をつけている項目が画面の外にあれば、まっすぐそちらへ向かう。見当がないときだけ、見ていない項目を探す。"""
        run = self.run
        give_up = int(self.cfg["search_give_up"])
        unseen = [lead for lead in leads(self.state, run.seen) if run.search_miss[self._lead_key(lead)] < give_up]
        known = [lead for lead in known_leads(self.state, self._known()) if run.search_miss[self._lead_key(lead)] < give_up]
        menus = {lead.menu for lead in known}
        return known + [lead for lead in unseen if lead.menu not in menus]

    @staticmethod
    def _lead_key(lead: Lead) -> str:
        return f"{lead.kind}:{lead.menu}/{lead.name}:{lead.direction}"

    def _lead_ops(self, found: list[Lead], stuck: set[str]) -> list[Op]:
        run = self.run
        click = float(self.cfg["weights"]["click"])
        low = float(self.cfg["no_response_factor"])
        ops: list[Op] = []
        for kind, group in (("unseen", "search"), ("known", "return")):
            these = [lead for lead in found if lead.kind == kind]
            weights: dict[str, float] = {}
            for lead in these:
                row = f"{lead.menu}/{lead.name}"
                w = click * run.screen_weights.get(lead.menu, 1.0)
                if kind == "known":
                    # 前に見て明るかった項目へ戻る。見えているものに明るいものがないときだけ候補になる
                    w *= float(self.cfg["return_factor"])
                    self._note_search(lead.menu, f"`{lead.menu}` で、前に見て明るかった項目（開示済みと分かっている"
                                                 "もの）が画面の外にあったので、そこへ戻った", final=False, tag="known")
                else:
                    w *= float(self.cfg["search_factor"])
                    if all(label in stuck for label in lead.visible) or row not in self._varied:
                        # 見えているものに押せそうなものがない、または見た目に差がなく、どれを押せばよいか分からない。
                        # 人が一覧を一通り眺めてから選ぶように、押して確かめる（反応がないと待たされる）より先に見て回る
                        w *= float(self.cfg["explore_factor"])
                weights[row] = max(weights.get(row, 0.0), w)
            for op in search_candidates(these, weights, group):
                targets = [lead for lead in these if (op.kind, op.arg) in REVEAL[lead.direction]]
                menu = targets[0].menu if targets else ""
                # この画面で動かしても反応しなかった手段（一覧が受け付けない方向キーなど）は後回しにする
                op = dataclasses.replace(op, weight=op.weight * low ** run.search_dead[(menu, op.label)])
                self._search[op.label] = [(self._lead_key(lead), lead.menu) for lead in targets]
                ops.append(op)
        return ops

    def _known(self) -> set[str]:
        """すでに認識している開示済みの項目（見当をつけている項目）: このランで測ったとき並びの中で明るかった（ほかに暗い
        ものがあった）もの、または押して先に進んだことのあるもの（前のランの経験を含む）。押して反応がなかったものは除く。"""
        bright = {label for label, dim in self.run.dim_history.items() if dim is False}
        return (bright | self.run.experience.progressed_targets()) - self.run.silent

    def _note_search(self, menu: str, text: str, final: bool = True, tag: str = "") -> None:
        """探し方を「ハーネスの記録」に書く。menu・行動（tag）ごとに、始めたときと終えたときの1回ずつ。"""
        key = f"{menu}:{tag}:{'end' if final else 'start'}"
        if key in self.run.search_noted:
            return
        self.run.search_noted.add(key)
        self.run.notes.append(f"{text}（起動 #{self.launch}）")

    def _quiet(self) -> set[str]:
        """押しても反応しないと見込んでいるボタン（暗い、文字で空と分かる、押しても一度も反応しなかったもの・それと
        同じ見た目のもの）の label。一度も反応しなかったボタンは S-05 の対象でないので、押し直しても待つ時間は短くてよい。"""
        return (self._dim | self._locked_alike() | (self.run.silent - self.run.answered)
                | inactive(self.state.targets(), self.run.inactive_words))

    def _judge_behind(self, measured: dict[str, Values], rects: dict[str, Rect]) -> None:
        """画面の見た目から、後ろに下がった画面を判定する。

        人は、ボタンの位置は変わらないのに見た目がまとめて変わった（暗幕がかかって暗くなった、別のパネルが重なった）
        画面を「後ろに下がった」とみなし、手前に出てきたものから次の操作を探す。ハーネスも、手前にあったときに測った
        明るさ（位置ごと）と比べ、同じ位置のボタンのうち behind_share 以上が、同じような明るさでなくなった（behind_ratio /
        behind_min_gap で判定）画面を後ろに下がったとし、押す候補から外す。見た目が元に戻れば、手前に戻ったとする。
        同じ位置で比べられるボタンが3つに満たないときは、前の判定を残す。手前に出てきたもの（ほかの画面の見えている
        ボタン・選択肢）がなければ、後ろに下がったとはしない（背景が動いて半透明のボタンの見た目が変わっただけのときに、
        押せるものがなくならないように）。"""
        run = self.run
        ratio, gap = float(self.cfg["behind_ratio"]), float(self.cfg["behind_min_gap"])
        share = float(self.cfg["behind_share"])
        compared: Counter[str] = Counter()
        changed: Counter[str] = Counter()
        by_menu: dict[str, dict[str, tuple[float, float]]] = {}
        for gkey, values in measured.items():
            menu = gkey.split("/", 1)[0]
            for label, v in values.items():
                by_menu.setdefault(menu, {})[label] = v
                base = run.look_base.get(label)
                if base is None or not _same_place(base[0], rects[label], float(self.cfg["behind_move_ratio"])):
                    continue  # 手前にあったときに、同じ位置で測っていない（スクロールした）
                compared[menu] += 1
                changed[menu] += not alike(v, base[1], ratio, gap)
        behind = {name for name in self.state.behind if name in self.state.menus}
        for menu, values in by_menu.items():
            ahead = bool(self.state.choices) or any(
                m.visible_buttons for name, m in self.state.menus.items() if name != menu and name not in behind
            )
            if compared[menu] >= 3:
                if changed[menu] / compared[menu] >= share and ahead:
                    if menu not in behind and menu not in run.behind_noted:
                        run.behind_noted.add(menu)
                        run.notes.append(
                            f"`{menu}` のボタンが、位置は変わらないのに見た目がまとめて変わった（{changed[menu]}/"
                            f"{compared[menu]} 個。暗くなった・ほかのものが重なった）ので、後ろに下がったとみなし、"
                            f"手前に出てきた画面を操作した（起動 #{self.launch}）"
                        )
                    behind.add(menu)
                else:
                    behind.discard(menu)
            if menu not in behind:
                for label, v in values.items():
                    run.look_base[label] = (rects[label], v)
        self.state.behind = behind

    def _locked_alike(self) -> set[str]:
        """押しても反応しなかったボタンと同じような明るさのボタン。人が1つ押してみて「この見た目は開示されていない」と
        覚え、同じ見た目のものは押さずに、ほかを探しにいくように。

        比べる相手は、同じ画像に写っている、反応しなかったボタン。写っていなければ、その並びで最後に反応しなかった
        ボタンを測ったときの明るさ（同じ画面の同じ並びなので、後ろの絵はほぼ変わらない）。"""
        run = self.run
        ratio, gap = float(self.cfg["dim_ratio"]), float(self.cfg["dim_min_gap"])
        out: set[str] = set()
        for gkey, values in self._values.items():
            refs = [v for label, v in values.items() if label in run.silent]
            if not refs and gkey in run.locked_look:
                refs = [run.locked_look[gkey]]
            if not refs:
                continue
            alikes = {
                label for label, v in values.items()
                if label not in run.silent and any(alike(v, r, ratio, gap) for r in refs)
            }
            if alikes and gkey not in run.locked_noted:
                run.locked_noted.add(gkey)
                run.notes.append(
                    f"`{gkey}` の並びで、押しても反応しなかったボタンと同じ見た目のもの（{len(alikes)} 個）も押せないと"
                    f"みなし、押さずにほかを探した（起動 #{self.launch}）"
                )
            out |= alikes
        return out

    def _factors(self) -> dict[str, float]:
        """ボタン（label）ごとに、押す重みに掛けるもの。"""
        out: dict[str, float] = {}
        low = float(self.cfg["dim_factor"])
        for label in self._quiet():
            out[label] = low
        for label in self.run.newly_bright:
            out[label] = float(self.cfg["new_bright_factor"])
        return out

    async def _look(self) -> None:
        """画面が落ち着いたら、同じ種類に見えるボタンの並びの明るさを比べ、暗いものを覚える（押す順番を後回しにする）。

        比べる相手は、同じときに撮った画像の中の、同じ並びでいちばん明るいもの。ランのあいだに見たいちばん明るいものとは
        比べない（半透明の UI は後ろの絵で明るさが変わり、別のときに撮ったものとは比べられないため）。
        前は暗かったのに、ほかに暗いものが残っている並びで明るくなったボタン（新しくアンロックされたもの）は、ランで1回だけ先に押す。
        """
        self._dim, self._varied, self._values = set(), set(), {}
        # 後ろに下がったと判定した画面のボタンも測る（手前に戻ってきたかを見るため）
        targets = self.state.targets() + [
            b for name in sorted(self.state.behind) if name in self.state.menus
            for b in self.state.menus[name].visible_buttons
        ]
        run = self.run
        looks = float(self.cfg["dim_factor"]) < 1 and bool(groups(targets))  # 同じ種類に見えるボタンが並んでいる
        reading = bool(self.cfg["read_text"]) and not run.read_off
        if not looks and not (reading and unread(self.state.targets(), run.read_done)):
            return
        try:
            shot = await asyncio.to_thread(self.pointer.capture, float(self.cfg["hover_wait_sec"]))
        except PointerError:
            return
        if shot is None:
            return  # 画面収録の権限がないなど。見た目なしで続ける
        measured = measure(targets, shot) if looks else {}
        if reading:
            await self._read_text(shot, measured)
        if not looks:
            return
        record: dict[str, Any] = {}
        self._judge_behind(measured, {t.label: t.rect for t in targets})
        for gkey, values in measured.items():
            if gkey.split("/", 1)[0] in self.state.behind:
                continue  # 後ろの画面は、手前の画面の影で暗く見えるので、暗い・明るいを判定しない
            ref = brightest(values)
            dk = dark(values, ref, float(self.cfg["dim_ratio"]), float(self.cfg["dim_min_gap"]))
            self._dim |= dk
            self._values[gkey] = values
            if dk:
                self._varied.add(gkey)
            # 並びがすべて同じ明るさなら、暗い・明るいは分からないので、前の記録を残す
            for label in values if dk else ():
                now = label in dk
                if run.dim_history.get(label) and not now and label not in run.bright_noted:
                    run.bright_noted.add(label)
                    run.newly_bright.add(label)
                    run.notes.append(f"`{label}` は前に見たときは暗かったのに明るくなったので、先に押す（起動 #{self.launch}）")
                run.dim_history[label] = now
            if dk and gkey not in run.dim_seen:
                run.dim_seen.add(gkey)
                run.notes.append(
                    f"`{gkey}` の並びで、いちばん明るいものより暗いボタン（{len(dk)}/{len(values)} 個）を後回しにした"
                    f"（起動 #{self.launch}）"
                )
            record[gkey] = {
                "ref": [round(ref[0], 3), round(ref[1], 3)],
                "buttons": {k: [round(v[0], 3), round(v[1], 3), k in dk] for k, v in values.items()},
            }
        if record:
            # 明るさの判定をあとから確かめられるように残す（events.jsonl）
            self.publish(Event("harness", "looks", {"groups": record}, self.launch))

    async def _read_text(self, shot: Shot, measured: dict[str, Values]) -> None:
        """ボタンの文字を、撮った画像から読む（fuzz/reading.py）。読んだボタンは、見た目が変わるまで読み直さない。"""
        run = self.run
        ratio, gap = float(self.cfg["dim_ratio"]), float(self.cfg["dim_min_gap"])
        now = {label: v for values in measured.values() for label, v in values.items()}
        for label, v in now.items():
            old = run.read_look.get(label)
            if old is not None and not alike(v, old, ratio, gap):
                run.read_done.discard(label)  # 見た目が変わった（開示された・中身が入れ替わった）ので読み直す
        todo = unread(self.state.targets(), run.read_done)
        if not todo:
            return
        read = getattr(self.pointer.platform, "read_text", None)
        boxes = await asyncio.to_thread(read, shot) if read is not None else None
        if boxes is None:
            run.read_off = True
            run.notes.append(f"画面の文字を読めない（この OS では未対応、または文字認識が使えない）ので、ボタンは名前と"
                             f"見た目だけで扱った（起動 #{self.launch}）")
            return
        texts = assign(boxes, todo, float(self.cfg["read_text_min_conf"]))
        for t in todo:
            run.read_done.add(t.label)
            if t.label in now:
                run.read_look[t.label] = now[t.label]
        if texts:
            self.state.set_read_text(texts)
            if not run.read_noted:
                run.read_noted = True
                run.notes.append(f"ボタンの文字を画面から読んだ（例: "
                                 f"{', '.join(f'`{k}` 「{v}」' for k, v in list(texts.items())[:3])}）（起動 #{self.launch}）")
        self.publish(Event("harness", "read_text", {"tried": len(todo), "texts": texts}, self.launch))

    async def _normal(self, op: Op, key: str, verify: bool) -> None:
        before = self.state.signature()
        screens_before = self.state.screens()
        saves_before = self.state.saves
        # 反応しないと見込んでいるボタンは、何も起きないことを確かめるだけなので、待つ時間を短くする
        quiet = op.target is not None and op.target.label in self._quiet()
        started = time.monotonic()
        press = Press(key, op.label, started)
        self.run.presses.append(press)
        if not await self._do(op):
            self.run.presses.remove(press)
            return
        self.run.experience.pressed(key, op.label)
        timeout = self.run.response_timeout
        if quiet:
            timeout = min(timeout, float(self.cfg["quiet_wait_sec"]))
        if op.group in ("move", "search", "return") and not verify:
            # 一覧を動かす操作は、効けば見た目がすぐ変わる（応答確認のときは S-01 の時間まで待つ）
            timeout = min(timeout, float(self.cfg["move_wait_sec"]))
        while time.monotonic() - started < timeout:
            if self.state.signature() != before:
                break
            await asyncio.sleep(0.05)
        waited = time.monotonic() - started
        responded = self.state.signature() != before
        if not responded and op.target is not None and op.target.kind == "button":
            await self._check_front(op.target.label)
        # 別の画面に切り替わったか（ボタンのある menu・モーダルが開いた／閉じた、scene が変わった）。V-07 が使う
        screen_changed = responded and self.state.screens() != screens_before
        self.run.stats["normal"] += 1
        self.run.decider.feedback(key, op, responded)
        if op.target is not None:
            t = op.target
            self.run.flow.pressed(t.label, t.name, responded, t.text if t.kind == "choice" else None)
        if responded:
            self.run.responsive.add((key, op.label))
        else:
            self.run.stats["no_response"] += 1
        if op.target is not None and op.target.kind == "button":
            (self.run.silent.discard if responded else self.run.silent.add)(op.target.label)
            if responded:
                self.run.answered.add(op.target.label)
            if not responded:
                for gkey, values in self._values.items():
                    if op.target.label in values:
                        self.run.locked_look[gkey] = values[op.target.label]
        if responded and op.target is not None:
            # ボタンで開いた画面は寄り道（fuzz/search.py の探し方を変える）
            for name in self.state.screens()[1] - screens_before[1]:
                if name in self.state.menus:
                    self.run.opened_by[name] = op.target.label
                    self.run.opened_press[name] = press
                    self.run.blind_tries.pop(name, None)
        if op.group in ("search", "return"):
            for menu in self._blind.get(op.label, []):
                self.run.blind_tries[menu] += 1
            for k, _menu in self._search.get(op.label, []):
                if responded:
                    self.run.search_miss.pop(k, None)
                else:
                    self.run.search_miss[k] += 1
            for menu in {m for _k, m in self._search.get(op.label, [])}:
                if responded:
                    self.run.search_dead.pop((menu, op.label), None)
                else:
                    self.run.search_dead[(menu, op.label)] += 1
        hb_age = time.monotonic() - self.state.last_game_t if self.state.last_game_t is not None else None
        self.publish(
            Event(
                "harness", "op_result",
                {
                    **op.to_json(),
                    "target": op.target.label if op.target else None,  # 同じ名前のボタンは #n で分けて数える
                    "type": op.target.type if op.target else None,
                    "dim": bool(op.target and op.target.label in self._dim),
                    "screen_changed": screen_changed,
                    "saved": self.state.saves != saves_before,  # 押してセーブが始まった（空きのスロットにセーブした）
                    "quiet": quiet,
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

    async def _check_front(self, key: str) -> None:
        """押しても画面が変わらなかったボタン。ゲームの外のアプリが前面に出ていたら external と確定し、ゲームを前面に戻す。"""
        front = await asyncio.to_thread(self.pointer.platform.frontmost_pid)
        if front is None or front == self.pointer.pid:
            return
        self.run.guard.book.confirm(key, "external", "前面のアプリがゲーム以外に切り替わった")
        self.run.guard.use_external(key)
        try:
            await asyncio.to_thread(self.pointer.ensure_front)
            back = True
        except PointerError:
            back = False
        self.publish(Event("harness", "external", {"target": key, "front_changed": True, "back": back}, self.launch))
        self._verify_next = True

    async def _expect_exit(self, op: Op) -> None:
        assert op.target is not None
        # 押す前に知らせる（押した直後に終わっても、ハーネスの終了操作として扱われるように）
        self.on_expect_exit(True)
        self._exiting = True
        if self.run.guard.book.is_estimate(op.target):
            await self._exit_probe(op)
            return
        if await self._do(op):
            self.run.stats["expect_exit"] += 1
            self.run.last_exit_t = time.monotonic()
            self.publish(Event("harness", "exit_expected", {"target": op.target.key if op.target else None}, self.launch))
        else:
            self.on_expect_exit(False)
            self._exiting = False

    async def _exit_probe(self, op: Op) -> None:
        """推定だけの exit を押す。終われば（ウォッチドッグが）確定、role_window_sec のうちに終わらなければ推定を取り消す。"""
        assert op.target is not None
        if await self._do(op):
            self.run.stats["expect_exit"] += 1
            self.run.last_exit_t = time.monotonic()
            await asyncio.sleep(float(self.cfg["role_window_sec"]))
            # ここまで来たら終わっていない（終われば、このセッションごと止められる）
            self.run.guard.book.cancel(op.target.label, f"{self.cfg['role_window_sec']:g} 秒たってもゲームが終わらなかった")
        self.on_expect_exit(False)
        self._exiting = False

    async def _external(self, op: Op) -> None:
        assert op.target is not None
        self.run.guard.use_external(op.target.label)
        if not await self._do(op):
            return
        self.run.stats["external"] += 1
        await asyncio.sleep(float(self.cfg["external_wait_sec"]))
        front = await asyncio.to_thread(self.pointer.platform.frontmost_pid)
        changed = front is not None and front != self.pointer.pid
        if changed:
            self.run.guard.book.confirm(op.target.label, "external", "前面のアプリがゲーム以外に切り替わった")
        try:
            await asyncio.to_thread(self.pointer.ensure_front)
            back = True
        except PointerError:
            back = False
        self.publish(
            Event("harness", "external", {"target": op.target.label, "front_changed": changed, "back": back}, self.launch)
        )
        self._verify_next = True

    async def _do(self, op: Op, trigger: str | None = None) -> bool:
        """op を実行して op イベントを出す。押せなければ op_error を出して False。"""
        blocked = False
        async with self._lock:
            try:
                pos = await asyncio.to_thread(self._input, op)
            except InputBlocked as e:
                self.run.stats["error"] += 1
                if self.run.block(str(e), time.time()):
                    self.publish(Event("harness", "input_blocked", {**op.to_json(), "error": str(e)}, self.launch))
                blocked = True
            except PointerError as e:
                self.run.stats["error"] += 1
                self.publish(Event("harness", "op_error", {**op.to_json(), "error": str(e)}, self.launch))
                return False
            except Exception as e:  # pyautogui の FailSafe（人がマウスを画面の隅に動かした）など
                self.run.stopped_reason = f"{type(e).__name__}: {e}"
                self.publish(Event("harness", "op_stopped", {**op.to_json(), "error": self.run.stopped_reason}, self.launch))
                return False
        if blocked:
            # 人が画面を戻すまで続くので、試す間隔をあける（何千回も試して events.jsonl を埋めないように）
            await asyncio.sleep(float(self.cfg["blocked_wait_sec"]))
            return False
        resumed = self.run.unblock(time.time())
        if resumed is not None:
            self.publish(Event("harness", "input_resumed", {"reason": resumed[0], "sec": round(resumed[1], 1)},
                               self.launch))
        self.run.recent.append(op.label)
        if op.target is not None:
            self.run.labels.add(op.target.label)
            self.run.newly_bright.discard(op.target.label)
            if op.target.kind == "button":
                self.run.last_press = (op.target.label, time.monotonic())
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
        px, py = op.point or (ax, ay)
        if k == "wheel_up":
            return p.scroll_at(px, py, int(op.arg or 1))
        if k == "wheel_down":
            return p.scroll_at(px, py, -int(op.arg or 1))
        if k == "drag":
            dx, dy = DRAG[str(op.arg)]
            d = float(self.cfg["drag_distance"])
            return p.drag(px, py, px + dx * d, py + dy * d)
        if k == "key":
            p.key(str(op.arg))
            return None
        if k == "rapid_key":
            p.key(str(op.arg), times=int(self.cfg["rapid_count"]))
            return None
        raise ValueError(f"未知の操作です: {k}")


def _same_place(a: Rect, b: Rect, ratio: float) -> bool:
    """同じ位置のボタンとみなせる: ずれが、ボタンの幅・高さの ratio 倍以内（開いたあとの小さな動きは同じ位置とする）。"""
    w, h = max(a[2] - a[0], 1e-6), max(a[3] - a[1], 1e-6)
    return (abs(a[0] - b[0]) <= w * ratio and abs(a[2] - b[2]) <= w * ratio
            and abs(a[1] - b[1]) <= h * ratio and abs(a[3] - b[3]) <= h * ratio)


def _duration(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600} 時間 {sec % 3600 // 60} 分" if sec >= 3600 else f"{sec // 60} 分 {sec % 60} 秒"
