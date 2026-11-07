"""ゲームの進め方（DESIGN.md §9「ゲームの流れ」）。

ゲームのプロファイルの [[flow]] に、人がこのゲームの遊び方を説明するように、ひと続きの手順を書く
（例: Rachel の「質問に答える」: 質問で思い出を選ぶ → 思い出を読む → 言葉（フラグメント）を選ぶ → この言葉で答える）。
ハーネスは、始まりの条件（start）に当てはまったら流れを追い始め、いまの手順の画面で押すボタン（press）や
本文送り（advance）、選択肢を選んで答えること（choose）の重みを上げ、流れの途中で押すと流れから外れるボタン（avoid）の重みを下げる。

手順は、いまの手順から後ろへ順に見て、最初に当てはまったものに進む（同じ画面が2回出てくる流れでも、
読んだあとの要約では「言葉を選ぶ」のように、手順の順番で区別する）。最後の手順で押して反応があれば、流れを
最後まで進めたとする。最後の手順が押す手順でない（本文を最後まで読むなど）ときは、その条件から外れたら最後まで
進めたとする。流れのどの画面・scene にも当てはまらない状態が flow_idle_sec 続いたら、途切れたとする。

試行錯誤: 同じ質問（流れを始めたときの本文の行）で流れをもう一度始めたら、前に選んだ組み合わせ（手順ごとに押した
ボタン・選んだ選択肢の並び）は外れだったとみなす。人が「この思い出のこの言葉はだめだった」と覚えるように、
組み合わせを木として覚え、まだ試していない枝を選ぶ。いまの枝（同じ思い出）にまだ試していないものが残っていれば
そちらを先に試し、すべて試し終えた枝（言葉をすべて試した思い出）は後回しにする。一度済ませれば次からは要らない
手順（思い出を読むと言葉が開示される、など）には once = true を書く。同じ選び方で一度済ませた once の手順は飛ばし、
その手順のボタンを後回しにする（once の手順は、組み合わせには数えない）。

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
    once: bool = False  # 同じ選び方で一度済ませたら、次からは飛ばす手順
    text: tuple[str, ...] = ()  # 押すボタンを、画面に見える文字（画面から読んだもの）で書く。press と両方あればどちらか

    @property
    def targets(self) -> bool:
        """押すボタンを書いた手順か。"""
        return bool(self.press or self.text)

    @property
    def picks(self) -> bool:
        """いくつかの中から選ぶ手順か（選択肢、ワイルドカードで書いたボタン、ボタンを書いていない押す手順）。

        決まった1つのボタンを押すだけの手順（言葉を選ぶ・これにする）は、選び方が分かれないので組み合わせに数えない。
        数えると、その手順を通ったかどうか（ゲームが一覧を直接開いたなど）で、同じ組み合わせが別のものとして
        覚えられ、外れを何度も試す（run 2026-11-06_1012）。"""
        if self.choose is not None:
            return True
        if self.advance:
            return False
        if not self.targets:
            return True
        return any(c in p for p in (*self.press, *self.text) for c in "*?[")

    def presses(self, label: str, name: str, text: str | None) -> bool:
        """ボタンが、この手順で押すボタンか（名前か、画面に見える文字で）。"""
        return matches(label, name, self.press) or text_matches(text, self.text)


@dataclass(frozen=True)
class Flow:
    name: str
    start: Cond
    steps: tuple[Step, ...]
    avoid: tuple[str, ...] = ()  # 流れの途中で押すと流れから外れるボタン
    avoid_text: tuple[str, ...] = ()  # 同じく、画面に見える文字で書いたもの
    answers: tuple[Answer, ...] = ()  # 質問ごとの正解（DESIGN.md §9「答えの選び方」）

    def avoids(self, label: str, name: str, text: str | None) -> bool:
        return matches(label, name, self.avoid) or text_matches(text, self.avoid_text)

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


def _squash(s: str) -> str:
    return "".join(s.split())


def text_matches(text: str | None, patterns: tuple[str, ...]) -> bool:
    """画面に見える文字（画面から読んだもの）が、プロファイルに書いた文字のどれかに当てはまる（ワイルドカード可）。
    画面から読んだ文字は、行の折り返しや字間で空白が入ることがあるので、空白を除いて比べる。"""
    if not text or not patterns:
        return False
    t = _squash(text)
    return any(fnmatchcase(t, _squash(p)) for p in patterns)


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


ANSWER_KEYS = ("at", "pick", "note")


@dataclass(frozen=True)
class Answer:
    """プロファイルに書いた、質問ごとの正解（DESIGN.md §9「答えの選び方」）。

    at: 質問の本文の行（line_id。ワイルドカード可）。pick: 選ぶ手順ごとに、選ぶものの画面に見える文字（ワイルドカード可）。
    """

    at: str
    pick: tuple[str, ...]
    note: str = ""


def _answers(raw: Any, where: str) -> tuple[Answer, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where} は {{ at = ..., pick = [...] }} の配列にしてください")
    out = []
    for i, a in enumerate(raw):
        aw = f"{where}[{i}]"
        if not isinstance(a, dict):
            raise ConfigError(f"{aw} は表にしてください")
        unknown = sorted(set(a) - set(ANSWER_KEYS))
        if unknown:
            raise ConfigError(f"{aw}: 知らない項目です: {', '.join(unknown)}（{' / '.join(ANSWER_KEYS)}）")
        at = a.get("at")
        if not isinstance(at, str) or not at:
            raise ConfigError(f"{aw}.at に質問の本文の行（line_id。例: \"main/scenarioA_004:44.0\"）を書いてください")
        pick = a.get("pick")
        if not isinstance(pick, list) or not pick or not all(isinstance(x, str) and x for x in pick):
            raise ConfigError(f"{aw}.pick に、選ぶ手順ごとの選ぶものの文字を配列で書いてください（例: [\"No.050*\", \"屋上\"]）")
        out.append(Answer(at, tuple(pick), str(a.get("note", ""))))
    return tuple(out)


STEP_KEYS = ("screen", "scene", "by", "choices", "press", "text", "advance", "choose", "note", "once")


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
        unknown = sorted(set(f) - {"name", "start", "steps", "avoid", "avoid_text", "answers"})
        if unknown:
            raise ConfigError(f"{where}: 知らない項目です: {', '.join(unknown)}（name / start / steps / avoid / avoid_text / answers）")
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
                              choose, str(s.get("note", "")), bool(s.get("once", False)),
                              _names(s.get("text"), f"{sw}.text")))
        if not steps:
            raise ConfigError(f"{where}（{name}）に steps がありません")
        answers = _answers(f.get("answers"), f"{where}.answers")
        picking = sum(1 for st in steps if st.picks and not st.once)
        for i, ans in enumerate(answers):
            if len(ans.pick) > picking:
                raise ConfigError(f"{where}.answers[{i}].pick が {len(ans.pick)} 個ありますが、選ぶ手順は {picking} 個です")
        flows.append(Flow(name, _cond(f.get("start"), f"{where}.start"), tuple(steps),
                          _names(f.get("avoid"), f"{where}.avoid"), _names(f.get("avoid_text"), f"{where}.avoid_text"),
                          answers))
    return flows


@dataclass
class FlowStats:
    started: int = 0
    completed: int = 0
    furthest: int = 0  # いちばん先まで進んだ手順（1 始まり）
    broken: list[str] = field(default_factory=list)  # 途切れた手順（直近のもの）
    retried: int = 0  # 同じ質問でもう一度始めた（前の組み合わせが外れだった）回数


Path = tuple[str, ...]  # 流れの中で選んだもの（押したボタンの label・選んだ選択肢の本文）の並び
Ctx = tuple[str, str]  # (流れの名前, 始めたときの本文の行。なければ scene)


class FlowTracker:
    """いま追っている流れと、その手順。"""

    def __init__(self, flows: list[Flow]) -> None:
        self.flows = flows
        self.active: Flow | None = None
        self.index = 0
        self.idle_since: float | None = None
        self.stats: dict[str, FlowStats] = {f.name: FlowStats() for f in flows}
        self.events: list[str] = []  # 「ハーネスの記録」向けに、流れを始めた・進めた・途切れたこと
        # 試行錯誤
        self.ctx: Ctx | None = None
        self.path: list[str] = []
        self.options: dict[tuple[Ctx, Path], set[str]] = {}  # 選んだものの並び → 次の手順で選べたもの
        self.leaves: set[tuple[Ctx, Path]] = set()  # 最後まで進めた組み合わせ
        self.done_once: set[tuple[str, int, Path]] = set()  # (流れ, 手順, 選んだものの並び) で済ませた once の手順
        self.ctx_seen: set[Ctx] = set()
        self.last_done: Ctx | None = None  # 最後まで進めた（答えた）ときの質問
        self.last_leaf: tuple[Ctx, Path] | None = None  # 最後まで進めた（答えた）組み合わせ
        # 外れと分かった組み合わせ（同じ質問にまた戻ってきた、その前の答え）。進め方の経験と共有し、ランをまたいで覚える
        self.wrong: set[tuple[Ctx, Path]] = set()
        self.wrong_loaded = 0  # 前のランまでに外れと分かっていた組み合わせの数
        self._loaded_leaves: set[tuple[Ctx, Path]] = set()
        self.dead: set[tuple[Ctx, Path]] = set()  # 行き止まり（言葉がまだない思い出）。開示の状態で変わる
        self.lines_after = 0  # 最後まで進めてから届いた本文の行の数
        self.exhausted_noted: set[Ctx] = set()

    @property
    def step(self) -> Step | None:
        return self.active.steps[self.index] if self.active is not None else None

    def remember(self, wrong: set[tuple[Ctx, Path]]) -> None:
        """前のランまでに外れと分かった組み合わせを使う（同じ set に、このランで外れと分かったものを足す）。"""
        self.wrong = wrong
        self.wrong_loaded = len(wrong)
        self.leaves |= wrong
        self._loaded_leaves = set(wrong)

    def line_seen(self) -> None:
        """本文の行が届いた。"""
        self.lines_after += 1

    def update(self, state: GameState, opened_by: dict[str, str], now: float, idle_sec: float,
               retry_lines: int = 0) -> Step | None:
        """いまの状態から、流れの始まり・手順の進み・途切れを判定し、いまの手順を返す。"""
        if self.active is None:
            for f in self.flows:
                if f.start.match(state, opened_by):
                    self.active, self.index, self.idle_since = f, 0, None
                    self.stats[f.name].started += 1
                    self.ctx = (f.name, (state.line or {}).get("line_id") or state.scene or "")
                    if (self.last_done is not None and self.last_done[0] == f.name
                            and self.lines_after <= retry_lines):
                        # 答えてから数行で同じ流れがまた始まった: 外れへの返事を読んで、同じ質問に戻った。
                        # 返事の行は外れ方ごとに違うので、行で見ると同じ質問が別々になり、外れを何度も試す
                        # （run 2026-11-06_1012: 外れ方で 318 行目と 323 行目に分かれた）
                        self.ctx = self.last_done
                    self.path = []
                    if self.ctx in self.ctx_seen:
                        self.stats[f.name].retried += 1
                    if self.last_leaf is not None and self.last_leaf[0] == self.ctx:
                        # 同じ質問に戻ってきた: 前の答えは外れだった。ランをまたいで覚える
                        # （行き止まり（言葉がまだない思い出）は、開示の状態で変わるので覚えない）
                        self.wrong.add(self.last_leaf)
                    self.last_leaf = None
                    self.ctx_seen.add(self.ctx)
                    break
            else:
                return None
        flow = self.active
        for i in range(self.index, len(flow.steps)):
            if self.is_done(i):
                continue  # 同じ選び方で一度済ませた手順（読んだ思い出をもう一度読む、など）は飛ばす
            if flow.steps[i].when.match(state, opened_by):
                if flow.steps[i].when.screen is None:
                    # scene だけで書いた手順（思い出を読む）は、あとの手順の画面が手前に出ていれば、そちらを先にする。
                    # Rachel では、思い出を読み終えて要約に戻っても、答えるまで scene が memories/… のまま変わらない
                    later = next((j for j in range(i + 1, len(flow.steps))
                                  if flow.steps[j].when.screen is not None and not self.is_done(j)
                                  and flow.steps[j].when.match(state, opened_by)), None)
                    if later is not None:
                        i = later
                if i != self.index:
                    self.index = i
                st = self.stats[flow.name]
                st.furthest = max(st.furthest, i + 1)
                self.idle_since = None
                return flow.steps[i]
        last = flow.steps[-1]
        if self.index == len(flow.steps) - 1 and not last.targets and last.choose is None and last.when.screen is None:
            # 最後の手順（本文を最後まで読むなど）の条件から外れた。押して終える手順がないので、ここで最後まで進めたとする
            self._complete("最後の手順の条件から外れた")
            return None
        # いまの手順より前の手順の画面に戻った（言葉の一覧から要約へ戻った、要約を閉じて思い出の一覧へ戻った）。
        # 人が戻って選び直すのと同じく、その手順からやり直す。選んだものの並びも、その手順の前までに戻す
        # （戻れないと、先の手順のまま思い出の一覧で何も優先せず、読む前の思い出を選び直せない。run 2026-11-05_1856）
        for j in range(self.index - 1, -1, -1):
            s = flow.steps[j]
            if s.when.screen is None or not s.when.match(state, opened_by):
                continue
            picks = self._picks_before(j)
            if s.once and (flow.name, j, tuple(self.path[:picks])) in self.done_once:
                continue
            # 同じ画面に戻る手順が前にもあり、そのあいだに選ぶ手順がなければ、まだ済ませていない once の手順を先にする
            # （言葉の一覧から要約に戻ったとき、まだ読んでいない思い出なら「言葉を選ぶ」より「この思い出を読む」）
            for k in range(j):
                e = flow.steps[k]
                if (e.once and e.when.screen == s.when.screen and self._picks_before(k) == picks
                        and (flow.name, k, tuple(self.path[:picks])) not in self.done_once):
                    j, s = k, e
                    break
            self.index, self.path, self.idle_since = j, self.path[:picks], None
            return s
        # 流れの始まりの画面にいるなら、まだ流れの中
        if flow.start.match(state, opened_by) or any(s.when.match(state, opened_by) for s in flow.steps[: self.index]):
            self.idle_since = None
            return None
        if self.idle_since is None:
            self.idle_since = now
        elif now - self.idle_since >= idle_sec:
            self._break(f"手順 {self.index + 1}（{flow.steps[self.index].note or flow.steps[self.index].when}）で、"
                        "流れのどの画面でもなくなった")
        return None

    def _picks_before(self, i: int) -> int:
        """手順 i より前に選んだものの数（once でない、押す・選ぶ手順の数）。"""
        flow = self.active
        if flow is None:
            return 0
        return sum(1 for s in flow.steps[:i] if not s.once and s.picks)

    def is_done(self, i: int) -> bool:
        """手順 i が once で、いまの選び方で一度済ませたか。"""
        flow = self.active
        return (flow is not None and flow.steps[i].once
                and (flow.name, i, tuple(self.path[:self._picks_before(i)])) in self.done_once)

    def done_steps(self) -> list[Step]:
        """いまの選び方で済ませた once の手順（そのボタンは後回しにする）。"""
        flow = self.active
        return [s for i, s in enumerate(flow.steps) if self.is_done(i)] if flow is not None else []

    def offer(self, options: list[str]) -> None:
        """いまの手順で選べるもの（押しても反応しないと見込んでいるものを除く）を覚える。"""
        step = self.step
        if self.ctx is None or not options or step is None or step.once or not step.picks:
            return
        self.options.setdefault((self.ctx, tuple(self.path)), set()).update(options)

    def exhausted(self, option: str) -> bool:
        """いまの選び方に option を足した枝を、すべて試し終えたか（外れだった）。選ぶ手順でなければ見ない。"""
        step = self.step
        if self.ctx is None or step is None or not step.picks:
            return False
        return self._exhausted(self.ctx, (*self.path, option), 0)

    def spent(self) -> bool:
        """いままで選んだものの枝（いま選んでいる思い出）を、すべて試し終えたか（言葉をすべて試した、言葉が1つもない）。"""
        return self.ctx is not None and bool(self.path) and self._exhausted(self.ctx, tuple(self.path), 0)

    def _exhausted(self, ctx: Ctx, path: Path, depth: int) -> bool:
        if (ctx, path) in self.leaves:
            return True
        opts = self.options.get((ctx, path))
        if not opts or depth > 32:
            return False
        return all(self._exhausted(ctx, (*path, o), depth + 1) for o in opts)

    def dead_end(self) -> None:
        """いまの選び方は、手順の画面で選べるものが1つもない行き止まり（外れと同じに扱う）。

        前の once の押す手順（この思い出を読む）を、この選び方でまだ済ませていなければ、行き止まりにしない
        （読む前の思い出の言葉の一覧が空なのは当たり前で、読めば言葉が開示される）。"""
        flow = self.active
        if self.ctx is None or not self.path or flow is None:
            return
        for i, s in enumerate(flow.steps[:self.index]):
            if s.once and s.targets and (flow.name, i, tuple(self.path[:self._picks_before(i)])) not in self.done_once:
                return
        self.leaves.add((self.ctx, tuple(self.path)))
        self.dead.add((self.ctx, tuple(self.path)))

    def forget_reveals(self, why: str) -> None:
        """ゲームの進み具合が入れ替わった（起動し直した、流れの外でセーブをロードした）。読んだ思い出（once の手順）と
        行き止まりは、開示の状態で変わるので忘れる（外れと分かった答えは、開示の状態によらないので覚えておく）。

        run 2026-11-06_1646: 思い出を読んだあと起動し直して、はじめから遊んだ（開示が消えた）のに、読んだと覚えていたので
        読み直さず、言葉のない思い出で「言葉を選ぶ」と空の言葉の一覧を閉じるのを繰り返した。"""
        if not self.done_once and not self.dead:
            return
        self.events.append(f"{why}ので、読んだ思い出など一度済ませた手順（{len(self.done_once)} 件）と行き止まり"
                           f"（{len(self.dead)} 件）を忘れた（開示の状態が変わるため）")
        self.leaves -= self.dead - self.wrong
        self.dead.clear()
        self.done_once.clear()

    def note_exhausted(self) -> None:
        """いまの手順で選べるものの枝を、すべて試し終えた（同じ質問で、試せる組み合わせがもうない）。"""
        if self.ctx is None or self.ctx in self.exhausted_noted:
            return
        self.exhausted_noted.add(self.ctx)
        tried = sum(1 for c, _p in self.leaves if c == self.ctx)
        self.events.append(
            f"流れ「{self.ctx[0]}」の `{self.ctx[1]}` で、試せる組み合わせ（{tried} 通り）をすべて試しても先に進めなかった"
            f"（手順 {self.index + 1}。まだ開示されていないものが答えか、進めない不具合の可能性がある）"
        )

    def tried(self, option: str) -> bool:
        """いまの選び方に option を足した枝を、前に試しはじめたことがあるか（同じ思い出の、ほかの言葉を試すため）。"""
        step = self.step
        if self.ctx is None or step is None or not step.picks:
            return False
        p = (*self.path, option)
        return (self.ctx, p) in self.options or (self.ctx, p) in self.leaves

    def pressed(self, label: str, name: str, responded: bool, choice_text: str | None = None,
                text: str | None = None) -> None:
        """押した結果。手順のボタン（選択肢）で反応があれば、選んだものとして覚える。最後の手順なら、最後まで進めたとする。"""
        flow, step = self.active, self.step
        if flow is None or step is None or not responded:
            return
        if choice_text is None:
            # いまの手順より前の once の手順のボタン（言葉を選ぶ画面から戻って思い出を読み直したなど）も、済ませたと覚える
            for i, s in enumerate(flow.steps[:self.index]):
                if (s.once and s.targets and (s.when.screen is None or label.startswith(s.when.screen + "/"))
                        and s.presses(label, name, text)):
                    self.done_once.add((flow.name, i, tuple(self.path[:self._picks_before(i)])))
        if choice_text is not None:
            if step.choose is None or (step.choose and not any(fnmatchcase(choice_text, p) for p in step.choose)):
                return
            picked = choice_text
        else:
            if step.when.screen is not None and not label.startswith(step.when.screen + "/"):
                return
            if not step.targets or not step.presses(label, name, text):
                return
            picked = label
        if step.once:
            self.done_once.add((flow.name, self.index, tuple(self.path[:self._picks_before(self.index)])))
        elif step.picks:
            # 同じ手順で選び直したら置き換える（思い出を選んで反応があったが、要約が出ずに一覧のままだったなど）
            self.path = [*self.path[:self._picks_before(self.index)], picked]
        if self.index == len(flow.steps) - 1:
            if self.ctx is not None:
                self.leaves.add((self.ctx, tuple(self.path)))
                self.last_done, self.lines_after = self.ctx, 0
                self.last_leaf = (self.ctx, tuple(self.path))
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
        # タイトルからは、はじめから遊ぶかセーブをロードするので、開示の状態が変わる
        self.forget_reveals("タイトルに戻った")

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
            if st.retried:
                tried = sum(1 for c, p in self.leaves - self._loaded_leaves if c[0] == f.name)
                line += f"。同じ質問でもう一度始めた（前の組み合わせが外れだった）のは {st.retried} 回、試した組み合わせは {tried} 通り"
            if st.broken:
                line += "。途切れたところ（直近）: " + " / ".join(st.broken)
            out.append(line)
        new = len(self.wrong) - self.wrong_loaded
        if self.wrong_loaded or new:
            out.append(f"流れの答えで、前のランまでに外れと分かっていた組み合わせ {self.wrong_loaded} 通りを避けた。"
                       f"このランで新しく外れと分かったのは {new} 通り（runs/_experience/<ゲーム>.json の flow_wrong に覚える）")
        return out + [e for e in self.events if "すべて試しても" in e]
