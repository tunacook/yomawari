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
from .experience import Experience, Press
from .guard import Guard
from .looks import brightest, dark, groups, inactive, measure
from .ops import (
    Op,
    escape_candidates,
    irregular_candidates,
    normal_candidates,
    state_key,
)
from .search import REVEAL, blind_candidates, leads, search_candidates, searchable_menus, stuck_rows

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
    # 画面の外を探す（fuzz/search.py）。ランのあいだ覚えておく
    seen: set[str] = field(default_factory=set)  # 一度でも見えたボタンの label
    silent: set[str] = field(default_factory=set)  # 最後に押したとき反応しなかったボタンの label
    search_miss: Counter[str] = field(default_factory=Counter)  # "<menu>/<name>:<向き>" → 動かしても続けて反応しなかった回数
    # menu → それを開いたボタンの label（ハーネスがボタンを押して開いた寄り道の画面）。ここにない menu はゲームが開いたもの
    opened_by: dict[str, str] = field(default_factory=dict)
    blind_tries: Counter[str] = field(default_factory=Counter)  # menu → 向きを決めずに探した回数（開き直すと 0 に戻す）
    search_noted: set[str] = field(default_factory=set)  # 「ハーネスの記録」に書いた探し方（menu ごと）
    # 進め方の経験（fuzz/experience.py）
    experience: Experience = field(default_factory=Experience)
    presses: deque[Press] = field(default_factory=lambda: deque(maxlen=20))  # 最近押した操作
    opened_press: dict[str, Press] = field(default_factory=dict)  # 寄り道の画面 → それを開いた操作
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
        self._looked: Any = None  # 見た目を比べたときの signature
        self._dim: set[str] = set()
        self._search: dict[str, list[str]] = {}  # 探す操作の label → それが向かう "<menu>/<name>:<向き>"
        self._blind: dict[str, list[str]] = {}  # 向きを決めずに探す操作の label → その menu
        self._title_returns = state.title_returns
        self._resolution_changes = state.resolution_changes

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
        if self.run.mode == "fuzz" and self.run.rng.random() < float(self.cfg["irregular_prob"]):
            await self._irregular("random")
            return

        allow_exit = now - (self.run.last_exit_t or self.run.started) >= float(self.cfg["exit_min_interval_sec"])
        cands = normal_candidates(
            self.state, self.run.guard, self.cfg["weights"], allow_exit, self.run.screen_weights,
            self._factors(),
        )
        if cands:
            cands += self._search_candidates()
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

    def _search_candidates(self) -> list[Op]:
        """まだ見ていないボタンが画面の外にある向きへ動かす操作（fuzz/search.py）。"""
        run = self.run
        for m in searchable_menus(self.state):
            run.seen |= {b.label for b in m.visible_buttons}
        for name in [n for n in run.opened_by if n not in self.state.menus]:
            del run.opened_by[name]  # 閉じた
            run.opened_press.pop(name, None)
        self._search, self._blind = {}, {}
        found = [
            lead for lead in leads(self.state, run.seen)
            if run.search_miss[f"{lead.menu}/{lead.name}:{lead.direction}"] < int(self.cfg["search_give_up"])
        ]
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
        weights: dict[str, float] = {}
        for lead in found:
            w = float(self.cfg["weights"]["click"]) * run.screen_weights.get(lead.menu, 1.0) * float(self.cfg["search_factor"])
            if all(label in stuck for label in lead.visible):
                w *= float(self.cfg["explore_factor"])  # 見えているものに押せそうなものがない。探すほうを先にする
            weights[f"{lead.menu}/{lead.name}"] = w
        ops = search_candidates(found, weights)
        for op in ops:
            self._search[op.label] = [
                f"{lead.menu}/{lead.name}:{lead.direction}" for lead in found
                if (op.kind, op.arg) in REVEAL[lead.direction]
            ]
        return ops

    def _note_search(self, menu: str, text: str, final: bool = True) -> None:
        """探し方を「ハーネスの記録」に書く。menu ごとに、始めたときと終えたときの1回ずつ。"""
        key = f"{menu}:{'end' if final else 'start'}"
        if key in self.run.search_noted:
            return
        self.run.search_noted.add(key)
        self.run.notes.append(f"{text}（起動 #{self.launch}）")

    def _quiet(self) -> set[str]:
        """押しても反応しないと見込んでいるボタン（暗い、文字で空と分かる）の label。"""
        return self._dim | inactive(self.state.targets(), self.run.inactive_words)

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
        self._dim = set()
        targets = self.state.targets()
        if float(self.cfg["dim_factor"]) >= 1 or not groups(targets):
            return  # 無効にしている、または同じ種類に見えるボタンが並んでいない
        try:
            shot = await asyncio.to_thread(self.pointer.capture, float(self.cfg["hover_wait_sec"]))
        except PointerError:
            return
        if shot is None:
            return  # 画面収録の権限がないなど。見た目なしで続ける
        run = self.run
        record: dict[str, Any] = {}
        for gkey, values in measure(targets, shot).items():
            ref = brightest(values)
            dk = dark(values, ref, float(self.cfg["dim_ratio"]), float(self.cfg["dim_min_gap"]))
            self._dim |= dk
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
        if responded:
            self.run.responsive.add((key, op.label))
        else:
            self.run.stats["no_response"] += 1
        if op.target is not None and op.target.kind == "button":
            (self.run.silent.discard if responded else self.run.silent.add)(op.target.label)
        if responded and op.target is not None:
            # ボタンで開いた画面は寄り道（fuzz/search.py の探し方を変える）
            for name in self.state.screens()[1] - screens_before[1]:
                if name in self.state.menus:
                    self.run.opened_by[name] = op.target.label
                    self.run.opened_press[name] = press
                    self.run.blind_tries.pop(name, None)
        if op.group == "search":
            for menu in self._blind.get(op.label, []):
                self.run.blind_tries[menu] += 1
            for k in self._search.get(op.label, []):
                if responded:
                    self.run.search_miss.pop(k, None)
                else:
                    self.run.search_miss[k] += 1
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
