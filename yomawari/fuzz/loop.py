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
from .looks import Values, alike, brighter, brightest, dark, disabled_looks, groups, inactive, letters, masked, measure, text_inactive
from .advisor import ChoiceAdvisor
from .reading import assign, unread
from .ops import (
    Op,
    escape_candidates,
    irregular_candidates,
    normal_candidates,
    state_key,
)
from .flow import Answer, FlowTracker, matches, text_matches
from .resume import SaveHints, newest_slots, older_than, stamp_key
from .rollback import EDGE_OPS, EDGE_TIMINGS, PATTERNS, RollbackWatch, Step, describe, edge_timing, step_of
from .listing import (
    MASKED, NO, OK, UNKNOWN, Goal, Ledger, Row, blind_candidates, direction, distance, moves, stuck_rows,
)

# プレイの目的（random モード）→ レポートでの呼び方
INTENTS = {"progress": "本編を進める", "explore": "画面を見て回る"}
# 再起動する前に、抜け出す操作を試すルール（ゲームは動いているが進まない）
RECOVERABLE = ("S-02", "S-03")

# イレギュラー操作を差し込むきっかけにするゲームのイベント
# drag の向き → 描画領域の正規化座標での移動の向き（上へドラッグすると、下にある項目が出てくる）
DRAG = {"up": (0.0, -1.0), "down": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}



def _pick_matches(text: str | None, pattern: str) -> bool:
    """画面から読んだ文字が、プロファイルの正解の pick に当たるか。画面から読んだ文字には、前後にアイコンや枠の読み違い
    （「•No.002 …」の「•」など）が付くことがあるので、前後に余計な文字があってもよいとする
    （run 2026-11-07_1909: 「•No.002 …」が "No.002*" に当たらず、正解の思い出を目標にしなかった）。"""
    return text_matches(text, (pattern, f"*{pattern}*"))


def _nearest_answers(answers: tuple[Answer, ...], ids: list[str]) -> tuple[Answer, ...]:
    """直近の本文の行（古い順）のうち、いちばん新しい行で at に当たる正解。その前の行で当たるものは、前の質問の正解なので含めない
    （run 2026-11-08_2227: 質問が続く場面で、前の質問の正解まで当たり、前の質問の思い出を選び続けた）。"""
    for line in reversed(ids):
        hit = tuple(a for a in answers if any(fnmatchcase(line, p) for p in a.at))
        if hit:
            return hit
    return ()


@dataclass
class FuzzRun:
    """ラン全体で持ち越すもの（試行回数、external の使用済み、統計）。起動し直しても引き継ぐ。"""

    mode: str  # fuzz | random
    decider: Decider
    guard: Guard
    rng: random.Random
    cfg: dict[str, Any]
    response_timeout: float
    # 応答確認に使う操作の数（rules.yaml の S-01 の checks）。反応がなければ、前に反応した別の操作でもう一度確かめる
    response_checks: int = 1
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
    saved_wall: float | None = None  # saved_t と同じときの time.time()（スロットの文字の日時と比べる。起動し直しても残す）
    resume_pages: int = 0  # 続きから遊ぶあいだに、ページを送った回数
    slot_stems: set[str] = field(default_factory=set)  # セーブ済みと読めたスロットの名前（#N を除く）。文字を読む前の同じ名前のボタンも押さない
    resume_texts: dict[str, str | None] = field(default_factory=dict)  # 続きから遊ぶあいだに見えていたセーブ済みのスロットの文字
    resume_load: tuple[str, str | None] | None = None  # 続きから遊ぶためにロードしたスロットと文字（本文が出たら消す）
    bad_saves: dict[str, str | None] = field(default_factory=dict)  # ロードしても進めなかったスロットと文字。続きから遊ぶときに選ばない
    save_first_until: float | None = None  # 終了ボタンを試す前にセーブしようとしているあいだ（この時刻まで）
    save_first_checked: float | None = None  # セーブしようとしているあいだ、最後に見た時刻（流れの途中の時間を数えないため）
    exit_unsaved: bool = False  # セーブできないまま終了を試すことにした（流れの途中では終了を試さない）
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
    # 一覧の台帳と目標（fuzz/listing.py）。ランのあいだ覚えておく
    ledger: Ledger = field(default_factory=Ledger)
    silent: set[str] = field(default_factory=set)  # 最後に押したとき反応しなかったボタンの label
    # 反応しなかったボタン → 反応しなかったあとに見た、いちばん明るい見た目（平均・明るい部分）と、読めた文字の数。
    # 開示された（はっきり明るくなった・文字が読めるようになった）かを見て、反応しないボタンから外す（_revive）
    silent_look: dict[str, tuple[float, float]] = field(default_factory=dict)
    silent_letters: dict[str, int] = field(default_factory=dict)
    revived_noted: set[str] = field(default_factory=set)
    answered: set[str] = field(default_factory=set)  # 押して反応があったことのあるボタンの label
    miss_streak: Counter[str] = field(default_factory=Counter)  # ボタンの label → 続けて反応しなかった回数（このラン）
    # 見た目の推定（明るさ・半透明・文字）。並び → 測ったボタン、見分けられた（押せなさそうなものがあった）並び
    look_groups: dict[str, set[str]] = field(default_factory=dict)
    look_split: set[str] = field(default_factory=set)
    shot_failed: bool = False  # 画面を撮れなかった（「ハーネスの記録」に1回書く）
    # menu → それを開いたボタンの label（ハーネスがボタンを押して開いた寄り道の画面）。ここにない menu はゲームが開いたもの
    opened_by: dict[str, str] = field(default_factory=dict)
    # 並び（looks の並びのキー）→ 押しても反応しなかったボタンの明るさ。同じような明るさのボタンも押せないとみなす
    locked_look: dict[str, tuple[float, float]] = field(default_factory=dict)
    locked_noted: set[str] = field(default_factory=set)
    # ボタン（label）→ 画面が手前にあったときに測った (位置, 明るさ)。後ろに下がったかを判定するのに使う
    look_base: dict[str, tuple[Rect, tuple[float, float]]] = field(default_factory=dict)
    behind_miss: int = 0  # 後ろに下がったとみなした画面があるあいだに、手前のものを押して続けて反応がなかった回数
    behind_refuted: set[str] = field(default_factory=set)  # 押した結果で、後ろに下がっていないと分かった menu（閉じるまで）
    behind_noted: set[str] = field(default_factory=set)  # 「後ろに下がった」と記録に書いた menu
    # ボタン → 最後に見えていたときに、見た目で押せなさそう（quiet）と見たか。見た目は見えているボタンしか測れないので、
    # 一覧の目標の順番では、画面の外へ出たボタンにこれを使う（閉じた画面のものは忘れる）
    quiet_seen: dict[str, bool] = field(default_factory=dict)
    blind_tries: Counter[str] = field(default_factory=Counter)  # menu → 向きを決めずに探した回数（開き直すと 0 に戻す）
    search_noted: set[str] = field(default_factory=set)  # 「ハーネスの記録」に書いた一覧の扱い（menu ごと）
    # 流れの手順で選ぶ候補の、同じ順位どうしの並べ方（ランの間は変えない。ランごとに変えて、いつも同じものから試さない）
    tie: dict[str, float] = field(default_factory=dict)
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
    line_ids: deque[str] = field(default_factory=lambda: deque(maxlen=100))  # 最近の本文の行（line_id）
    # 質問ごとの正解（DESIGN.md §9「答えの選び方」）。質問（流れの名前, 始めたときの行）→ プロファイルの answers のうち当たったもの
    ctx_answers: dict[Any, tuple[Answer, ...]] = field(default_factory=dict)
    wrong_left: dict[Any, int] = field(default_factory=dict)  # 質問 → この流れでまだわざと答える外れの数
    wrong_noted: set[Any] = field(default_factory=set)  # 正解のはずが外れだった組み合わせ（「ハーネスの記録」に書いたもの）
    wrong_given: set[Any] = field(default_factory=set)  # わざと答えた外れ (質問, 選んだものの並び)
    advice_seen: set[Any] = field(default_factory=set)
    # プロファイルの言語（DESIGN.md §9「プロファイルの言語」）。None なら合わせない
    locale: str | None = None
    locale_first: str | None = None  # ランで最初に見たゲームの言語
    locale_off: tuple[str, float] | None = None  # いまプロファイルと違う言語 (言語, 始まりの time.monotonic())
    locale_spans: list[tuple[str, float, float]] = field(default_factory=list)  # 終わったもの (言語, 始まり, 終わり)
    locale_unjudged: int = 0  # 違う言語のあいだに答えて、当たり外れを数えなかった回数
    locale_missing: bool = False  # プロファイルの言語が、ゲームの使える言語（settings の locales）になかった
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

    def locale_notes(self, now: float) -> list[str]:
        """ゲームの言語がプロファイルの言語と違ったあいだの記録。"""
        if self.locale is None or self.locale_first is None:
            return []
        out = []
        if self.locale_missing:
            out.append(f"プロファイルの言語（{self.locale}）が、ゲームの使える言語にない。プロファイルの locale を確かめる")
        spans = self.locale_spans + ([(*self.locale_off, now)] if self.locale_off else [])
        if not spans:
            return out
        total = sum(e - s for _l, s, e in spans)
        langs = "、".join(sorted({lang for lang, _s, _e in spans}))
        text = (f"ゲームの言語がプロファイルの言語（{self.locale}）と違ったのは計 {_duration(total)}（{langs}、{len(spans)} 回）。"
                "そのあいだはプロファイルの正解と照らし合わせなかった")
        if self.locale_first != self.locale:
            text += f"。ランを始めたときの言語は {self.locale_first} だった"
        if self.locale_unjudged:
            text += f"。そのあいだに答えた {self.locale_unjudged} 回は、当たり外れを数えなかった"
        if self.locale_off is not None:
            text += ("。ランの終わりまでプロファイルの言語に戻せなかった（その言語に切り替えるボタンを、まだ押して確かめて"
                     "いないか、その画面へ行けなかった）")
        out.append(text)
        return out

    def summary(self) -> str:
        s = self.stats
        return (
            f"操作: 通常 {s['normal']} 回（反応なし {s['no_response']} 回）、イレギュラー {s['irregular']} 回、"
            f"終了を期待する操作 {s['expect_exit']} 回、外部を開く操作 {s['external']} 回、"
            f"押せなかった {s['error']} 回。押したボタン・選択肢 {len(self.labels)} 種類"
        )


