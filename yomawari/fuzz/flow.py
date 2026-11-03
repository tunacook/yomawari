"""ゲームの進め方（DESIGN.md §9「ゲームの流れ」）。

ゲームのプロファイルの [[flow]] に、人がこのゲームの遊び方を説明するように、ひと続きの手順を書く
（例: Rachel の「質問に答える」: 質問で思い出を選ぶ → 思い出を読む → 言葉（フラグメント）を選ぶ → この言葉で答える）。
ハーネスは、始まりの条件（start）に当てはまったら流れを追い始め、いまの手順の画面で押すボタン（press）や
本文送り（advance）、選択肢を選んで答えること（choose）の重みを上げ、流れの途中で押すと流れから外れるボタン（avoid）の重みを下げる。

手順は、いまの手順から後ろへ順に見て、最初に当てはまったものに進む（同じ画面が2回出てくる流れでも、
読んだあとの要約では「言葉を選ぶ」のように、手順の順番で区別する）。最後の手順で押して反応があれば、流れを
最後まで進めたとする。最後の手順が押す手順でない（本文を最後まで読むなど）ときは、その条件から外れたら最後まで
進めたとする。流れのどの画面・scene にも当てはまらない状態が flow_idle_sec 続いたら、途切れたとする。

条件は、画面（screen）・scene・選択肢が出ていること（choices）で書く。画面（screen）は、いま押せる画面（手前のモーダル、なければボタンの見えている画面）の名前で見る。ボタンの名前は
"<menu>/<name>" か "<name>" で、* などのワイルドカードを使える。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from ..config import ConfigError
from ..state import GameState


@dataclass(frozen=True)
class Cond:
    screen: str | None = None  # いま押せる画面の名前
    scene: str | None = None  # scene の名前（ワイルドカード可）
    by: str | None = None  # game: ゲームが開いた画面 / harness: ハーネスがボタンで開いた画面
    choices: bool = False  # 選択肢が出ている

    def match(self, state: GameState, opened_by: dict[str, str]) -> bool:
        if self.choices and not state.choices:
            return False
        if self.screen is not None:
            if self.screen not in front_screens(state):
                return False
            if self.by == "game" and self.screen in opened_by:
                return False
            if self.by == "harness" and self.screen not in opened_by:
                return False
        if self.scene is not None and not (state.scene and fnmatchcase(state.scene, self.scene)):
            return False
        return self.screen is not None or self.scene is not None or self.choices


@dataclass(frozen=True)
class Step:
    when: Cond
    press: tuple[str, ...] = ()  # 押すボタン。空で advance でもなければ、その画面のボタンならどれでもよい
    advance: bool = False  # 本文を送る
    # 選択肢を選んで答える。文字のワイルドカードを書けば、その本文の選択肢だけ（空の配列ならどれでもよい）
    choose: tuple[str, ...] | None = None
    note: str = ""


@dataclass(frozen=True)
class Flow:
    name: str
    start: Cond
    steps: tuple[Step, ...]
    avoid: tuple[str, ...] = ()  # 流れの途中で押すと流れから外れるボタン

    def screens(self) -> set[str]:
        return {c.screen for c in (self.start, *(s.when for s in self.steps)) if c.screen}


def front_screens(state: GameState) -> set[str]:
    """いま押せる画面の名前。"""
    top = state.top_modal()
    if top is not None:
        return {top.name}
    return {m.name for m in state.menus.values() if m.visible_buttons and m.name not in state.behind}


def matches(target_label: str, target_name: str, patterns: tuple[str, ...]) -> bool:
    """ボタン（"<menu>/<name>#n" の label と、name）が、プロファイルに書いたボタンの名前のどれかに当てはまる。"""
    base = target_label.split("#", 1)[0]
    return any(fnmatchcase(base, p) or fnmatchcase(target_name, p) for p in patterns)


def _cond(raw: Any, where: str) -> Cond:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} は表にしてください（screen / scene / by / choices）")
    unknown = sorted(set(raw) - {"screen", "scene", "by", "choices"})
    if unknown:
        raise ConfigError(f"{where}: 知らない項目です: {', '.join(unknown)}（screen / scene / by / choices）")
    by = raw.get("by")
    if by not in (None, "game", "harness"):
        raise ConfigError(f"{where}.by は game か harness にしてください: {by}")
    cond = Cond(raw.get("screen"), raw.get("scene"), by, bool(raw.get("choices", False)))
    if cond.screen is None and cond.scene is None and not cond.choices:
        raise ConfigError(f"{where} には screen・scene・choices のどれかを書いてください")
    return cond


def _names(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{where} はボタンの名前（文字列）か、その配列にしてください")
    return tuple(raw)


STEP_KEYS = ("screen", "scene", "by", "choices", "press", "advance", "choose", "note")


def parse_flows(raw: Any) -> list[Flow]:
    """プロファイルの [[flow]] を読む。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("flow は [[flow]] の配列にしてください")
    flows = []
    for n, f in enumerate(raw):
        where = f"flow[{n}]"
        if not isinstance(f, dict):
            raise ConfigError(f"{where} は表にしてください")
        unknown = sorted(set(f) - {"name", "start", "steps", "avoid"})
        if unknown:
            raise ConfigError(f"{where}: 知らない項目です: {', '.join(unknown)}（name / start / steps / avoid）")
        name = str(f.get("name") or f"flow{n}")
        steps = []
        for i, s in enumerate(f.get("steps") or []):
            sw = f"{where}.steps[{i}]"
            if not isinstance(s, dict):
                raise ConfigError(f"{sw} は表にしてください")
            unknown = sorted(set(s) - set(STEP_KEYS))
            if unknown:
                raise ConfigError(f"{sw}: 知らない項目です: {', '.join(unknown)}（{' / '.join(STEP_KEYS)}）")
            choose = s.get("choose")
            if choose is True:
                choose = ()
            elif choose is False:
                choose = None
            elif choose is not None:
                choose = _names(choose, f"{sw}.choose")
            # 選択肢を選ぶ手順は、条件を書かなければ「選択肢が出ている」とする
            cond_raw = {k: s[k] for k in ("screen", "scene", "by", "choices") if k in s}
            if choose is not None and not cond_raw:
                cond_raw = {"choices": True}
            when = _cond(cond_raw, sw)
            steps.append(Step(when, _names(s.get("press"), f"{sw}.press"), bool(s.get("advance", False)),
                              choose, str(s.get("note", ""))))
        if not steps:
            raise ConfigError(f"{where}（{name}）に steps がありません")
        flows.append(Flow(name, _cond(f.get("start"), f"{where}.start"), tuple(steps),
                          _names(f.get("avoid"), f"{where}.avoid")))
    return flows


