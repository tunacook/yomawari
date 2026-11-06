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
import re
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..events import Event
from ..input.pointer import InputBlocked, Pointer, PointerError
from ..sensors.screen import Shot
from ..state import GameState, Rect, Target
from .decider import Decider
from .experience import Experience, Press
from .guard import Guard
from .looks import Values, alike, brightest, dark, disabled_looks, groups, inactive, masked, measure, text_inactive
from .advisor import ChoiceAdvisor
from .reading import assign, unread
from .ops import (
    Op,
    escape_candidates,
    irregular_candidates,
    normal_candidates,
    state_key,
)
from .flow import FlowTracker
from .resume import SaveHints, newest_slots
from .rollback import EDGE_OPS, EDGE_TIMINGS, PATTERNS, RollbackWatch, Step, describe, edge_timing, step_of
from .search import (
    REVEAL, Lead, blind_candidates, known_leads, leads, search_candidates, searchable_menus, stuck_rows, toward,
)

# プレイの目的（random モード）→ レポートでの呼び方
INTENTS = {"progress": "本編を進める", "explore": "画面を見て回る"}
# 再起動する前に、抜け出す操作を試すルール（ゲームは動いているが進まない）
RECOVERABLE = ("S-02", "S-03")

# イレギュラー操作を差し込むきっかけにするゲームのイベント
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
    run_sec: float = 0.0  # ランの長さ（--hours）。長いランで expect_exit の間隔を延ばすのに使う
    stats: Counter[str] = field(default_factory=Counter)
    labels: set[str] = field(default_factory=set)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    responsive: set[tuple[str, str]] = field(default_factory=set)  # (状態, 操作) のうち、反応があったもの
    last_press: tuple[str, float] | None = None  # 最後に押したボタンの label と時刻（役割の確定に使う）
    # 押したあともゲームが動き続けた（同じ起動のうちに次の操作をした）ボタンの label。終了ボタンとはみなさない
    survived: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # report.md の「ハーネスの記録」に載せること
    inactive_words: list[str] = field(default_factory=list)  # rules.yaml の inactive_hints.text
    masked_chars: str = ""  # rules.yaml の inactive_hints.masked（伏せ字の文字）
    save_hints: SaveHints = field(default_factory=SaveHints)  # rules.yaml の save_hints（セーブ済みのスロット・続きから）
    resume_until: float | None = None  # 起動し直して、続きから遊ぼうとしているあいだ（この時刻まで）
    saved_t: float | None = None  # 最後にセーブした時刻（流れの外。ゲームが流れの中で自分でするセーブは除く）
    save_first_until: float | None = None  # 終了ボタンを試す前にセーブしようとしているあいだ（この時刻まで）
    continue_previous: bool = False  # ランの最初の起動でも続きから遊ぶ（--continue）
    # 巻き戻しのエッジケース（fuzz/rollback.py）
    rollback: RollbackWatch = field(default_factory=RollbackWatch)
    edge_sent: Counter[tuple[str, str]] = field(default_factory=Counter)  # (タイミング, 操作の種類) → 差し込んだ回数
    rollback_dead: Counter[str] = field(default_factory=Counter)  # タイミング → 差し込んだのに前の行に戻らなかった回数
    menu_opener: dict[str, Step] = field(default_factory=dict)  # 開いているモーダル → それを開いた操作
    last_step: tuple[Step, float] | None = None  # 最後に行った操作と時刻
    text_decided: set[str] = field(default_factory=set)  # 文字で押せるかを決めた並び（「ハーネスの記録」に1回書く）
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
    # 見た目の推定（明るさ・半透明・文字）。並び → 測ったボタン、見分けられた（押せなさそうなものがあった）並び
    look_groups: dict[str, set[str]] = field(default_factory=dict)
    look_split: set[str] = field(default_factory=set)
    shot_failed: bool = False  # 画面を撮れなかった（「ハーネスの記録」に1回書く）
    search_miss: Counter[str] = field(default_factory=Counter)  # "<menu>/<name>:<向き>" → 動かしても続けて反応しなかった回数
    # menu → それを開いたボタンの label（ハーネスがボタンを押して開いた寄り道の画面）。ここにない menu はゲームが開いたもの
    opened_by: dict[str, str] = field(default_factory=dict)
    # 並び（looks の並びのキー）→ 押しても反応しなかったボタンの明るさ。同じような明るさのボタンも押せないとみなす
    locked_look: dict[str, tuple[float, float]] = field(default_factory=dict)
    locked_noted: set[str] = field(default_factory=set)
    # ボタン（label）→ 画面が手前にあったときに測った (位置, 明るさ)。後ろに下がったかを判定するのに使う
    look_base: dict[str, tuple[Rect, tuple[float, float]]] = field(default_factory=dict)
    behind_miss: int = 0  # 後ろに下がったとみなした画面があるあいだに、手前のものを押して続けて反応がなかった回数
    behind_refuted: set[str] = field(default_factory=set)  # 押した結果で、後ろに下がっていないと分かった menu（閉じるまで）
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
    # 通常のプレイの途中で、ある地点（初めて来た画面・scene）でときどき切り替える区間（purpose.py の Burst）
    burst_plan: Any = None  # Burst。None なら切り替えない
    burst_cfg: dict[str, Any] | None = None  # 区間の [fuzz] の設定
    burst: tuple[str, float, float, int] | None = None  # いまの区間 (地点, 始めた時刻, 終える時刻, 始めたときの操作数)
    burst_saved: tuple[dict[str, Any], str, str | None] | None = None  # 区間の前の (設定, mode, intent)
    burst_seen: set[str] = field(default_factory=set)  # 見たことのある地点
    bursts: list[tuple[str, float, int]] = field(default_factory=list)  # 終えた区間 (地点, 秒数, 操作数)
    # 本文の文脈から、どれを先に試すかを AI に聞く（fuzz/advisor.py）。None なら聞かない
    advisor: ChoiceAdvisor | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=100))  # 最近の本文
    question: list[str] = field(default_factory=list)  # 流れを始めたときの、質問の前の本文
    read_lines: list[str] = field(default_factory=list)  # 流れの途中で読んだ本文
    advice_seen: set[Any] = field(default_factory=set)
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

    def looks_notes(self) -> list[str]:
        """見た目では押せるかを見分けられなかった並び（推定できなかったことを、開発者に伝える）。

        明るさ・半透明・文字のどれでも差が出なかったのに、押して反応したものとしなかったものが混じっていた並び。
        ゲームが押せないことを見た目で表していないか、ハーネスの見方では拾えない表し方（アイコン・形など）をしている。"""
        out = []
        for gkey in sorted(self.look_groups):
            if gkey in self.look_split:
                continue
            labels = self.look_groups[gkey]
            res, sil = labels & self.answered, labels & (self.silent - self.answered)
            if res and sil:
                out.append(f"`{gkey}` の並びは、見た目（明るさ・半透明・文字）では押せるかを見分けられなかった（押して反応した"
                           f" {len(res)} 個と反応しなかった {len(sil)} 個が、同じように見えた）。押して確かめて覚えた")
        return out

    def edge_notes(self) -> list[str]:
        """エッジケース（壊れやすいタイミングへの差し込み）の記録。"""
        if not self.edge_sent:
            return []
        by_t: Counter[str] = Counter()
        by_k: Counter[str] = Counter()
        for (t, k), n in self.edge_sent.items():
            by_t[EDGE_TIMINGS.get(t, t)] += n
            by_k[EDGE_OPS.get(k, k)] += n
        line = (f"エッジケース: {sum(self.edge_sent.values())} 回差し込んだ（タイミング: "
                + "、".join(f"{t} {n} 回" for t, n in by_t.most_common()) + "。操作: "
                + "、".join(f"{k} {n} 回" for k, n in by_k.most_common()) + "）")
        seq = self.rollback.best()
        out = [line]
        if seq is not None:
            text = f"巻き戻しの操作は {describe(seq)} を使った（ハーネスの操作のあとに前の行が出たもの）"
            dead = sum(self.rollback_dead.values())
            if dead:
                text += (f"。巻き戻しを差し込んだのに本文が前の行に戻らなかったのは {dead} 回（"
                         + "、".join(f"{EDGE_TIMINGS.get(t, t)} {n} 回" for t, n in self.rollback_dead.most_common())
                         + "。巻き戻せない場面なら正常。戻るはずの場面なら不具合の可能性がある）")
            out.append(text)
        else:
            out.append("巻き戻しの操作をまだ覚えていないので、エッジケースに巻き戻しは使わなかった"
                       "（ハーネスの操作のあとに前の行が出たら、その操作を巻き戻しとして覚える）")
        return out

    def burst_notes(self) -> list[str]:
        """区間（ガチャプレイ・ボタンの網羅）の記録。"""
        if self.burst_plan is None:
            return []
        if not self.bursts:
            return [f"初めて来た画面・scene {len(self.burst_seen)} か所のどこでも、区間（{self.burst_plan.mode}）を始めなかった"]
        total = sum(sec for _s, sec, _n in self.bursts)
        spots = "、".join(f"{s}（{sec:.0f} 秒・{n} 回）" for s, sec, n in self.bursts[:10])
        more = f" ほか {len(self.bursts) - 10} か所" if len(self.bursts) > 10 else ""
        return [f"通常のプレイの途中で、初めて来た {len(self.burst_seen)} か所のうち {len(self.bursts)} か所で、区間"
                f"（{self.burst_plan.mode}）に切り替えた（計 {total:.0f} 秒）: {spots}{more}"]

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
        self._lock = asyncio.Lock()
        self._verify_next = False
        self._irregular_t = -1.0
        self._sig: Any = None
        self._sig_t = 0.0
        self._modal_since: float | None = None
        self._trigger: asyncio.Task[None] | None = None
        self._exiting = False
        self._pressed: str | None = None  # この起動で最後に押したボタン（次のボタンを押せたら、押しても終わらなかったもの）
        self._looked: Any = None  # 見た目を比べたときの signature
        self._dim: set[str] = set()
        # 暗いもののうち、押せないときの一般的な見せ方（半透明・灰色）か伏せ字で押せないと見たもの（V-07 の対象）。
        # 明るさの差だけでは入れない（絵柄・サムネイルが暗いだけのことがあり、暗い＝押せないかはデザイン次第のため）
        self._disabled: set[str] = set()
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
        self._start_resume()
        if launch > 0:
            run.flow.forget_reveals("起動し直した")

    # --- イベント（バスから同期で呼ばれる） ---

    @property
    def cfg(self) -> dict[str, Any]:
        """いまの [fuzz] の設定（区間（burst）の間は、区間の設定）。"""
        return self.run.cfg

    def recent_press(self, now: float | None = None) -> str | None:
        """role_window_sec のうちに押したボタンの "<menu>/<name>"。なければ None。"""
        lp = self.run.last_press
        now = time.monotonic() if now is None else now
        if lp is None or now - lp[1] > float(self.cfg["role_window_sec"]):
            return None
        return lp[0]

    # --- ある地点でときどき切り替える区間（ガチャプレイ・ボタンの網羅） ---

    def _spot(self, ev: Event) -> None:
        """初めて来た画面（ボタンのある menu）・scene なら、確率 prob で区間を始める（同じ地点では1回だけ）。"""
        run = self.run
        plan = run.burst_plan
        if plan is None or run.burst is not None or run.burst_cfg is None:
            return
        if ev.kind == "menu" and ev.data.get("buttons"):
            spot = f"画面 `{ev.data.get('name')}`"
        elif ev.kind == "scene" and ev.data.get("name"):
            spot = f"scene `{ev.data.get('name')}`"
        else:
            return
        if spot in run.burst_seen:
            return
        run.burst_seen.add(spot)
        now = time.monotonic()
        used = sum(sec for _s, sec, _n in run.bursts)
        if used + plan.sec > plan.share * max(now - run.started + plan.sec, plan.sec):
            return  # ランのうち区間に使ってよい割合を超える。先へ進めるのを優先する
        if run.rng.random() >= plan.prob:
            return
        run.burst_saved = (run.cfg, run.mode, run.intent)
        run.cfg, run.mode = run.burst_cfg, plan.mode
        if plan.intent is not None:
            run.intent = plan.intent
        ops = run.stats["normal"] + run.stats["irregular"]
        run.burst = (spot, now, now + plan.sec, ops)
        self.publish(Event("harness", "burst", {"phase": "start", "spot": spot, "sec": plan.sec, "mode": plan.mode},
                           self.launch))

    def _end_burst(self, why: str) -> None:
        run = self.run
        if run.burst is None or run.burst_saved is None:
            return
        spot, started, _until, ops0 = run.burst
        sec = time.monotonic() - started
        ops = run.stats["normal"] + run.stats["irregular"] - ops0
        run.cfg, run.mode, intent = run.burst_saved
        if run.burst_plan is not None and run.burst_plan.intent is not None:
            run.intent = intent
        run.burst, run.burst_saved = None, None
        run.bursts.append((spot, sec, ops))
        self.publish(Event("harness", "burst", {"phase": "end", "spot": spot, "sec": round(sec, 1), "ops": ops,
                                                "why": why}, self.launch))

    def _remember_line(self, d: dict[str, Any]) -> None:
        """本文を覚える（AI に文脈として渡す）。"""
        text = " ".join(re.sub(r"<[^>]*>", "", str(d.get("text") or "")).split())  # <br> などのタグは詰める
        if not text:
            return
        speaker = d.get("speaker")
        line = f"{speaker}「{text}」" if speaker else text
        self.run.lines.append(line)
        if self.run.flow.active is not None and len(self.run.read_lines) < 200:
            self.run.read_lines.append(line)

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
        if self.run.resume_until is not None:
            if ev.kind == "load" and d.get("phase") == "start":
                self._end_resume(f"セーブ（{d.get('slot')}）をロードした")
            elif ev.kind == "line":
                self._end_resume("本文が始まった（ロードせずに始めた）")
        if ev.kind == "load" and d.get("phase") == "start" and self.run.flow.active is None:
            # 流れの外でセーブをロードした（続きから遊ぶ・ロードの行き来）。流れの中のロード（思い出から本編に戻る）は除く
            self.run.flow.forget_reveals(f"セーブ（{d.get('slot')}）をロードした")
        if (ev.kind == "save" and d.get("phase") == "start" and self.run.flow.active is None
                and self.run.resume_until is None):
            self.run.saved_t = time.monotonic()
            if self.run.save_first_until is not None:
                self.run.save_first_until = None
                self.run.notes.append(f"終了ボタンを試す前にセーブした（{d.get('slot')}、起動 #{self.launch}）")
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
            if ev.launch == self.launch:
                self._watch_rollback(ev)
            if ev.launch == self.launch:
                self._spot(ev)
            if ev.kind == "line" and ev.launch == self.launch:
                self._remember_line(ev.data)
                self.run.flow.line_seen()
        if ev.source != "game" or ev.launch != self.launch:
            return
        timing = edge_timing(ev.kind, ev.data)
        if timing is not None:
            self._maybe_edge(timing)

    def _maybe_edge(self, timing: str) -> None:
        """壊れやすいタイミングに、狙って行うのが難しい操作（エッジケース）を差し込む。fuzz モード（ガチャプレイの区間など）は
        trigger_prob、random モード（通常のプレイ）は edge_prob の確率で（本文が出るたびには差し込まない）。"""
        run = self.run
        if run.mode == "fuzz":
            prob = float(self.cfg["trigger_prob"])
        elif run.mode == "random" and timing != "line":
            prob = float(self.cfg["edge_prob"])
        else:
            return
        if prob <= 0 or self._exiting or run.resume_until is not None:
            return
        if self._trigger is not None and not self._trigger.done():
            return
        if run.rng.random() >= prob:
            return
        delay = run.rng.uniform(0, float(self.cfg["trigger_delay_max_sec"]))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._trigger = loop.create_task(self._delayed_irregular(delay, timing))

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
            self._end_burst("起動が終わった")
            self._end_play()

    def _ready(self) -> bool:
        return self.state.booted and self.state.resolution is not None

    async def _step(self) -> None:
        if not self._ready():
            return
        now = time.monotonic()
        if self.run.burst is not None and now >= self.run.burst[2]:
            self._end_burst("時間になった")
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

        allow_exit = now - (self.run.last_exit_t or self.run.started) >= self._exit_interval()
        if allow_exit:
            allow_exit = self._saved_for_exit(now)
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
        cands = self._apply_resume(cands, now)
        cands = self._apply_save_first(cands)
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
        if run.burst is not None and run.burst_plan is not None and run.burst_plan.intent is not None:
            return  # 区間の間は、区間の目的（画面を見て回る）のまま
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
        step = tracker.update(self.state, run.opened_by, now, float(cfg["flow_idle_sec"]),
                              int(cfg["flow_retry_lines"]))
        after = (tracker.active.name if tracker.active else None, tracker.index)
        if after != before:
            self._note_flow(before, after)
        flow = tracker.active
        if flow is None:
            return cands
        if before[0] is None:
            # 流れを始めた（同じ質問に戻ってきたときも）: 質問の前の本文を覚え、途中で読んだ本文を数え直す
            run.question = list(run.lines)[-int(cfg["ai_context_lines"]):]
            run.read_lines = []
        boost, low = float(cfg["flow_factor"]), float(cfg["flow_avoid_factor"])
        # 押しても反応しないと分かっているもの（押して反応がなかった、文字で空・伏せ字と分かる）は選ばない。
        # 見た目だけで押せなさそうと見たもの（暗い、反応しなかったものと同じ見た目）は、ほかを試し終えてから選ぶ
        # （人も、明るいものが外れなら暗いものも試す。絵柄が暗いだけで選べるものがある。run 2026-11-05_1346）
        quiet = self._quiet() | run.silent
        sure = (run.silent - run.answered) | inactive(self.state.targets(), run.inactive_words, run.masked_chars)
        done = tracker.done_steps()

        def option(t: Any, skip: set[str]) -> str | None:
            """いまの手順で選べるもの（ボタンの label・選択肢の本文）。当てはまらなければ None。"""
            if step is None or t is None:
                return None
            if t.kind == "choice" and step.choose is not None:
                ok = not step.choose or any(fnmatchcase(t.text or "", p) for p in step.choose)
                return (t.text or t.label) if ok else None
            if (t.kind == "button" and t.label not in skip and step.when.screen == t.menu
                    and (step.presses(t.label, t.name, t.text) if step.targets
                         else not (step.advance or step.choose is not None))):
                return t.label
            return None

        options = {op.label: o for op in cands if (o := option(op.target, quiet)) is not None}
        later = {op.label: o for op in cands if (o := option(op.target, sure)) is not None and op.label not in options}
        tracker.offer(sorted(set(options.values()) | set(later.values())))
        if (step is not None and step.targets and not options and not later
                and (top := self.state.top_modal()) is not None and top.name == step.when.screen):
            # 手順の画面が出ているのに、選べるものが1つもない（言葉がまだ1つも開示されていない思い出など）。
            # この選び方は行き止まりとして、ほかの選び方を試す
            tracker.dead_end()
        # 試行錯誤: すべて試し終えた枝（外れだった組み合わせ）は後回し。いまの枝で試しかけのものがあれば、そちらを先に
        fresh = {o for o in options.values() if not tracker.exhausted(o)}
        if not fresh and later and (options or not self._step_bright_anywhere(step, quiet)):
            # 押せそうに見えるものを試し終えた（または、画面の外も含めて押せそうに見えるものがない）。見た目だけで
            # 押せなさそうと見たものも試す。押せそうに見えるものが画面の外にあるだけなら、先にそれを探す（ロック中の
            # 思い出を押して回らない。run 2026-11-05_1811）
            options |= later
            fresh = {o for o in later.values() if not tracker.exhausted(o)}
        away: list[Op] = []
        if options and not fresh:
            # 見えている選び方は試し終えた。画面の外に、まだ試していない選び方があれば、人が一覧をめくるようにそちらを探す
            # （一覧が最後のほうで開き、見えている 2 つの思い出だけを 1 時間試し続けた。run 2026-11-05_2230）
            away = self._flow_search(step, sure, boost)
            if not away:
                tracker.note_exhausted()
                fresh = set(options.values())  # 試せる組み合わせをすべて試した。ほかの道もないので、どれも同じに戻す
        going = {o for o in fresh if tracker.tried(o)}
        # いま選んでいるもの（思い出）の枝を試し終えた（言葉をすべて試した、言葉の一覧が空の行き止まり）のに、
        # その先の決まったボタン（言葉を選ぶ）の手順か、選べるものが1つもない手順の画面にいる。人が一覧に戻って選び直すように、
        # 手順の操作は優先せず、流れから外れるボタン（要約を閉じる・一覧に戻る）を優先して前の手順へ戻る
        # （run 2026-11-06_1646: 言葉のない思い出で、「言葉を選ぶ」と空の言葉の一覧を閉じるのを繰り返した）
        spent = step is not None and tracker.spent() and (not step.picks or not (options or later))
        favored = self._advice(going or fresh, cands, options)
        ai = float(cfg["ai_choice_factor"])
        out = []
        for op in cands:
            t = op.target
            f = 1.0
            o = options.get(op.label)
            if t is not None and flow.avoids(t.label, t.name, t.text):
                f = boost if spent else low
            elif spent:
                pass
            elif t is not None and t.kind == "button" and any(
                    s.when.screen == t.menu and s.targets and s.presses(t.label, t.name, t.text) for s in done):
                f = low  # 同じ選び方で一度済ませた手順のボタン（読んだ思い出をもう一度読む）
            elif step is not None and op.kind == "advance" and step.advance:
                f = boost
            elif o is not None:
                if o not in fresh:
                    f = low
                elif not going or o in going:
                    f = boost * (ai if o in favored else 1.0)
            if f >= boost and not spent:
                # 流れの手順の操作は、同じ質問に答え直すたびに同じボタン（言葉を選ぶ・この言葉で答える・答えを決める）を押す。
                # 押した回数で重みを下げる（novelty）と、答え直すほど流れの優先が薄れ、ドラッグ・Esc などの寄り道が増える
                # （run 2026-11-06_1418: 「この言葉で答える」を 45 回押したあとは、手順の外の操作のほうが多くなった）。
                # 同じ組み合わせを繰り返さないことは、試行錯誤（exhausted・tried）で見る
                op = dataclasses.replace(op, novelty=False)
            out.append(dataclasses.replace(op, weight=op.weight * f) if f != 1.0 else op)
        if away:
            labels = {op.label for op in away}
            out = [op for op in out if op.label not in labels] + away
        return out

    def _flow_search(self, step: Any, sure: set[str], boost: float) -> list[Op]:
        """手順の画面の外にある、手順のボタンでまだ試していない選び方へ動かす操作（試し終えたもの、押せないと分かっている
        もの（伏せ字・押して反応がなかった）は探さない）。動かしても見えてこない向きは search_give_up 回であきらめる。"""
        run, tracker = self.run, self.run.flow
        menu = self.state.menus.get(step.when.screen) if step is not None and step.when.screen else None
        if menu is None or not step.targets:
            return []
        skip = sure | inactive(menu.buttons, run.inactive_words, run.masked_chars)
        give_up = int(self.cfg["search_give_up"])

        def want(b: Target) -> bool:
            return (not b.locked and b.label not in skip and step.presses(b.label, b.name, b.text)
                    and not tracker.exhausted(b.label))
        found = [lead for lead in toward(self.state, menu.name, want)
                 if run.search_miss[self._lead_key(lead)] < give_up]
        if not found:
            return []
        w = float(self.cfg["weights"]["click"]) * run.screen_weights.get(menu.name, 1.0) * boost
        low = float(self.cfg["no_response_factor"])
        ops = []
        for op in search_candidates(found, {f"{lead.menu}/{lead.name}": w for lead in found}):
            targets = [lead for lead in found if (op.kind, op.arg) in REVEAL[lead.direction]]
            op = dataclasses.replace(op, weight=op.weight * low ** run.search_dead[(menu.name, op.label)])
            self._search[op.label] = [(self._lead_key(lead), lead.menu) for lead in targets]
            ops.append(op)
        self._note_search(menu.name, f"`{menu.name}` で、見えている選び方をすべて試したので、画面の外のまだ試していない"
                                     "項目を探した", final=False, tag="flow")
        return ops

    def _step_bright_anywhere(self, step: Any, quiet: set[str]) -> bool:
        """手順の画面に、手順のボタンで押せそうに見えるもの（見た目で押せなさそうとしていないもの）があるか。
        画面の外（hidden）のボタンは明るさを測っていないので、押せそうに見えるものとして数える。"""
        menu = self.state.menus.get(step.when.screen) if step is not None and step.when.screen else None
        if menu is None or not step.targets:
            return False
        return any(not b.locked and not b.covered and b.label not in quiet and step.presses(b.label, b.name, b.text)
                   for b in menu.buttons)

    def _advice(self, pick: set[str], cands: list[Op], options: dict[str, str]) -> set[str]:
        """先に試す候補のうち、本文の文脈にいちばん合うと AI が答えたもの。答えがまだ・聞かない目的なら空。"""
        run, tracker = self.run, self.run.flow
        if run.advisor is None or not self.cfg["ai_choice"] or len(pick) < 2 or tracker.ctx is None:
            return set()
        texts: dict[str, str] = {}
        for op in cands:
            o, t = options.get(op.label), op.target
            if o in pick and t is not None:
                text = t.text if t.kind == "button" else o  # 選択肢の候補は本文そのもの
                if text:
                    texts[o] = text
        if len(texts) < 2:
            return set()  # 文字の分からない候補（アイコンだけなど）は比べられない
        key = (tracker.ctx, tracker.index, tuple(tracker.path))
        scores = run.advisor.scores(key, run.question, run.read_lines, texts)
        if not scores:
            return set()
        best = max(scores.values())
        favored = {o for o, v in scores.items() if v == best and v > 0}
        seen = (key, tuple(sorted(texts.items())))
        if seen not in run.advice_seen:
            run.advice_seen.add(seen)
            self.publish(Event("harness", "advice", {
                "flow": tracker.ctx[0], "question": tracker.ctx[1], "step": tracker.index + 1,
                "scores": {texts[o]: scores.get(o) for o in texts}, "favored": sorted(texts[o] for o in favored),
            }, self.launch))
        return favored

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

    # --- 続きから遊ぶ（fuzz/resume.py） ---

    def _start_resume(self) -> None:
        """ラン中にゲームを起動し直したら（終了ボタンを確かめた・再起動した）、人がするように続きから遊ぶ。
        ロードするか本文が始まるか、resume_sec たったら終える。ランの最初の起動では、はじめから遊ぶ
        （--continue なら、最初の起動でも前のランのセーブの続きを遊ぶ）。"""
        run = self.run
        if (self.launch == 0 and not run.continue_previous) or float(self.cfg["resume_sec"]) <= 0:
            return
        run.resume_until = time.monotonic() + float(self.cfg["resume_sec"])

    def _end_resume(self, how: str) -> None:
        self.run.resume_until = None
        why = "前のランの続きから遊ぶ（--continue）ので" if self.launch == 0 else "起動し直したので"
        self.run.notes.append(f"{why}続きから遊ぼうとした: {how}（起動 #{self.launch}）")

    def _apply_resume(self, cands: list[Op], now: float) -> list[Op]:
        """続きから遊ぶあいだは、「続きから」のボタンと、いちばん新しいセーブ済みのスロットを先に押し、「はじめから」を
        後回しにする。見えているスロットがどれも空（文字で分かる）なら、ページを送るボタンを先に押す。
        続きから遊ぶとき以外は、セーブ済みのスロットに saved_slot_factor を掛ける（既定 0 で押さない。自分のセーブのロードは
        目的があるときだけにする。run 2026-11-06_1012 で、B_004 から A_009 のセーブをロードして 12 分やり直した）。"""
        run, hints = self.run, self.run.save_hints
        if run.resume_until is not None and now >= run.resume_until:
            self._end_resume(f"{float(self.cfg['resume_sec']):g} 秒のうちにロードも本文の始まりもなかった")
        targets = [op.target for op in cands if op.target is not None and op.target.kind == "button"]
        saved = [t for t in targets if hints.saved(t)]
        if run.resume_until is None:
            low = float(self.cfg["saved_slot_factor"])
            if not saved or low == 1.0:
                return cands
            labels = {t.label for t in saved}
            if low <= 0:
                return [op for op in cands if op.target is None or op.target.label not in labels]
            return [dataclasses.replace(op, weight=op.weight * low)
                    if op.target is not None and op.target.label in labels else op for op in cands]
        boost, low = float(self.cfg["resume_factor"]), float(self.cfg["resume_new_game_factor"])
        newest = newest_slots({t.label: t.text for t in saved}) or {t.label for t in saved}
        empty = inactive(targets, run.inactive_words, run.masked_chars)
        paging = not saved and bool(stuck_rows(self.state, empty))
        out = []
        for op in cands:
            t = op.target
            f = 1.0
            if t is not None and t.kind == "button":
                if t.label in newest or hints.resume(t) or (paging and hints.next_page(t)):
                    f = boost
                elif run.guard.book.role_of(t) == "new_game":
                    f = low
            out.append(dataclasses.replace(op, weight=op.weight * f) if f != 1.0 else op)
        return out

    # --- 終了を試す前にセーブする ---

    def _saved_for_exit(self, now: float) -> bool:
        """終了ボタンを試してよいか。起動し直したら続きから遊べるように、save_before_exit_sec のうちにセーブしていなければ、
        先に save_first_sec のあいだセーブしようとする（run 2026-11-06_1646: 22 分セーブしないまま終了ボタンを試し、
        起動し直したらロードするセーブがなく、はじめから遊び直した）。流れの途中（質問に答えているあいだ）は待つ。"""
        run, fresh = self.run, float(self.cfg["save_before_exit_sec"])
        if fresh <= 0 or run.resume_until is not None:
            return True
        if run.saved_t is not None and now - run.saved_t <= fresh:
            return True
        if run.save_first_until is None:
            if run.flow.active is not None:
                return False
            run.save_first_until = now + float(self.cfg["save_first_sec"])
            return False
        if now < run.save_first_until:
            return False
        run.notes.append(f"終了ボタンを試す前に {float(self.cfg['save_first_sec']):g} 秒セーブしようとしたが、セーブできなかった"
                         f"（起動 #{self.launch}。起動し直したら、はじめから遊ぶことがある）")
        run.save_first_until = None
        run.saved_t = now  # あきらめた。次の終了の間隔まで、セーブしようとしない
        return True

    def _apply_save_first(self, cands: list[Op]) -> list[Op]:
        """セーブしようとするあいだは、セーブするボタン（save_hints.save）と、それがある画面の空きのスロット（文字で空と
        分かるもの）を先に押す。"""
        run, hints = self.run, self.run.save_hints
        if run.save_first_until is None:
            return cands
        targets = [op.target for op in cands if op.target is not None and op.target.kind == "button"]
        menus = {t.menu for t in targets if hints.save(t)}
        empty = inactive([t for t in targets if t.menu in menus], run.inactive_words, run.masked_chars)
        boost = float(self.cfg["save_first_factor"])
        return [dataclasses.replace(op, weight=op.weight * boost)
                if op.target is not None and op.target.kind == "button"
                and (hints.save(op.target) or op.target.label in empty) else op for op in cands]

    # --- 巻き戻しのエッジケース（fuzz/rollback.py） ---

    def _operated(self, op: Op) -> None:
        """ハーネスが操作した。本文送りのほかは、次に前の行が出たら巻き戻しとして覚えるために控える。"""
        run = self.run
        step = step_of(op)
        run.last_step = (step, time.monotonic())
        if op.kind == "advance":
            return
        top = self.state.top_modal()
        if op.target is not None and top is not None and op.target.menu == top.name and top.name in run.menu_opener:
            run.rollback.operated((run.menu_opener[top.name], step))  # モーダル（バックログ）を開いた操作と合わせて
        else:
            run.rollback.operated((step,))

    def _watch_rollback(self, ev: Event) -> None:
        """本文の行の順番を追い、巻き戻しを差し込むタイミングを見る。"""
        run, d = self.run, ev.data
        if ev.kind == "menu" and d.get("modal") and d.get("name") not in run.menu_opener:
            last = run.last_step
            if last is not None and time.monotonic() - last[1] <= float(self.cfg["progress_window_sec"]):
                run.menu_opener[str(d.get("name"))] = last[0]
        elif ev.kind == "menu_close":
            run.menu_opener.pop(str(d.get("name")), None)
        elif ev.kind == "line" and d.get("line_id") is not None:
            run.rollback.line(str(d["line_id"]))
        elif ev.kind in ("scene", "load"):
            run.rollback.jumped()

    async def _rollback_edge(self, timing: str, seq: tuple[Step, ...]) -> bool:
        """巻き戻しを差し込む。1回戻る・続けて戻る・戻ってすぐ送るを繰り返す、のどれかで。"""
        run = self.run
        pattern = run.rng.choice(list(PATTERNS))
        times = 1 if pattern == "single" else run.rng.randint(2, max(2, int(self.cfg["rollback_repeat_max"])))
        before = run.rollback.rewinds
        sent = 0
        for _ in range(times):
            if self._exiting or not await self._replay(seq, timing):
                break
            sent += 1
            if pattern == "bounce":
                await self._do(Op("advance", irregular=True), trigger=f"rollback:{timing}")
        if not sent:
            return False
        await asyncio.sleep(float(self.cfg["rollback_check_sec"]))
        rewound = run.rollback.rewinds > before
        if not rewound:
            run.rollback_dead[timing] += 1
        self.publish(Event("harness", "rollback_edge", {
            "timing": timing, "pattern": pattern, "times": sent, "ops": describe(seq), "rewound": rewound,
        }, self.launch))
        return True

    async def _replay(self, seq: tuple[Step, ...], timing: str) -> bool:
        """覚えた巻き戻しの操作の並びを行う。ボタンは同じ名前のもの（バックログの別の行でもよい）を、出るまで少し待って押す。"""
        for kind, arg in seq:
            if arg is not None and "/" in arg:
                wait_until = time.monotonic() + float(self.cfg["rollback_menu_wait_sec"])
                found: list[Target] = []
                while True:
                    found = [t for t in self.state.targets() if t.label.split("#", 1)[0] == arg]
                    if found or time.monotonic() >= wait_until:
                        break
                    await asyncio.sleep(0.05)
                if not found:
                    return False
                op = Op(kind, self.run.rng.choice(found), irregular=True)
            else:
                value: str | int | None = int(arg) if arg is not None and arg.isdigit() else arg
                op = Op(kind, arg=value, irregular=True)
            if not await self._do(op, trigger=f"rollback:{timing}"):
                return False
        return True

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

    def _exit_interval(self) -> float:
        """expect_exit のボタンを押す間隔。exit_min_interval_sec か、長いランではランの長さの exit_interval_ratio 倍。"""
        return max(float(self.cfg["exit_min_interval_sec"]),
                   self.run.run_sec * float(self.cfg.get("exit_interval_ratio", 0.0)))

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
        # 前のランまでに押して先へ進んだボタン（タイトルの「はじめから」など）は、1回反応しなかっただけでは後回しにしない
        # （画面が切り替わる途中に押して反応しなかったなど。run 2026-11-06_1830: 起動直後に「はじめから」を1回押して反応せず、
        # 以後ほとんど押さずに、設定やロード画面の開け閉めを 4 分続けた）
        silent = self.run.silent - self.run.answered - self.run.experience.progressed_targets()
        return (self._dim | self._locked_alike() | silent
                | inactive(self.state.targets(), self.run.inactive_words, self.run.masked_chars))

    def _verify_behind(self, target: Target, responded: bool) -> None:
        """後ろに下がったとみなした画面があるあいだに手前のものを押した結果で、見た目での判定を確かめる。続けて
        behind_verify_misses 回反応がなければ、その画面は実は手前で入力を受けていた（開いている途中で測った、など）として
        判定を取り消す。押しても画面が変わらないと測り直すきっかけもないので、押した結果で直す。"""
        run, state = self.run, self.state
        run.behind_refuted &= set(state.menus)
        behind = {name for name in state.behind if name in state.menus and name != target.menu}
        if not behind or responded:
            run.behind_miss = 0
            return
        run.behind_miss += 1
        if run.behind_miss < int(self.cfg["behind_verify_misses"]):
            return
        run.behind_miss = 0
        run.behind_refuted |= behind
        state.behind -= behind
        run.notes.append(
            f"{'・'.join(f'`{m}`' for m in sorted(behind))} を見た目から後ろに下がったとみなしたが、手前のものを続けて "
            f"{self.cfg['behind_verify_misses']} 回押しても反応がなかったので、手前で入力を受けているとして判定を取り消した"
            f"（起動 #{self.launch}）"
        )

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
        run.behind_refuted &= set(self.state.menus)
        behind = {name for name in self.state.behind if name in self.state.menus}
        for menu, values in by_menu.items():
            ahead = bool(self.state.choices) or any(
                m.visible_buttons for name, m in self.state.menus.items() if name != menu and name not in behind
            )
            if compared[menu] >= 3:
                if menu in run.behind_refuted:
                    behind.discard(menu)  # 押した結果で、手前にあると分かっている（閉じるまで）
                elif changed[menu] / compared[menu] >= share and ahead:
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
        self._dim, self._disabled, self._varied, self._values = set(), set(), set(), {}
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
            # 画面収録の権限がないなど。見た目なしで続ける
            if not run.shot_failed:
                run.shot_failed = True
                run.notes.append(f"画面を撮れなかった（画面収録の権限がないなど）ので、ボタンの見た目（明るさ・半透明・文字）で"
                                 f"押せるかを推定できなかった。押して確かめた（起動 #{self.launch}）")
            return
        measured = measure(targets, shot) if looks else {}
        if reading:
            await self._read_text(shot, measured)
        if not looks:
            return
        record: dict[str, Any] = {}
        texts = {t.label: t.text for t in targets if t.text}
        self._judge_behind(measured, {t.label: t.rect for t in targets})
        for gkey, values in measured.items():
            if gkey.split("/", 1)[0] in self.state.behind:
                continue  # 後ろの画面は、手前の画面の影で暗く見えるので、暗い・明るいを判定しない
            ref = brightest(values)
            dk = dark(values, ref, float(self.cfg["dim_ratio"]), float(self.cfg["dim_min_gap"]))
            fade: dict[str, str] = {}
            if not dk:
                # 明るさでは差がない。押せないときのほかの一般的な見せ方（半透明・灰色）をしているものを、暗いものと同じに扱う
                fade = disabled_looks(targets, list(values), shot, float(self.cfg["fade_ratio"]),
                                      float(self.cfg["grey_ratio"]), float(self.cfg["fade_min_gap"]))
                dk = set(fade)
            dk = self._by_text(gkey, values, dk, texts)
            run.look_groups.setdefault(gkey, set()).update(values)
            if dk:
                run.look_split.add(gkey)
            self._dim |= dk
            self._disabled |= dk & (set(fade) | {label for label in dk if masked(texts.get(label), run.masked_chars)})
            self._values[gkey] = values
            if dk:
                self._varied.add(gkey)
            # 並びがすべて同じ明るさなら、暗い・明るいは分からないので、前の記録を残す
            for label in values if dk else ():
                now = label in dk
                # 文字で空と分かるもの（空のセーブスロットなど）は、明るさが揺れて明るく見えても先に押さない
                # （run 2026-11-06_1830: 空のスロットを「明るくなった」として次々に先に押し、ロード画面で空のスロットを押し続けた）
                empty = text_inactive(texts.get(label), run.inactive_words, run.masked_chars)
                if run.dim_history.get(label) and not now and label not in run.bright_noted and not empty:
                    run.bright_noted.add(label)
                    run.newly_bright.add(label)
                    run.notes.append(f"`{label}` は前に見たときは暗かったのに明るくなったので、先に押す（起動 #{self.launch}）")
                run.dim_history[label] = now
            if dk and gkey not in run.dim_seen:
                run.dim_seen.add(gkey)
                kinds = {"faded": "半透明に見える", "grey": "色を抜いた（灰色の）"}
                what = ("明るさでは差がなかったが、" + "・".join(kinds[k] for k in sorted(set(fade.values()))) + "ボタン"
                        if fade and dk == set(fade) else "いちばん明るいものより暗いボタン")
                run.notes.append(f"`{gkey}` の並びで、{what}（{len(dk)}/{len(values)} 個）を後回しにした（起動 #{self.launch}）")
            record[gkey] = {
                "ref": [round(ref[0], 3), round(ref[1], 3)],
                "buttons": {k: [round(v[0], 3), round(v[1], 3), k in dk] for k, v in values.items()},
            }
            if fade:
                record[gkey]["disabled_looks"] = dict(sorted(fade.items()))
        if record:
            # 明るさの判定をあとから確かめられるように残す（events.jsonl）
            self.publish(Event("harness", "looks", {"groups": record}, self.launch))

    def _by_text(self, gkey: str, values: Values, dk: set[str], texts: dict[str, str]) -> set[str]:
        """並びに伏せ字（「？？？」）のボタンがあれば、その並びは明るさではなく文字で押せるかを決める。

        人は、サムネイルの下が「？？？」なら未開放、題名が書いてあれば開放済みと読み、サムネイルの暗さでは決めない
        （絵そのものが暗いものがある）。伏せ字のものを暗い扱い（V-07 の対象）にし、文字を読めて伏せ字でないものは
        暗くても押せる扱いにする。文字を読めていないものは明るさで決める。並びに伏せ字のものが1つもなければ、
        明るさで決める（灰色にして押せないことを示すボタンは、文字はそのままのことが多いため）。「空き」などの語
        （inactive_hints.text）はきっかけにしない（空きのスロットを押して何かが開くのは正しい動作のことがあり、
        V-07 に使わないため。後回しにするのは _quiet でする）。"""
        run = self.run
        named = [label for label in values if label in texts]
        off = {label for label in named if masked(texts[label], run.masked_chars)}
        if not off:
            return dk
        out = (dk - set(named)) | off
        if gkey not in run.text_decided:
            run.text_decided.add(gkey)
            run.notes.append(
                f"`{gkey}` の並びは、伏せ字（「{texts[sorted(off)[0]]}」）で開いていないものが分かるので、明るさではなく文字で"
                f"押せるかを決めた（伏せ字 {len(off)} 個、文字を読めて押せる {len(set(named) - off)} 個）（起動 #{self.launch}）"
            )
        return out

    async def _read_text(self, shot: Shot, measured: dict[str, Values]) -> None:
        """ボタンの文字を、撮った画像から読む（fuzz/reading.py）。読んだボタンは、見た目が変わるまで読み直さない。"""
        run = self.run
        if self.state.closed:
            # 閉じた画面で読んだ文字は捨てる（同じ名前のボタンでも、次に開いたときは中身が違うことがある）
            prefixes = tuple(name + "/" for name in self.state.closed)
            run.read_done = {label for label in run.read_done if not label.startswith(prefixes)}
            run.read_look = {k: v for k, v in run.read_look.items() if not k.startswith(prefixes)}
            self.state.closed.clear()
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
            done = sum(st.completed for st in self.run.flow.stats.values())
            tracker = self.run.flow
            before = (tracker.active.name if tracker.active else None, tracker.index)
            tracker.pressed(t.label, t.name, responded, t.text if t.kind == "choice" else None,
                            t.text if t.kind == "button" else None)
            if sum(st.completed for st in tracker.stats.values()) > done:
                self._note_flow(before, (None, 0))  # 最後まで進めた（events.jsonl に流れの終わりを残す）
                self._maybe_edge("answer")
        if responded:
            self.run.responsive.add((key, op.label))
        else:
            self.run.stats["no_response"] += 1
        if op.target is not None:
            self._verify_behind(op.target, responded)
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
                    "disabled_look": bool(op.target and op.target.label in self._disabled),
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
        run = self.run
        seq = run.rollback.best()
        cands = irregular_candidates(self.state, run.guard, int(self.cfg["rapid_count"]), seq is not None)
        op = run.decider.choose(key, cands)
        if op is None:
            return
        self._irregular_t = time.monotonic()
        if op.kind == "rollback":
            assert seq is not None
            done = await self._rollback_edge(trigger, seq)
        elif op.kind == "menu_flash":
            # メニューを開いてすぐ閉じる（開く途中で閉じる）
            done = await self._do(Op("right_click", irregular=True), trigger=trigger)
            if done:
                await asyncio.sleep(float(self.cfg["menu_flash_sec"]))
                await self._do(Op("key", arg="esc", irregular=True), trigger=trigger)
        else:
            done = await self._do(op, trigger=trigger)
        if done:
            run.stats["irregular"] += 1
            run.edge_sent[(trigger, op.kind)] += 1
            self._verify_next = True

    async def _check_front(self, key: str) -> None:
        """押しても画面が変わらなかったボタン。ゲームの外のアプリが前面に出ていたら external と確定し、ゲームを前面に戻す。"""
        front = await asyncio.to_thread(self.pointer.platform.frontmost_pid)
        if front is None or front == self.pointer.pid:
            return
        if key in self.run.answered or key in self.run.experience.progressed_targets():
            # 前にゲームの中で反応したボタン。ほかのアプリ（前に開いた Web サイトのブラウザなど）が、あとから前面に出ていた
            # だけなので、external とはしない（run 2026-11-06_1830: ロード画面の「戻る」「次のページ」を external と確定し、
            # 1回のランで1回しか押さなくなって、ロード画面から抜けにくくなった）。ゲームを前面に戻し、押し直せるようにする
            try:
                await asyncio.to_thread(self.pointer.ensure_front)
            except PointerError:
                pass
            self.run.notes.append(f"`{key}` を押したとき、ほかのアプリが前面に出ていた（前にゲームの中で反応したボタンなので"
                                  f" external とはせず、ゲームを前面に戻した。起動 #{self.launch}）")
            self._verify_next = True
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
        else:
            # external と確定していたのに、押しても前面のアプリが変わらなかった（誤って確定していた）。ふつうのボタンに戻す
            self.run.guard.book.revoke(op.target.label, "external", "前面のアプリが変わらなかった")
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
        self._operated(op)
        if op.target is not None:
            self.run.labels.add(op.target.label)
            self.run.newly_bright.discard(op.target.label)
            if op.target.kind == "button":
                if self._pressed is not None:
                    self.run.survived.add(self._pressed)
                self._pressed = op.target.label
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