def _slot_stem(label: str) -> str:
    """同じ名前のボタンの番号（#N）を除いた名前。"""
    return re.sub(r"#\d+$", "", label)


def _presses(op: Op, labels: set[str]) -> bool:
    """op が labels のボタンを押すか。Enter はフォーカスのあるボタンを押すので、labels が見えている画面では押すものとする。"""
    if not labels:
        return False
    return op.label == "key enter" or (op.target is not None and op.target.label in labels)


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
        self._verify_tried: list[str] = []  # いまの応答確認で、押しても反応がなかった操作
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
        self._values: dict[str, Values] = {}  # 最後に撮った画像での、並びごとのボタンの明るさ
        self._blind: dict[str, list[str]] = {}  # 向きを決めずに探す操作の label → その menu
        self._list_row: dict[str, str] = {}  # 一覧の目標へ向かう操作の label → その一覧（Row.key）
        self._rows: dict[str, Row] = {}  # いまの一覧（Row.key → Row）
        # 目標へ向かって動かした操作の結果を、次に画面が落ち着いたときに確かめる: (一覧, 目標, 操作の label, 動かす前の距離)
        self._moving: tuple[str, str, str, float] | None = None
        self._odds: tuple[str, dict[str, Any]] | None = None  # Decider が最後に選んだ操作の label と確率
        self._title_returns = state.title_returns
        self._resolution_changes = state.resolution_changes
        self._play_start = 0  # このプレイを始めたときに届いていた本文・scene・選択肢の数
        # 再起動の前に抜け出す操作を試している間: {"rule", "left", "since", "ops", "on_fail"}
        self._recovery: dict[str, Any] | None = None
        self._choose_intent("起動した")
        self._start_resume()
        if launch > 0:
            self._forget_progress("起動し直した")
            run.decider.forget_last()

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
        """本文を覚える（AI に文脈として渡す。行はプロファイルの正解の質問と照らす）。"""
        if d.get("line_id") is not None:
            self.run.line_ids.append(str(d["line_id"]))
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
            self.run.wrong_left.clear()  # 次にまた同じ質問に着いたら、わざと外れを答えるかを決め直す
            self._forget_ledger("タイトルに戻った")
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
                label = self.recent_press()
                if label is not None and label in self.run.resume_texts:
                    self.run.resume_load = (label, self.run.resume_texts[label])
                self._end_resume(f"セーブ（{d.get('slot')}）をロードした")
            elif ev.kind == "line":
                self._end_resume("本文が始まった（ロードせずに始めた）")
        if ev.kind == "line":
            # ロードしたセーブから本文が出た。このセーブからは進める
            self.run.resume_load = None
        if ev.kind == "load" and d.get("phase") == "start" and self.run.flow.active is None:
            # 流れの外でセーブをロードした（続きから遊ぶ・ロードの行き来）。流れの中のロード（思い出から本編に戻る）は除く
            self._forget_progress(f"セーブ（{d.get('slot')}）をロードした")
            # 答えたあとにロードしたなら、また同じ質問に着いても、ゲームが外れと判定したのではない
            self.run.flow.rewound(f"セーブ（{d.get('slot')}）をロードした")
        if (ev.kind == "save" and d.get("phase") == "start" and self.run.flow.active is None
                and self.run.resume_until is None):
            self.run.saved_t = time.monotonic()
            self.run.saved_wall = time.time()
            self.run.exit_unsaved = False
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
                line_id = ev.data.get("line_id")
                self.run.flow.line_seen(str(line_id) if line_id is not None else None)
            elif ev.kind == "scene" and ev.launch == self.launch:
                self.run.flow.scene_changed()
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
        self._track_locale(now)
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
            cands += self._list_ops(cands)
        cands = self._apply_intent(cands)
        cands = self._apply_resume(cands, now)
        cands = self._apply_save_first(cands)
        cands = self._apply_flow(cands, now)
        cands = self._apply_locale(cands)
        cands = self._one_mover(cands)
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
            known = self._verify_candidates(cands, key)
            if known:
                cands, verify = known, True
        op = self.run.decider.choose(key, cands)
        if op is None:
            return  # 暗転中などで押せない。応答確認は次に押せるときまで持ち越す
        self._odds = (op.label, getattr(self.run.decider, "last_odds", {}))
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
        if tracker.abandon(int(cfg["flow_exhausted_picks"])):
            step, after = None, (None, tracker.index)
        if after != before:
            self._note_flow(before, after)
        self._judged()
        flow = tracker.active
        if flow is None:
            # 流れの外でも、読んだ思い出（どれかの流れの once の手順を、いま選んでいるもので済ませた）は読み直さない
            low = float(cfg["flow_avoid_factor"])
            return [op.scaled("flow", low) if self._read_before(op.target) else op for op in cands]
        self._sync_lists()
        if before[0] is None:
            # 流れを始めた（同じ質問に戻ってきたときも）: 質問の前の本文を覚え、途中で読んだ本文を数え直す
            run.question = list(run.lines)[-int(cfg["ai_context_lines"]):]
            run.read_lines = []
            self._start_question(flow)
        boost, low = float(cfg["flow_factor"]), float(cfg["flow_avoid_factor"])
        # 選べるもの（候補）: 選択肢は見えているもの、ボタンは手順の画面の一覧の台帳（画面の外の項目も含む。目標として
        # そこへ向かうため）。押しても反応しないと分かっているもの（押せない・伏せ字、押して反応がなかった、文字で空と
        # 分かる）は選ばない。見た目だけで押せなさそうと見たもの（暗い、反応しなかったものと同じ見た目）は、順番を後ろにする
        # （人も、明るいものが外れなら暗いものも試す。絵柄が暗いだけで選べるものがある。run 2026-11-05_1346）
        quiet = self._quiet() | run.silent
        sure = (run.silent - self._trusted()) | inactive(self.state.targets(), run.inactive_words, run.masked_chars)
        done = tracker.done_steps()
        row, options, texts = self._step_options(step, cands, sure)
        tracker.offer(sorted(options))
        if (step is not None and step.targets and not options
                and (top := self.state.top_modal()) is not None and top.name == step.when.screen):
            # 手順の画面が出ているのに、選べるものが1つもない（言葉がまだ1つも開示されていない思い出など）。
            # この選び方は行き止まりとして、ほかの選び方を試す
            tracker.dead_end()
        # 試行錯誤: すべて試し終えた枝（外れだった組み合わせ）は後回し
        fresh = {o for o in options if not tracker.exhausted(o)}
        known = self._answer_options(options, texts)  # 正解が分かっている質問なら、正解に沿う候補
        wrong = run.wrong_left.get(tracker.ctx, 0) > 0  # 正解の前に、わざと外れを答える
        if known and not wrong:
            # 正解は、このランで一度答えて試し終えた枝でも選ぶ（タイトルから遊び直して同じ質問に着いたなど）。ただし、この
            # ランでゲームが外れと判定したものは選び直さない。正解の照らし方を誤ったとき（読んだ文字の取り違え、プロファイルの
            # 正解が古い）に、同じ外れを答え続けないため（run 2026-11-10_2135: 同じ外れを 199 回答えた）
            fresh |= {o for o in known if (tracker.ctx, (*tracker.path, o)) not in tracker.wrong_now}
        elif known and wrong and fresh <= known and options - known:
            # わざと外れを答えるのに、まだ答えたことのない外れがない: 外れと分かっているもののうち、選んだ回数の少ないもの
            fresh = tracker.least_picked(options - known)
        press_ans = self._press_answer() if step is not None and step.picks and not wrong else ()
        if options and not fresh:
            tracker.note_exhausted()
            # 試せる組み合わせをすべて試した。ほかの道もないので、このランで選んだ回数の少ないものから選び直す
            # （同じものを選び続けない。run 2026-11-07_1343: 2 つとも外れと覚えた選択肢で、先頭の「話す」を 16 回選んだ）
            fresh = tracker.least_picked(options)
        # いま選んでいるもの（思い出）の枝を試し終えた（言葉をすべて試した、言葉の一覧が空の行き止まり）のに、
        # その先の決まったボタン（言葉を選ぶ）の手順か、選べるものが1つもない手順の画面にいる。人が一覧に戻って選び直すように、
        # 手順の操作は優先せず、流れから外れるボタン（要約を閉じる・一覧に戻る）を優先して前の手順へ戻る
        # （run 2026-11-06_1646: 言葉のない思い出で、「言葉を選ぶ」と空の言葉の一覧を閉じるのを繰り返した）
        # ただし、質問の組み合わせをすべて試し終えていて、ほかに選び直す道がないときは戻らずに答え直す（上の least_picked）。
        # 戻ると、選ぶ手順でまた選び直し、その先でまた戻る、をくり返す
        # （run 2026-11-12_0959: 言葉を選ぶ → 「この言葉で答える」の画面から一覧へ戻る、を 3 時間 15 分くり返した）
        spent = (step is not None and tracker.spent() and not tracker.all_spent()
                 and (not step.picks or not options))
        goal = None if spent or press_ans else self._flow_goal(row, fresh, quiet, texts, known, wrong)
        out = []
        for op in cands:
            t = op.target
            f = 1.0
            o = self._option_of(step, t)
            if press_ans and t is not None and t.kind == "button" and matches(t.label, t.name, press_ans):
                f = boost  # 選ばずに押すのが正解のボタン（avoid に書いたものでも押す）
            elif t is not None and flow.avoids(t.label, t.name, t.text):
                f = boost if spent else low
            elif spent:
                pass
            elif t is not None and t.kind == "button" and (any(
                    s.when.screen == t.menu and s.targets and s.presses(t.label, t.name, t.text) for s in done)
                    or self._read_before(t)):
                f = low  # 同じ選び方で一度済ませた手順のボタン（読んだ思い出をもう一度読む）
            elif step is not None and op.kind == "advance" and step.advance:
                f = boost
            elif o is not None and step is not None and step.picks:
                # 選ぶ手順: 目標（決めた順番の先頭）だけを優先する。正解が分からなければ、ほかの候補はふだんの重み。試し終えたものは後回し
                # 正解が分かっている質問（わざと外れを答えるときを除く）では、ほかの候補も後回しにする。目標へ動かしている途中で
                # 別の候補を押さないように（run 2026-11-08_2022: 正解へ向かう途中で、見えていた別の思い出を押して読み始めた）
                f = boost if o == goal else (low if o in options and (o not in fresh or (known and not wrong)) else 1.0)
            elif o is not None:
                f = boost  # 決まったボタンの手順（この思い出を読む・言葉を選ぶ・答えを決める）
            if f >= boost and not spent:
                # 流れの手順の操作は、同じ質問に答え直すたびに同じボタン（言葉を選ぶ・この言葉で答える・答えを決める）を押す。
                # 押した回数で重みを下げる（novelty）と、答え直すほど流れの優先が薄れ、ドラッグ・Esc などの寄り道が増える
                # （run 2026-11-06_1418: 「この言葉で答える」を 45 回押したあとは、手順の外の操作のほうが多くなった）。
                # 同じ組み合わせを繰り返さないことは、試行錯誤（exhausted・tried）で見る
                op = dataclasses.replace(op, novelty=False, by="flow", goal=o if o == goal else None)
            out.append(op.scaled("flow", f))
        if row is not None:
            # 手順の画面の一覧は、流れが目標を決める（ほかの目標へ向かう操作は使わない）。候補の文字がそろうまでは、
            # 台帳を埋める（まだ見えていない項目を見にいく）
            out = [op for op in out if not (op.group == "list" and self._list_row.get(op.label) == row.key)]
            unshown = run.ledger.unshown(row)
            cur = run.ledger.goals.get(row.key)
            filling = goal is None and bool(unshown) and not spent
            self._note_fill(row, filling)
            if goal is not None:
                out += self._toward(row, goal, "flow")
            elif filling:
                # 見にいっている途中の項目を続ける（近さで入れ替えると、逆の向きへ動かして行き来する）
                keep = cur.label if cur is not None and cur.purpose == "fill" and cur.label in unshown else None
                out += self._toward(row, keep or run.ledger.nearest(row, unshown), "flow", "fill")
        return out

    # --- プロファイルの言語（DESIGN.md §9「プロファイルの言語」） ---

    def _locale_off(self) -> bool:
        """ゲームの言語が、プロファイルの言語と違う（正解の文字と照らし合わせられない）。"""
        have = (self.state.settings or {}).get("locale")
        return self.run.locale is not None and isinstance(have, str) and have != self.run.locale

    def _track_locale(self, now: float) -> None:
        """プロファイルの言語と違う言語だったあいだを数える（「ハーネスの記録」に書く）。"""
        run = self.run
        settings = self.state.settings or {}
        have = settings.get("locale")
        if run.locale is None or not isinstance(have, str):
            return
        if run.locale_first is None:
            run.locale_first = have
            locales = settings.get("locales")
            if isinstance(locales, list) and locales and run.locale not in locales:
                run.locale_missing = True
        if run.locale_off is not None and run.locale_off[0] != have:
            lang, since = run.locale_off
            run.locale_spans.append((lang, since, now))
            run.locale_off = None
        if have != run.locale and run.locale_off is None:
            run.locale_off = (have, now)

    def _locale_switched(self, t: Target, locale: str) -> None:
        """ボタンを押して、ゲームの言語が切り替わった。そのボタンと、その画面を開いたボタンの並びを覚える
        （ランをまたいで、経験に覚える）。"""
        run = self.run
        route: list[str] = []
        menu, seen = t.menu, set()
        while menu in run.opened_by and menu not in seen:
            seen.add(menu)
            opener = run.opened_by[menu]
            route.insert(0, opener)
            menu = opener.split("/", 1)[0]
        known = run.experience.locale_buttons.get(t.label)
        run.experience.locale_switched(t.label, locale, route)
        if known != locale:
            run.notes.append(f"`{t.label}` を押すとゲームの言語が {locale} に切り替わったので、言語を戻すのに使う"
                             + (f"（この画面は {' → '.join(f'`{r}`' for r in route)} で開いた）" if route else ""))

    def _apply_locale(self, cands: list[Op]) -> list[Op]:
        """ゲームの言語がプロファイルの言語と違うあいだ、その言語に切り替えたことのあるボタンと、その画面を開くボタンを
        優先する（区間のあいだは戻さない。区間の中で言語を変えるのも、ガチャプレイで試したいことなので）。"""
        run = self.run
        f = float(self.cfg["locale_factor"])
        if f <= 1.0 or run.burst is not None or run.locale_missing or not self._locale_off():
            return cands
        exp = run.experience
        buttons = {label for label, tag in exp.locale_buttons.items() if tag == run.locale}
        if not buttons:
            return cands
        menus = {label.split("/", 1)[0] for label in buttons}
        route: set[str] = set()
        if not any(m in self.state.menus for m in menus):
            # 言語を切り替える画面が出ていない: その画面を開くボタン（開くまでの画面のボタンも）を押しにいく
            for m in menus:
                route |= set(exp.locale_routes.get(m, ()))
        out = []
        for op in cands:
            t = op.target
            if t is not None and t.kind == "button" and (t.label in buttons or t.label in route):
                op = dataclasses.replace(op, novelty=False, by="locale").scaled("locale", f)
            out.append(op)
        return out

    def _read_before(self, t: Target | None) -> bool:
        return t is not None and t.kind == "button" and self.run.flow.read_before(t.label, t.name, t.text)

    def _option_of(self, step: Any, t: Target | None) -> str | None:
        """いまの手順で選べるもの（ボタンの label・選択肢の本文）か。当てはまらなければ None。"""
        if step is None or t is None:
            return None
        if t.kind == "choice" and step.choose is not None:
            ok = not step.choose or any(fnmatchcase(t.text or "", p) for p in step.choose)
            return (t.text or t.label) if ok else None
        if (t.kind == "button" and step.when.screen == t.menu
                and (step.presses(t.label, t.name, t.text) if step.targets
                     else not (step.advance or step.choose is not None))):
            return t.label
        return None

    def _step_options(self, step: Any, cands: list[Op], sure: set[str]) -> tuple[Row | None, set[str], dict[str, str]]:
        """手順で選べるもの（候補）。(手順の画面の一覧, 候補, 候補の文字)。

        選択肢は見えているもの。ボタンを選ぶ手順は、手順の画面の一覧の台帳から（画面の外の項目も含む）。台帳で押せない・
        伏せ字、届かなかったもの、押せないと分かっているもの（sure）、ゲームが押せないと伝えたもの（locked）は除く。"""
        run = self.run
        texts: dict[str, str] = {}
        if step is None:
            return None, set(), texts
        if step.choose is not None or not step.picks:
            # 選択肢、または決まったボタンを押す手順: 見えているもの
            for op in cands:
                o = self._option_of(step, op.target)
                if o is not None and op.target is not None and o not in sure:
                    texts[o] = o
            return None, set(texts), texts
        row = next((r for r in self._rows.values() if r.menu == step.when.screen and (not step.targets or any(
            step.presses(label, run.ledger.items[label].name, run.ledger.items[label].text) for label in r.labels))), None)
        menu = self.state.menus.get(step.when.screen)
        buttons = {b.label: b for b in menu.buttons} if menu is not None else {}
        out: set[str] = set()
        labels = list(row.labels) if row is not None else [b.label for b in menu.visible_buttons] if menu else []
        for label in labels:
            b = buttons.get(label)
            item = run.ledger.items.get(label)
            if b is None or b.locked or label in sure or label in run.ledger.unreachable:
                continue
            if row is None and not b.visible:
                continue
            if item is not None and item.status in (NO, MASKED):
                continue
            if step.targets and not step.presses(label, b.name, b.text):
                continue
            out.add(label)
            if b.text:
                texts[label] = b.text
        return row, out, texts

    def _flow_goal(self, row: Row | None, fresh: set[str], quiet: set[str], texts: dict[str, str],
                   known: set[str] | None = None, wrong: bool = False) -> str | None:
        """選ぶ手順の目標: まだ試していない候補を、次の順に並べた先頭。
        0. 正解が分かっている質問なら、正解に沿うもの（わざと外れを答えるときは、正解に沿わないもの）
        1. いまの枝で試しかけのもの（同じ思い出の、ほかの言葉を試す） 2. 本文の文脈にいちばん合うと AI が答えたもの
        3. 見た目で押せそうなもの（暗くない・押せなかったものと同じ見た目でない） 4. 表示範囲から近いもの
        5. ランごとに決めた順（いつも同じものから試さない）。
        一覧の台帳を埋めている間（まだ見えていない項目がある）は、目標を決めない（候補の文字をそろえてから決める）。"""
        run, tracker = self.run, self.run.flow
        if not fresh:
            return None
        if row is not None and run.ledger.unshown(row) and not (known and not wrong and fresh & known):
            # 正解に沿う候補が見えていれば、台帳を埋め終えるのを待たずにそこへ向かう
            return None
        going = {o for o in fresh if tracker.tried(o)}
        favored = self._advice(going or fresh, texts)
        for o in fresh:
            run.tie.setdefault(o, run.rng.random())
        # 見た目で押せなさそうか（quiet）は、見えているボタンしか測れない（暗い、押しても反応しなかったものと同じ見た目）。
        # 画面の外へ出たボタンは、最後に見えていたときの判定を使う。いまの判定だけで並べると、目標が見えて quiet になる
        # たびに、画面の外の quiet でないものに目標が移り、逆の向きへ動かして行き来し続ける
        # （run 2026-11-13_0959: 一覧の上下にある 2 つを目標にし直し続け、100 分スクロールだけを続けた）
        measured = {label for values in self._values.values() for label in values}
        for o in fresh & measured:
            run.quiet_seen[o] = o in quiet

        def is_quiet(o: str) -> bool:
            return o in quiet if o in measured else run.quiet_seen.get(o, o in quiet)

        def rank(o: str) -> tuple:
            near = distance(run.ledger.items[o].rect, row.view) if row is not None and o in run.ledger.items else 0.0
            answer = (o in known) == wrong if known is not None else False
            return (answer, o not in going if going else False, o not in favored, is_quiet(o), near, run.tie[o])
        best = min(fresh, key=rank)
        # いまの目標が、順番の先頭と同じ順位（近さより前の項目）のままなら、目標を変えない。動かすたびに近さが変わって
        # 目標が入れ替わると、逆の向きへ動かして行き来するため
        cur = run.ledger.goals.get(row.key) if row is not None else None
        if cur is not None and cur.by == "flow" and cur.label in fresh and rank(cur.label)[:4] == rank(best)[:4]:
            return cur.label
        return best

    def _start_question(self, flow: Any) -> None:
        """流れを始めた: プロファイルの正解のうち、この質問のもの（流れを始めたときの直近 ai_context_lines 行のうち、いちばん
        新しい行で at に当たるもの）を覚え、正解が分かっていれば、わざと外れを答えるかを決める（DESIGN.md §9「答えの選び方」）。"""
        run, cfg, ctx = self.run, self.cfg, self.run.flow.ctx
        if ctx is None:
            return
        if flow.answers:
            ids = list(run.line_ids)[-int(cfg["ai_context_lines"]):]
            hit = _nearest_answers(flow.answers, ids)
            if hit:
                # 外れへの返事のあとに同じ質問に戻ったときは、質問の行が直近にないことがあるので、前に当たったものを使う
                run.ctx_answers[ctx] = hit
        if ctx not in run.wrong_left and not self._locale_off() and (self._profile_answers() or run.flow.known_right()):
            prob = float(cfg["wrong_answer_prob"])
            run.wrong_left[ctx] = int(cfg["wrong_answers_per_question"]) if prob > 0 and run.rng.random() < prob else 0

    def _profile_answers(self) -> tuple[Answer, ...]:
        """いまの質問の、プロファイルの正解（answer_source が profile のときだけ）。"""
        if self.cfg["answer_source"] != "profile" or self._locale_off():
            return ()  # 違う言語のあいだは、プロファイルの文字と照らし合わせられない
        return self.run.ctx_answers.get(self.run.flow.ctx, ())

    def _text_of(self, o: str) -> str | None:
        """選んだもの（ボタンの label・選択肢の本文）の画面に見える文字。"""
        item = self.run.ledger.items.get(o)
        if item is not None and item.text:
            return item.text
        for menu in self.state.menus.values():
            for b in menu.buttons:
                if b.label == o:
                    return b.text
        return None if "#" in o else o  # label でなければ選択肢の本文

    def _picked(self, o: str, pick: str | tuple[str, ...], shown: str | None = None) -> bool:
        """選んだもの（の、いま見えている文字とこれまでに読んだ文字のどれか）が、pick の1つの手順に当たるか。
        同じ項目でも読むたびに番号だけ・題名だけと変わるので、最後に読んだ文字だけでは比べない。"""
        item = self.run.ledger.items.get(o)
        seen = (shown, self._text_of(o), *(item.readings if item is not None else ()))
        spellings = (pick,) if isinstance(pick, str) else pick
        return any(_pick_matches(t, s) for t in seen for s in spellings)

    def _answer_options(self, options: set[str], texts: dict[str, str]) -> set[str] | None:
        """いまの手順の候補のうち、正解に沿うもの。正解が分からない質問なら None。

        プロファイルの正解は、ここまでに選んだものの文字が pick の前の手順と合うときに、いまの手順の pick の文字で照らす。
        プロファイルに正解がないか、書いた文字に当たる候補がまだない（文字を読めていない）なら、覚えた正解（flow_right）の並びで
        照らす（answer_source が learn なら、いつも）。"""
        tracker = self.run.flow
        path = tuple(tracker.path)
        depth = len(path)
        answers = self._profile_answers()
        if answers:
            out: set[str] = set()
            loose: set[str] = set()
            for a in answers:
                if len(a.pick) <= depth:
                    continue
                hit = {o for o in options if self._picked(o, a.pick[depth], texts.get(o))}
                if all(self._picked(path[k], a.pick[k]) for k in range(depth)):
                    out |= hit
                else:
                    loose |= hit
            # 前に選んだものの文字が読めていない・読み違えた（思い出の番号が欠けたなど）ときは、いまの手順の文字だけで照らす
            if out or loose:
                return out or loose
        # プロファイルの正解を読めていないときの代わりなので、外れと分かった並び（ビルドで番号が変わったなど）は使わない
        learned = [r for r in tracker.known_right() if not answers or (tracker.ctx, r) not in tracker.wrong]
        if learned:
            return {o for o in options if any(len(r) > depth and r[:depth] == path and r[depth] == o for r in learned)}
        return set() if answers else None

    def _press_answer(self) -> tuple[str, ...]:
        """選ばずに押すのが正解のボタン（プロファイルの answers の press）。"""
        return tuple(a.press for a in self._profile_answers() if a.press)

    def _is_answer(self, ctx: Any, path: tuple[str, ...]) -> bool:
        answers = self.run.ctx_answers.get(ctx, ()) if self.cfg["answer_source"] == "profile" else ()
        if answers:
            return any(len(a.pick) == len(path) and all(self._picked(p, a.pick[k]) for k, p in enumerate(path))
                       for a in answers)
        return path in self.run.flow.known_right(ctx)

    def _answered(self) -> None:
        """流れを最後まで進めた（答えた）。わざと外れを答えるつもりで、正解でないものを答えたら、外れの残りを減らす。"""
        run, tracker = self.run, self.run.flow
        leaf = tracker.last_leaf
        if leaf is not None:
            self._note_answer(*leaf)
        if leaf is not None and self._locale_off():
            # プロファイルと違う言語で答えた: 選んだものの文字が正解の文字と比べられないので、当たり外れを数えない
            # （run 2026-11-12_2156: 英語で答えた外れのはずの答えを、外れと照らし合わせられずに「先へ進んだ」と記録した）
            run.locale_unjudged += 1
            tracker.forget_answer()
            return
        if leaf is None or run.wrong_left.get(leaf[0], 0) <= 0 or self._is_answer(*leaf):
            return
        run.wrong_left[leaf[0]] -= 1
        run.wrong_given.add(leaf)
        tracker.deliberate_wrong += 1

    def _note_answer(self, ctx: Any, path: tuple[str, ...]) -> None:
        """答えたことを events.jsonl に残す。レポートで、質問ごとに何を答えたか・プロファイルの正解が分かっていたかを
        数えるため（DESIGN.md §12「レポート構成」）。"""
        run = self.run
        answers = run.ctx_answers.get(ctx, ()) if self.cfg["answer_source"] == "profile" else ()
        known = [a.note or " → ".join(p if isinstance(p, str) else " / ".join(p) for p in a.pick) or a.press
                 for a in answers]
        self.publish(Event("harness", "answer", {
            "flow": ctx[0], "question": ctx[1], "picked": [self._text_of(p) or p for p in path],
            "known": known,  # プロファイルに書いた、この質問の正解（なければ空）
            "follows": self._is_answer(ctx, path) if answers else None,  # 選んだものがプロファイルの正解に沿うか
            "deliberate": run.wrong_left.get(ctx, 0) > 0,  # わざと外れを答えるつもりだった
            "locale_off": self._locale_off(),
        }, self.launch))

    def _judged(self) -> None:
        """答えの当たり外れが分かった。わざと答えた外れなのに先へ進んだら、また正解に沿うはずの答えなのに同じ質問に
        戻ったら、「ハーネスの記録」に書く（後者は、同じ組み合わせで1回だけ）。"""
        run, tracker = self.run, self.run.flow
        for (ctx, path), right in tracker.judged:
            shown = " → ".join(self._text_of(p) or p for p in path)
            self.publish(Event("harness", "judged", {
                "flow": ctx[0], "question": ctx[1], "picked": [self._text_of(p) or p for p in path], "right": right,
            }, self.launch))
            if right and (ctx, path) in run.wrong_given:
                tracker.events.append(
                    f"流れ「{ctx[0]}」の `{ctx[1]}` で、外れのはずの答え（{shown}）のあと、同じ質問に戻らずに先へ進んだ"
                    "（プロファイルの正解が古いか、ゲームの判定の不具合の可能性がある）")
            elif (not right and (ctx, path) not in run.wrong_given and (ctx, path) not in run.wrong_noted
                    and self._is_answer(ctx, path)):
                run.wrong_noted.add((ctx, path))
                tracker.events.append(
                    f"流れ「{ctx[0]}」の `{ctx[1]}` で、正解のはずの答え（{shown}）のあと、同じ質問に戻った。この組み合わせは"
                    "このランでは選び直さない（プロファイルの正解が古いか、ハーネスが選んだものの文字を読み違えたか、ゲームの判定"
                    "の不具合の可能性がある）")
        tracker.judged.clear()

    def _advice(self, pick: set[str], texts: dict[str, str]) -> set[str]:
        """先に試す候補のうち、本文の文脈にいちばん合うと AI が答えたもの。答えがまだ・聞かない目的なら空。"""
        run, tracker = self.run, self.run.flow
        if run.advisor is None or not self.cfg["ai_choice"] or len(pick) < 2 or tracker.ctx is None:
            return set()
        texts = {o: t for o, t in texts.items() if o in pick}
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

        return [op.scaled("intent", f) if detour(op) else op for op in cands]

    # --- 続きから遊ぶ（fuzz/resume.py） ---

    def _start_resume(self) -> None:
        """ラン中にゲームを起動し直したら（終了ボタンを確かめた・再起動した）、人がするように続きから遊ぶ。
        ロードするか本文が始まるか、resume_sec たったら終える。ランの最初の起動では、はじめから遊ぶ
        （--continue なら、最初の起動でも前のランのセーブの続きを遊ぶ）。"""
        run = self.run
        run.resume_load = None
        if (self.launch == 0 and not run.continue_previous) or float(self.cfg["resume_sec"]) <= 0:
            return
        run.resume_until = time.monotonic() + float(self.cfg["resume_sec"])
        run.resume_pages = 0
        run.resume_texts = {}

    def _end_resume(self, how: str) -> None:
        self.run.resume_until = None
        why = "前のランの続きから遊ぶ（--continue）ので" if self.launch == 0 else "起動し直したので"
        self.run.notes.append(f"{why}続きから遊ぼうとした: {how}（起動 #{self.launch}）")

    def _apply_resume(self, cands: list[Op], now: float) -> list[Op]:
        """続きから遊ぶあいだは、「続きから」のボタンと、いちばん新しいセーブ済みのスロットを先に押し、「はじめから」を
        後回しにする。見えているスロットがどれも空（文字で分かる）なら、ページを送るボタンを先に押す。
        続きから遊ぶとき以外は、セーブ済みのスロットに saved_slot_factor を掛ける（既定 0 で押さない。自分のセーブのロードは
        目的があるときだけにする。run 2026-11-06_1012 で、いまの章から前の章のセーブをロードして 12 分やり直した）。"""
        run, hints = self.run, self.run.save_hints
        if run.resume_until is not None and now >= run.resume_until:
            self._end_resume(f"{float(self.cfg['resume_sec']):g} 秒のうちにロードも本文の始まりもなかった")
        targets = [op.target for op in cands if op.target is not None and op.target.kind == "button"]
        saved = [t for t in targets if hints.saved(t)]
        run.slot_stems.update(_slot_stem(t.label) for t in saved)
        # 文字を読む前のスロットは、セーブ済みかどうか分からない。ページを送った直後に押して古いセーブをロードしないように、
        # セーブ済みと同じように扱う（文字は画面が落ち着けば読める）
        unread = {t.label for t in targets if not t.text and _slot_stem(t.label) in run.slot_stems}
        if run.resume_until is None:
            low = float(self.cfg["saved_slot_factor"])
            labels = {t.label for t in saved} | unread
            if not labels or low == 1.0:
                return cands
            if low <= 0:
                return [op for op in cands if not _presses(op, labels)]
            return [op.scaled("saved_slot", low)
                    if op.target is not None and op.target.label in labels else op for op in cands]
        boost, low = float(self.cfg["resume_factor"]), float(self.cfg["resume_new_game_factor"])
        # ロードしても進めなかったセーブ（同じスロットに同じ文字）は選ばない。選ぶと、起動し直すたびに同じところで止まる
        # （run 2026-11-11_2135: 止まった状態でセーブしたスロットを 14 回ロードし、再起動の上限で打ち切った）
        bad = {t.label for t in saved if t.label in run.bad_saves and run.bad_saves[t.label] == t.text}
        saved = [t for t in saved if t.label not in bad]
        run.resume_texts = {t.label: t.text for t in saved}
        newest = newest_slots({t.label: t.text for t in saved}, time.time()) or {t.label for t in saved}
        # 選ばないセーブは、ページを送るかを決めるときは空のスロットと同じに扱う
        empty = inactive(targets, run.inactive_words, run.masked_chars) | bad
        # 文字を読めたのに日時がないスロットも、ロードするセーブではない。空を表す語（Empty）の読み違いで空と分からない
        # スロットが1つでもあると、ページを送らずに「はじめから」を押す（run 2026-11-11_1007: 9 個のうち 2 個を
        # 「Emply」と読み、セーブのある 3 ページ目以降を探さなかった）
        empty |= {t.label for t in targets if t.text and not hints.saved(t)}
        paging = not saved and bool(stuck_rows(self.state, empty))
        # 次のページがなければ探せないので、見えているものでよいとする
        if saved and any(hints.next_page(t) for t in targets) and \
                self._older_than_own_save(newest, {t.label: t.text for t in saved}):
            # 見えているセーブは、どれもこのランで最後にセーブしたときより古い。人は自分がどこにセーブしたかを覚えていて、
            # ほかのページを探す（run 2026-11-06_2157: 22:44 に 16 番へセーブしたのに、起動し直したあと 1 ページ目の
            # 22:00 のセーブをロードし、44 分前に戻った）
            newest, paging = set(), True
        if paging:
            # ページを送って探すと決めたら、一覧の探索（台帳を埋める・向きを決めずに探す）はしない。起動し直した直後は
            # 台帳が空で、空のスロットが一覧の目標（重み list_factor）になり、ページ送りより先に選ばれる
            # （run 2026-11-10_0957: ページを送ると決めたのに、空のスロットを押す一覧の探索が 97% で選ばれ、ページを
            # 一度も送らないまま「はじめから」を押した）
            cands = [op for op in cands if op.by not in ("list", "blind")]
        # いちばん新しいセーブ以外は押さない。一覧の探索（list）の重みで古いセーブを選ぶと、何章も戻る
        # （run 2026-11-09_1358: 15:58 のセーブがあるのに 14:54 のセーブをロードし、5 章戻って 30 分やり直した）
        stale = ({t.label for t in saved} - newest) | unread | bad
        out = []
        for op in cands:
            if _presses(op, stale):
                continue
            t = op.target
            f = 1.0
            if t is not None and t.kind == "button":
                if t.label in newest or hints.resume(t) or (paging and hints.next_page(t)):
                    f = boost
                elif run.guard.book.role_of(t) == "new_game":
                    f = low
            out.append(op.scaled("resume", f))
        return out

    def _older_than_own_save(self, newest: set[str], texts: dict[str, str | None]) -> bool:
        """見えているいちばん新しいセーブが、このランで最後にセーブした時刻より古いか（resume_page_limit 回ページを送っても
        見つからなければ、見えているものでよいとする）。"""
        run = self.run
        if run.saved_wall is None or not newest or run.resume_pages >= int(self.cfg["resume_page_limit"]):
            return False
        slack = float(self.cfg["resume_stamp_slack_sec"])
        keys = [k for label in newest if (k := stamp_key(texts.get(label))) is not None]
        if not keys or not all(older_than(k, run.saved_wall, slack) for k in keys):
            return False
        at = time.strftime("%H:%M:%S", time.localtime(run.saved_wall))
        note = (f"続きから遊ぶとき、見えているセーブがどれも最後にセーブした時刻（{at}）より古かったので、ページを送って探した"
                f"（起動 #{self.launch}）")
        if note not in run.notes:
            run.notes.append(note)
        return True

    # --- 終了を試す前にセーブする ---

    def _saved_for_exit(self, now: float) -> bool:
        """終了ボタンを試してよいか。起動し直したら続きから遊べるように、save_before_exit_sec のうちにセーブしていなければ、
        先に save_first_sec のあいだセーブしようとする（run 2026-11-06_1646: 22 分セーブしないまま終了ボタンを試し、
        起動し直したらロードするセーブがなく、はじめから遊び直した）。流れの途中（質問に答えているあいだ）は待つ。"""
        run, fresh = self.run, float(self.cfg["save_before_exit_sec"])
        if fresh <= 0 or run.resume_until is not None:
            return True
        if run.exit_unsaved and run.flow.active is not None:
            # セーブできないまま終了を試すことにしたが、流れの途中では試さない（流れが終わるのを待つ）
            return False
        if run.saved_t is not None and now - run.saved_t <= fresh:
            return True
        if run.save_first_until is None:
            if run.flow.active is not None:
                return False
            run.save_first_until, run.save_first_checked = now + float(self.cfg["save_first_sec"]), now
            return False
        if run.flow.active is not None:
            # 流れの途中の時間は数えない。流れの操作が先に選ばれてセーブの操作は選ばれず、流れの途中で時間切れになって
            # 終了を試した（run 2026-11-10_0957: 流れの外にいた一瞬に数え始め、そのあとずっと流れの途中で、一度もセーブ
            # しないまま流れの途中で終了を試し、約 2 時間前のセーブに戻った）
            run.save_first_until += now - (now if run.save_first_checked is None else run.save_first_checked)
            run.save_first_checked = now
            return False
        run.save_first_checked = now
        if now < run.save_first_until:
            return False
        run.notes.append(f"終了ボタンを試す前に {float(self.cfg['save_first_sec']):g} 秒セーブしようとしたが、セーブできなかった"
                         f"（起動 #{self.launch}。起動し直したら、はじめから遊ぶことがある）")
        run.save_first_until = None
        run.saved_t = now  # あきらめた。次の終了の間隔まで、セーブしようとしない
        run.exit_unsaved = True
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
        return [op.scaled("save_first", boost)
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
        if rule not in RECOVERABLE:
            return False
        if int(self.cfg["recover_ops"]) <= 0 or self._exiting:
            self._blame_resume_save(rule)
            return False

        def fail() -> None:
            self._blame_resume_save(rule)
            on_fail()

        if self._recovery is None:
            self._recovery = {
                "rule": rule, "left": int(self.cfg["recover_ops"]), "since": self.state.last_progress_t,
                "ops": [], "on_fail": fail,
            }
        return True

    def _blame_resume_save(self, rule: str) -> None:
        """続きから遊ぶためにロードしたあと、本文が1行も出ないまま進めなくなって再起動するなら、そのセーブを覚えて、
        このランで続きから遊ぶときに選ばない。"""
        run = self.run
        if run.resume_load is None:
            return
        label, text = run.resume_load
        run.resume_load = None
        run.bad_saves[label] = text
        # このランで最後にセーブしたのがこのセーブなら、ほかのページに自分のセーブを探しに行かない
        key = stamp_key(text)
        if run.saved_wall is not None and key is not None and \
                not older_than(key, run.saved_wall, float(self.cfg["resume_stamp_slack_sec"])):
            run.saved_wall = None
        run.notes.append(f"続きから遊ぶためにロードしたセーブ（{label}「{text}」）のあと、本文が出ないまま {rule} で"
                         f"再起動するので、このランでは続きから遊ぶときにこのセーブを選ばない（起動 #{self.launch}）")
        self.publish(Event("harness", "bad_save", {"label": label, "text": text, "rule": rule}, self.launch))

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
        cands += [Op("key", arg="esc", by="recover"), Op("right_click", by="recover"), Op("advance", by="recover")]
        seen: dict[str, Op] = {}
        for op in cands:
            if op.kind in ("click", "key", "right_click", "advance", "drag", "wheel_down", "wheel_up"):
                seen.setdefault(op.label, dataclasses.replace(op, weight=1.0, novelty=True, group=None, by="recover", why=()))
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

    # --- 一覧の台帳と目標（fuzz/listing.py） ---

    def _sync_lists(self) -> None:
        """台帳をいまの画面に合わせる。閉じた画面の台帳を events.jsonl に残し、閉じた寄り道を忘れる。"""
        run = self.run
        for name in [n for n in run.opened_by if n not in self.state.menus]:
            del run.opened_by[name]  # 閉じた
            run.opened_press.pop(name, None)
        for menu in run.ledger.closed(set(self.state.menus)):
            for label in [k for k in run.quiet_seen if k.split("/", 1)[0] == menu]:
                del run.quiet_seen[label]
            records = run.ledger.records(menu)
            if records:
                self.publish(Event("harness", "listing", {"menu": menu, "items": records}, self.launch))
        self._rows = {r.key: r for r in run.ledger.sync(self.state, run.masked_chars)}
        self._check_moving()

    def _check_moving(self) -> None:
        """目標へ向かって動かしたあと、目標が表示範囲に近づいたかを見る。近づかなかった手段は後回しにし、どの手段でも
        続けて list_move_give_up 回近づかなければ、目標に届かないとして、その画面を閉じるまで目標から外す。"""
        if self._moving is None:
            return
        run = self.run
        key, label, how, before = self._moving
        self._moving = None
        row, goal, item = self._rows.get(key), run.ledger.goals.get(key), run.ledger.items.get(label)
        if row is None or goal is None or goal.label != label or item is None:
            return
        now = distance(item.rect, row.view)
        goal.dist = now
        if now < before - 1e-3 or self._visible(label):
            goal.misses = 0
            run.ledger.dead.pop((row.menu, how), None)
            return
        goal.misses += 1
        run.ledger.dead[(row.menu, how)] += 1
        if goal.misses >= int(self.cfg["list_move_give_up"]):
            run.ledger.unreachable.add(label)
            del run.ledger.goals[key]
            run.ledger.filling.discard(key)
            self._note_search(row.menu, f"`{row.key}` の一覧で、目標の `{label}` へ {goal.misses} 回動かしても近づかなかった"
                                        "ので、届かないとして、ほかの項目を目標にした", final=False, tag="unreachable")

    def _visible(self, label: str) -> bool:
        menu = self.state.menus.get(label.split("/", 1)[0])
        return menu is not None and any(b.label == label and b.visible for b in menu.buttons)

    def _list_pressed(self, label: str, responded: bool) -> None:
        """押した結果を台帳に書き、押した項目が目標なら、次の目標を決め直す。"""
        run = self.run
        # 押して反応したことのあるもの（前のランで先へ進んだものも）は、続けて answered_miss_limit 回反応しなければ押せない。
        # 一度も反応したことのないものは、1回で押せないとする（_quiet と同じ）
        known = label in run.answered or label in run.experience.progressed_targets()
        limit = int(self.cfg["answered_miss_limit"]) if known else 1
        run.ledger.pressed(label, responded, run.miss_streak[label], limit)
        for key, goal in list(run.ledger.goals.items()):
            if goal.label == label:
                del run.ledger.goals[key]

    def _list_ops(self, cands: list[Op]) -> list[Op]:
        """流れの外で、一覧ごとに目標を決め、そこへ向かう操作（DESIGN.md §9「一覧の台帳と目標」）。

        目標の決め方（上から順に、当てはまったもの）:
        1. 前は暗かったのに明るくなった（新しくアンロックされた）項目があれば、それを押しにいく
        2. 画面を開いてからまだ見えていない項目があれば、台帳を埋める（いちばん近いものを見えるところまで動かす。押さない）
        3. ハーネスがボタンで開いた寄り道の画面なら、目標を持たない（押せるものがないのが正常なこともあるため）
        4. ゲームが開いた画面で、見えている項目に押せそうなもの（押せた、または見た目で押せそうな未確認）がなければ、
           画面の外の押せたもの → 見た目で押せそうな未確認 → 未確認、の順にいちばん近いものを押しにいく
        矩形が来ず、ページ送りのボタンも見つからない一覧（画面の外の項目がない）で、ゲームが開いた画面の見えている項目が
        どれも押せないときだけ、向きを決めずにすべての向きへ動かして探す（blind_search_limit 回まで）。"""
        run = self.run
        self._sync_lists()
        self._blind, self._list_row = {}, {}
        quiet = self._quiet() | run.silent
        ops: list[Op] = []
        for key, row in self._rows.items():
            goal = self._pick_goal(row, quiet)
            if goal is None:
                run.ledger.goals.pop(key, None)
                continue
            ops += self._toward(row, goal, "list", run.ledger.goals[key].purpose)
        offscreen = {r.menu for r in self._rows.values() if any(not self._visible(label) for label in r.labels)}
        for menu, _name, point in stuck_rows(self.state, quiet):
            if menu in offscreen or menu in run.opened_by:
                continue
            if run.blind_tries[menu] >= int(self.cfg["blind_search_limit"]):
                self._note_search(menu, f"`{menu}` はゲームが開いた画面で、見えている項目がどれも押せず、すべての向きへ"
                                        f"{run.blind_tries[menu]} 回動かしても押せるものが見つからなかった（進めない可能性がある）")
                continue
            self._note_search(menu, f"`{menu}` はゲームが開いた画面で、見えている項目がどれも押せず、画面の外の項目も"
                                    "分からないので、すべての向きへ動かして押せるものを探した", final=False)
            w = float(self.cfg["weights"]["click"]) * run.screen_weights.get(menu, 1.0)
            for op in blind_candidates(point, w):
                ops.append(op)
                self._blind.setdefault(op.label, []).append(menu)
        return ops

    def _pick_goal(self, row: Row, quiet: set[str]) -> str | None:
        """流れの外での、一覧の目標（_list_ops の 1〜4）。決めたら ledger.goals に置く（同じ目標なら近づき具合を引き継ぐ）。"""
        run, ledger = self.run, self.run.ledger
        usable = [label for label in row.labels if label not in ledger.unreachable
                  and ledger.items[label].status not in (NO, "masked")]
        bright = [label for label in usable if label in run.newly_bright]
        unshown = ledger.unshown(row)
        cur = ledger.goals.get(row.key)
        keep = cur.label if cur is not None and cur.by == "list" else None
        if keep in usable and cur is not None and cur.purpose == "pick" and keep not in run.silent:
            return keep  # 押しにいっている途中の目標は、押すまで続ける（見えて押せるようになったら押す）
        # いまの目標が同じ組にあれば続ける（近さで入れ替えると、逆の向きへ動かして行き来する）
        label, purpose = (keep if keep in bright else ledger.nearest(row, bright)), "pick"
        if label is None and keep in unshown:
            label, purpose = keep, "fill"  # 見にいっている途中の項目を続ける
        if label is None and unshown:
            label, purpose = ledger.nearest(row, unshown), "fill"
        self._note_fill(row, label is not None and purpose == "fill")
        if label is None and row.menu in run.opened_by:
            self._note_search(row.menu, f"`{row.menu}` は `{run.opened_by[row.menu]}` で開いた寄り道の画面なので、台帳を"
                                        "埋めたら目標を持たない（押せるものがないのが正常なこともあるため）", tag="detour")
        if label is None and row.menu not in run.opened_by:
            visible = [lb for lb in usable if self._visible(lb)]
            if not any(ledger.items[lb].status == OK or lb not in quiet for lb in visible):
                hidden = [lb for lb in usable if not self._visible(lb)]
                for group in ([lb for lb in hidden if ledger.items[lb].status == OK],
                              [lb for lb in hidden if lb not in quiet],
                              [lb for lb in usable if ledger.items[lb].status == UNKNOWN and lb not in run.silent]):
                    # いまの目標が同じ組にあれば続ける（近さで入れ替えると、逆の向きへ動かして行き来する）
                    label = keep if keep in group else ledger.nearest(row, group)
                    if label is not None:
                        break
                if label is None:
                    self._note_search(row.menu, f"`{row.menu}` はゲームが開いた画面だが、`{row.key}` の一覧に押せるものが"
                                                "見つからなかった（進めない可能性がある）", tag="nothing")
        if label is None:
            return None
        goal = ledger.goals.get(row.key)
        if goal is None or goal.label != label:
            ledger.goals[row.key] = Goal(label, purpose, "list", distance(ledger.items[label].rect, row.view))
        return label

    def _note_fill(self, row: Row, filling: bool) -> None:
        """台帳を埋め始めた・埋め終えたことを「ハーネスの記録」に書く（画面ごとに1回ずつ）。"""
        ledger = self.run.ledger
        if filling and row.key not in ledger.filling:
            ledger.filling.add(row.key)
            self._note_search(row.menu, f"`{row.key}` の一覧で、見えていない項目を見にいき、台帳を埋め始めた",
                              final=False, tag="fill")
        elif not filling and row.key in ledger.filling and not ledger.unshown(row):
            ledger.filling.discard(row.key)
            self._note_search(row.menu, f"`{row.key}` の一覧の台帳を埋め終えた（{len(row.labels)} 項目）", tag="fill")

    def _toward(self, row: Row, label: str, by: str, purpose: str | None = None) -> list[Op]:
        """目標へ向かう操作。見えていて押せるなら押し（押すのが目的のとき）、見えていなければ向きを計算してそちらへ動かす。
        流れの目標（by が flow）を押す操作は、_apply_flow が手順のボタンとして重みを付けるので、ここでは出さない。"""
        run, ledger = self.run, self.run.ledger
        goal = ledger.goals.get(row.key)
        if purpose is None:
            purpose = goal.purpose if goal is not None and goal.label == label else "pick"
        if goal is None or goal.label != label or goal.by != by or goal.purpose != purpose:
            goal = ledger.goals[row.key] = Goal(label, purpose, by, distance(ledger.items[label].rect, row.view))
        factor = float(self.cfg["list_factor"])
        w = float(self.cfg["weights"]["click"]) * run.screen_weights.get(row.menu, 1.0)
        if self._visible(label):
            if goal.purpose == "fill" or by == "flow":
                return []  # 見えた（文字は画面が落ち着いたときに読む）。流れの目標は _apply_flow が押す
            menu = self.state.menus[row.menu]
            t = next(b for b in menu.buttons if b.label == label)
            op = Op("click", t, weight=w, group="list", by="list", goal=label).scaled("list", factor)
            self._list_row[op.label] = row.key
            return [op]
        d = direction(ledger.items[label].rect, row.view)
        if d is None:
            return []
        low = float(self.cfg["no_response_factor"])
        out = []
        for op in moves(row, d, w, label, by):
            op = op.scaled("list", factor).scaled("list_dead", low ** ledger.dead[(row.menu, op.label)])
            self._list_row[op.label] = row.key
            out.append(op)
        return out

    def _one_mover(self, cands: list[Op]) -> list[Op]:
        """目標を持っている一覧があれば、その画面を動かすほかの操作（モーダルでいつも試すドラッグ・ホイール・方向キー）を
        候補に入れない（目標と逆の向きへ動かして打ち消し合わないように）。Enter は動かす操作ではないので残す。
        目標を押す操作がある一覧の、ほかの項目を押す操作（ふだんの重み）はそのまま残す。"""
        if not any(op.goal is not None and op.target is None for op in cands):
            return cands
        return [op for op in cands if op.group != "move" or op.label == "key enter"]

    def _forget_progress(self, why: str) -> None:
        """ゲームの進み具合が入れ替わった（起動し直した、流れの外でセーブをロードした）。"""
        self.run.flow.forget_reveals(why)
        self._forget_ledger(why)

    def _forget_ledger(self, why: str) -> None:
        n = self.run.ledger.forget_progress()
        if n:
            self.publish(Event("harness", "listing_reset", {"why": why, "items": n}, self.launch))

    def _note_search(self, menu: str, text: str, final: bool = True, tag: str = "") -> None:
        """探し方を「ハーネスの記録」に書く。menu・行動（tag）ごとに、始めたときと終えたときの1回ずつ。"""
        key = f"{menu}:{tag}:{'end' if final else 'start'}"
        if key in self.run.search_noted:
            return
        self.run.search_noted.add(key)
        self.run.notes.append(f"{text}（起動 #{self.launch}）")

    def _trusted(self) -> set[str]:
        """押して反応したことのあるボタン（このランで反応した、前のランまでに押して先へ進んだ）のうち、続けて反応しなかった
        回数が answered_miss_limit 未満のもの。1回反応しなかっただけでは、反応しないボタンとして後回しにしない。

        続けて何度も反応しなければ、いまは押せない（ゲームの進み具合が変わって、開示済みだった思い出がロックされているなど）
        として、ほかのボタンと同じに後回しにする（run 2026-11-06_2050: 前に押して読めた思い出のサムネが、起動し直してロード
        したあとはロックされていて、反応しないのに約 7 分で 227 回押し続けた）。"""
        run = self.run
        limit = int(self.cfg["answered_miss_limit"])
        return {label for label in run.answered | run.experience.progressed_targets()
                if run.miss_streak[label] < limit}

    def _quiet(self) -> set[str]:
        """押しても反応しないと見込んでいるボタン（暗い、文字で空と分かる、押しても一度も反応しなかったもの・それと
        同じ見た目のもの）の label。一度も反応しなかったボタンは S-05 の対象でないので、押し直しても待つ時間は短くてよい。"""
        # 前のランまでに押して先へ進んだボタン（タイトルの「はじめから」など）は、1回反応しなかっただけでは後回しにしない
        # （画面が切り替わる途中に押して反応しなかったなど。run 2026-11-06_1830: 起動直後に「はじめから」を1回押して反応せず、
        # 以後ほとんど押さずに、設定やロード画面の開け閉めを 4 分続けた）
        silent = self.run.silent - self._trusted()
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

    def _note_silent(self, t: Target, responded: bool) -> None:
        """押した結果で、反応しなかったときの見た目と文字を覚える（反応したら忘れる）。"""
        run = self.run
        if responded:
            run.silent_look.pop(t.label, None)
            run.silent_letters.pop(t.label, None)
            return
        look = next((values[t.label] for values in self._values.values() if t.label in values), None)
        if look is not None:
            old = run.silent_look.get(t.label)
            run.silent_look[t.label] = look if old is None else (max(old[0], look[0]), max(old[1], look[1]))
        item = run.ledger.items.get(t.label)
        text = t.text or (item.text if item is not None else None)
        run.silent_letters[t.label] = max(run.silent_letters.get(t.label, 0), letters(text, run.masked_chars))

    def _revive_by_look(self, values: Values) -> None:
        """反応しなかったボタンが、反応しなかったあとに見たいちばん明るい見た目より、はっきり明るくなったら開示されたとみなす。
        そうでなければ、いちばん明るい見た目を更新する（後ろの絵の揺れで少しずつ明るく見えても、何度も戻さないように）。"""
        run = self.run
        ratio, gap = float(self.cfg["dim_ratio"]), float(self.cfg["dim_min_gap"])
        for label, v in values.items():
            if label not in run.silent:
                continue
            old = run.silent_look.get(label)
            if old is not None and brighter(v, old, ratio, gap):
                self._revive(label, f"明るくなった（{old[1]:.2f} → {v[1]:.2f}）")
            else:
                run.silent_look[label] = v if old is None else (max(old[0], v[0]), max(old[1], v[1]))

    def _revive_by_text(self) -> None:
        """反応しなかったボタンの、画面から読める文字が増えた（「？？？」が題名になった）ら開示されたとみなす。"""
        run = self.run
        need = int(self.cfg["revive_letters"])
        for t in self.state.targets():
            if t.kind != "button" or t.label not in run.silent or not t.text:
                continue
            n = letters(t.text, run.masked_chars)
            before = run.silent_letters.get(t.label, 0)
            # 伏せ字の文字が残っていれば、まだ開示されていない（後ろの本文を一緒に読んだだけ）
            if n - before >= need and not any(c in run.masked_chars for c in t.text):
                self._revive(t.label, f"文字が読めるようになった（「{t.text}」）")

    def _revive(self, label: str, why: str) -> None:
        """押しても反応しなかったボタンが、そのあと開示された。反応しないボタンから外し、押せるかを未確認に戻して、
        流れの候補・一覧の目標に入れ直す（run 2026-11-07_1511: 開示前に押して反応しなかった思い出「No.012」が、
        質問の時点では開示されていたのに候補から外れたままで、「試せる組み合わせをすべて試した」で止まった）。"""
        run = self.run
        run.silent.discard(label)
        run.silent_look.pop(label, None)
        run.silent_letters.pop(label, None)
        run.miss_streak.pop(label, None)
        item = run.ledger.items.get(label)
        if item is not None and item.status == NO:
            item.status = UNKNOWN
        run.ledger.unreachable.discard(label)
        if label not in run.revived_noted:
            run.revived_noted.add(label)
            run.notes.append(f"`{label}` は押しても反応しなかったが、そのあと{why}ので、開示されたとみなして"
                             f"押せるものに戻した（起動 #{self.launch}）")

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
        return out

    async def _look(self) -> None:
        """画面が落ち着いたら、同じ種類に見えるボタンの並びの明るさを比べ、暗いものを覚える（押す順番を後回しにする）。

        比べる相手は、同じときに撮った画像の中の、同じ並びでいちばん明るいもの。ランのあいだに見たいちばん明るいものとは
        比べない（半透明の UI は後ろの絵で明るさが変わり、別のときに撮ったものとは比べられないため）。
        前は暗かったのに、ほかに暗いものが残っている並びで明るくなったボタン（新しくアンロックされたもの）は、ランで1回だけ先に押す。
        """
        self._dim, self._disabled, self._values = set(), set(), {}
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
            self._revive_by_text()
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
            self._revive_by_look(values)
            # 並びがすべて同じ明るさなら、暗い・明るいは分からないので、前の記録を残す
            for label in values if dk else ():
                now = label in dk
                # 文字で空と分かるもの（空のセーブスロットなど）は、明るさが揺れて明るく見えても先に押さない
                # （run 2026-11-06_1830: 空のスロットを「明るくなった」として次々に先に押し、ロード画面で空のスロットを押し続けた）
                empty = text_inactive(texts.get(label), run.inactive_words, run.masked_chars)
                if run.dim_history.get(label) and not now and label not in run.bright_noted and not empty:
                    run.bright_noted.add(label)
                    run.newly_bright.add(label)
                    run.notes.append(f"`{label}` は前に見たときは暗かったのに明るくなったので、一覧の目標にして先に押す（起動 #{self.launch}）")
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

    def _verify_candidates(self, cands: list[Op], key: str) -> list[Op]:
        """応答確認に使える操作: 同じ状態で前に反応があったもの。

        画面を動かす操作（方向キー・Enter・ドラッグ・ホイール。一覧を探す操作も）は、フォーカスやスクロール位置で
        結果が変わるので使わない。いま押しても反応しないと見込んでいるボタン（暗い、文字で空と分かる）と、
        この応答確認ですでに反応がなかった操作も使わない（run 2026-11-12_0959: セーブで反応したスロットを、
        ロードの画面で空きのスロットとして押して S-01 にした）。"""
        quiet = self._quiet()
        return [op for op in cands
                if op.group not in ("move", "list", "search") and (key, op.label) in self.run.responsive
                and op.label not in self._verify_tried and not (op.target is not None and op.target.label in quiet)]

    def _recheck(self, op: Op, key: str, verify: bool, responded: bool, commands_ran: bool, interfered: bool) -> bool:
        """応答確認の操作に反応がなかったら、前に反応があった別の操作で、もう一度確かめるか（そのときは S-01 を出さない）。

        同じ画面でも、モード・スクロール位置・スロットの中身で、前に反応した操作が今は何も起こさないことがある
        （run 2026-11-12_0959: すでに選ばれているタブ、端までスクロールした一覧のスクロールバー、空きのスロット）。
        反応がなかった操作は、この状態では応答確認に使わない。"""
        if not verify:
            return False
        if responded:
            self._verify_tried = []
            return False
        self.run.responsive.discard((key, op.label))
        self._verify_tried.append(op.label)
        if commands_ran or interfered or len(self._verify_tried) >= self.run.response_checks:
            return False
        cands = normal_candidates(self.state, self.run.guard, self.cfg["weights"], False)
        if not self._verify_candidates(cands, state_key(self.state)):
            return False
        self._verify_next = True
        return True

    async def _normal(self, op: Op, key: str, verify: bool) -> None:
        before = self.state.signature()
        screens_before = self.state.screens()
        menus_before = set(self.state.menus)
        saves_before = self.state.saves
        commands_before = self.state.commands
        settings_before = self.state.settings_changes
        locale_before = (self.state.settings or {}).get("locale")
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
        if op.group in ("move", "list", "search") and op.target is None and not verify:
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
            # 選択肢は、画面が変わっただけでなく、ゲームがその選択肢を選んだと知らせたとき（choices_done）だけ、答えたとする
            picked = responded and (t.kind != "choice" or self.state.chosen == t.name)
            tracker.pressed(t.label, t.name, picked, t.text if t.kind == "choice" else None,
                            t.text if t.kind == "button" else None)
            if sum(st.completed for st in tracker.stats.values()) > done:
                self._note_flow(before, (None, 0))  # 最後まで進めた（events.jsonl に流れの終わりを残す）
                self._answered()
                self._maybe_edge("answer")
        if responded:
            self.run.responsive.add((key, op.label))
        else:
            self.run.stats["no_response"] += 1
        if op.target is not None:
            self._verify_behind(op.target, responded)
        if op.target is not None and op.target.kind == "button":
            (self.run.silent.discard if responded else self.run.silent.add)(op.target.label)
            self._note_silent(op.target, responded)
            if responded:
                self.run.flow.seen(op.target.label, op.target.name, op.target.text)
            if responded and self.run.resume_until is not None and self.run.save_hints.next_page(op.target):
                self.run.resume_pages += 1
            if responded:
                self.run.miss_streak.pop(op.target.label, None)
            else:
                self.run.miss_streak[op.target.label] += 1
            self._list_pressed(op.target.label, responded)
            if responded:
                self.run.answered.add(op.target.label)
            if not responded:
                for gkey, values in self._values.items():
                    if op.target.label in values:
                        self.run.locked_look[gkey] = values[op.target.label]
        if responded and op.target is not None:
            # ボタンで開いた画面は寄り道（一覧の目標の決め方を変える）。押す前から開いていた画面（ボタンのない・モーダルで
            # ない状態で後ろに残っていて、ボタン付きで出し直されたもの）は、押したボタンで開いたとしない
            # （run 2026-11-07_0849: 思い出を読むあいだボタンなしで残っていた、ゲームが開いた思い出の一覧を、要約を閉じたら
            # 「要約を閉じるボタンで開いた寄り道」とみなした）
            for name in self.state.screens()[1] - screens_before[1]:
                if name in self.state.menus and name not in menus_before:
                    self.run.opened_by[name] = op.target.label
                    self.run.opened_press[name] = press
                    self.run.blind_tries.pop(name, None)
        if op.group == "search":
            for menu in self._blind.get(op.label, []):
                self.run.blind_tries[menu] += 1
        if op.goal is not None and op.target is None and op.label in self._list_row:
            # 目標へ向かって動かした。近づいたかは、次に画面が落ち着いたときに見る（_check_moving）
            key = self._list_row[op.label]
            row, item = self._rows.get(key), self.run.ledger.items.get(op.goal)
            if row is not None and item is not None:
                self._moving = (key, op.goal, op.label, distance(item.rect, row.view))
        locale_now = (self.state.settings or {}).get("locale")
        if (op.target is not None and op.target.kind == "button" and isinstance(locale_now, str)
                and locale_before is not None and locale_now != locale_before):
            self._locale_switched(op.target, locale_now)
        hb_age = time.monotonic() - self.state.last_game_t if self.state.last_game_t is not None else None
        commands_ran = self.state.commands != commands_before
        interfered = self._irregular_t >= started
        recheck = self._recheck(op, key, verify, responded, commands_ran, interfered)
        tried_before = self._verify_tried[:-1] if verify and not responded else []
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
                    # 押してゲームの設定（settings）が変わった。変わった後の値は settings
                    "settings_changed": self.state.settings_changes != settings_before,
                    "settings": self.state.settings if self.state.settings_changes != settings_before else None,
                    # 待っているあいだに、スクリプトのコマンドが進んだ（本文のない演出の途中。S-01 はゲームが止まったとしない）
                    "commands_ran": commands_ran,
                    "quiet": quiet,
                    "responded": responded,
                    "waited": round(waited, 3),
                    "verify": verify,
                    # 反応がなかったので、別の操作でもう一度確かめる（S-01 はそちらで判定する）
                    "recheck": recheck,
                    # この応答確認で、先に押して反応がなかった操作
                    "verify_tried": tried_before,
                    "interfered": interfered,
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
            self._verify_tried = []

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
            self._verify_tried = []
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
        self._verify_tried = []

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
        self._verify_tried = []

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
        data: dict[str, Any] = {**op.to_json(), "trigger": trigger, "pos": pos}
        if self._odds is not None and self._odds[0] == op.label and trigger is None:
            data["odds"] = self._odds[1]  # 選ばれた確率と、確率の高かった候補（どの候補と競り合っていたか）
        self._odds = None
        self.publish(Event("harness", "op", data, self.launch))
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