@dataclass
class FlowStats:
    started: int = 0
    completed: int = 0
    furthest: int = 0  # いちばん先まで進んだ手順（1 始まり）
    broken: list[str] = field(default_factory=list)  # 途切れた手順（直近のもの）


class FlowTracker:
    """いま追っている流れと、その手順。"""

    def __init__(self, flows: list[Flow]) -> None:
        self.flows = flows
        self.active: Flow | None = None
        self.index = 0
        self.idle_since: float | None = None
        self.stats: dict[str, FlowStats] = {f.name: FlowStats() for f in flows}
        self.events: list[str] = []  # 「ハーネスの記録」向けに、流れを始めた・進めた・途切れたこと

    @property
    def step(self) -> Step | None:
        return self.active.steps[self.index] if self.active is not None else None

    def update(self, state: GameState, opened_by: dict[str, str], now: float, idle_sec: float) -> Step | None:
        """いまの状態から、流れの始まり・手順の進み・途切れを判定し、いまの手順を返す。"""
        if self.active is None:
            for f in self.flows:
                if f.start.match(state, opened_by):
                    self.active, self.index, self.idle_since = f, 0, None
                    self.stats[f.name].started += 1
                    break
            else:
                return None
        flow = self.active
        for i in range(self.index, len(flow.steps)):
            if flow.steps[i].when.match(state, opened_by):
                if i != self.index:
                    self.index = i
                st = self.stats[flow.name]
                st.furthest = max(st.furthest, i + 1)
                self.idle_since = None
                return flow.steps[i]
        last = flow.steps[-1]
        if self.index == len(flow.steps) - 1 and not last.press and last.choose is None and last.when.screen is None:
            # 最後の手順（本文を最後まで読むなど）の条件から外れた。押して終える手順がないので、ここで最後まで進めたとする
            self._complete("最後の手順の条件から外れた")
            return None
        # いまの手順より前の画面（流れの始まりの画面など）にいるなら、まだ流れの中
        if flow.start.match(state, opened_by) or any(s.when.match(state, opened_by) for s in flow.steps[: self.index]):
            self.idle_since = None
            return None
        if self.idle_since is None:
            self.idle_since = now
        elif now - self.idle_since >= idle_sec:
            self._break(f"手順 {self.index + 1}（{flow.steps[self.index].note or flow.steps[self.index].when}）で、"
                        "流れのどの画面でもなくなった")
        return None

    def pressed(self, label: str, name: str, responded: bool, choice_text: str | None = None) -> None:
        """押した結果。最後の手順のボタン（選択肢）で反応があれば、最後まで進めたとする。"""
        flow, step = self.active, self.step
        if flow is None or step is None or not responded or self.index != len(flow.steps) - 1:
            return
        if choice_text is not None:
            if step.choose is None or (step.choose and not any(fnmatchcase(choice_text, p) for p in step.choose)):
                return
        elif step.press and not matches(label, name, step.press):
            return
        self._complete(f"`{label}`")

    def _complete(self, how: str) -> None:
        assert self.active is not None
        self.stats[self.active.name].completed += 1
        self.events.append(f"流れ「{self.active.name}」を最後まで進めた（{how}）")
        self.active = None
        self.idle_since = None

    def title_returned(self) -> None:
        if self.active is not None:
            self._break("タイトルに戻った")

    def _break(self, why: str) -> None:
        assert self.active is not None
        st = self.stats[self.active.name]
        st.broken = (st.broken + [why])[-3:]
        self.active = None
        self.idle_since = None

    def summary(self) -> list[str]:
        out = []
        for f in self.flows:
            st = self.stats[f.name]
            if not st.started:
                out.append(f"流れ「{f.name}」は一度も始まらなかった（始まりの条件: {f.start}）")
                continue
            line = (f"流れ「{f.name}」: {st.started} 回始め、{st.completed} 回最後まで進めた。"
                    f"いちばん先まで進んだのは手順 {st.furthest}/{len(f.steps)}")
            if st.furthest:
                s = f.steps[st.furthest - 1]
                line += f"（{s.note or s.when}）"
            if st.broken:
                line += "。途切れたところ（直近）: " + " / ".join(st.broken)
            out.append(line)
        return out
