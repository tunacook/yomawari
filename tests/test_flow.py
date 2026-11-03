"""ゲームの流れ（fuzz/flow.py、プロファイルの [[flow]]）。"""

import random
import tomllib

import pytest

from yomawari.config import FUZZ_DEFAULTS, ConfigError
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.flow import FlowTracker, parse_flows
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.ops import normal_candidates
from yomawari.input.pointer import Pointer
from yomawari.profile import build_spec
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState

from .test_window import FakeWindows

# Rachel の「質問に答える」（ゲームが開いた思い出の一覧で思い出を選ぶ → 読む → 言葉を選んで答える）
PROFILE = """
[[flow]]
name = "質問に答える"
start = { screen = "MemoryListUI", by = "game" }
avoid = ["MemoryListUI/SelectButton", "MemorySummaryUI/CloseMemorySummary", "MemoryScenarioPlaying/OpenDialog"]
steps = [
  { screen = "MemoryListUI", press = "MemoryListGridSlot*", note = "開示済みの思い出を選ぶ" },
  { screen = "MemorySummaryUI", press = "StartToMemoryScenario", note = "思い出を読む" },
  { scene = "memories/*", advance = true, note = "最後まで読む" },
  { screen = "MemorySummaryUI", press = "ShowFragmentListButton", note = "言葉を選ぶ" },
  { screen = "MemoryFragmentListUI", press = "MemoryFragmentListSlot*", note = "言葉を選ぶ" },
  { screen = "FragmentSelectDialogUI", press = "FragmentSelectButton", note = "この言葉で答える" },
]

[[flow]]
name = "選択肢で答える"
start = { choices = true }
steps = [{ choose = ["*はい*"], note = "はいと答える" }]
"""

SUMMARY = [
    {"name": "StartToMemoryScenario", "rect": [0.42, 0.76, 0.61, 0.84]},
    {"name": "ShowFragmentListButton", "rect": [0.64, 0.76, 0.82, 0.83]},
    {"name": "CloseMemorySummary", "rect": [0.18, 0.77, 0.24, 0.82]},
]
LIST = [{"name": "MemoryListGridSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(3)]
LIST += [{"name": "SelectButton", "rect": [0.08, 0.89, 0.32, 0.94]}]


def _flows():
    return parse_flows(tomllib.loads(PROFILE)["flow"])


def _session(cfg=None):
    st = GameState()
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0, **(cfg or {})}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1, flow=FlowTracker(_flows()))
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    events = []
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), events.append, 0, lambda _: None)
    return s, st, run, events


def _feed(st, kind, **d):
    st.feed(Event("game", kind, d, 0))


def _weights(s, st, now=0.0):
    cands = normal_candidates(st, Guard({}), s.cfg["weights"], False)
    return {op.label: op.weight for op in s._apply_flow(cands, now)}


def test_parse_profile_and_errors():
    flows = _flows()
    assert [f.name for f in flows] == ["質問に答える", "選択肢で答える"]
    assert flows[0].steps[2].advance and flows[0].steps[2].when.scene == "memories/*"
    assert flows[1].steps[0].when.choices and flows[1].steps[0].choose == ("*はい*",)
    spec = build_spec(tomllib.loads(PROFILE), {})
    assert len(spec.flows) == 2
    with pytest.raises(ConfigError, match="知らない項目"):
        parse_flows([{"name": "x", "start": {"screen": "A"}, "steps": [{"screen": "A", "click": "B"}]}])
    with pytest.raises(ConfigError, match="screen・scene・choices のどれか"):
        parse_flows([{"name": "x", "start": {}, "steps": [{"screen": "A"}]}])
    with pytest.raises(ConfigError, match="steps がありません"):
        parse_flows([{"name": "x", "start": {"screen": "A"}}])


def test_follows_question_flow_and_avoids_leaving():
    s, st, run, events = _session()
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    # ハーネスがボタンで開いた一覧（寄り道）では始めない
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
    run.opened_by["MemoryListUI"] = "RClickMenuUI/ToMemoryList"
    assert _weights(s, st)["click MemoryListUI/SelectButton"] == click and run.flow.active is None
    # ゲームが開いた一覧（質問）: 思い出を選ぶのを優先し、「思い出を選ばない」は後回し
    del run.opened_by["MemoryListUI"]
    w = _weights(s, st)
    assert run.flow.active.name == "質問に答える" and run.flow.index == 0
    assert w["click MemoryListUI/MemoryListGridSlot (Clone)"] == click * k
    assert w["click MemoryListUI/SelectButton"] == pytest.approx(click * low)
    # 押しても反応しなかったスロット（ロック中）と同じ見た目のものは、手順のボタンでも優先しない
    run.silent.add("MemoryListUI/MemoryListGridSlot (Clone)#1")
    assert _weights(s, st)["click MemoryListUI/MemoryListGridSlot (Clone)#1"] == click
    # 要約が出た（読む前）: 「この思い出を読む」
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 1
    assert w["click MemorySummaryUI/StartToMemoryScenario"] == click * k
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == click
    assert w["click MemorySummaryUI/CloseMemorySummary"] == pytest.approx(click * low)
    # 思い出を読んでいる: 本文送りを優先し、「本編へ戻る」は後回し
    _feed(st, "menu_close", name="MemorySummaryUI")
    _feed(st, "menu_close", name="MemoryListUI")
    _feed(st, "scene", name="memories/memories_6011")
    _feed(st, "menu", name="MemoryScenarioPlaying", modal=False,
          buttons=[{"name": "OpenDialog", "rect": [0, 0.0, 0.23, 0.08]}])
    _feed(st, "line", line_id="memories/memories_6011:1", text="a")
    w = _weights(s, st)
    assert run.flow.index == 2
    assert w["advance"] == FUZZ_DEFAULTS["weights"]["advance"] * k
    assert w["click MemoryScenarioPlaying/OpenDialog"] == pytest.approx(click * low)
    # 読み終えて要約に戻った: 同じ画面でも、手順の順番で「言葉を選ぶ」
    _feed(st, "menu_close", name="MemoryScenarioPlaying")
    _feed(st, "scene", name="main/scenarioA_004")
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == click * k
    assert w["click MemorySummaryUI/StartToMemoryScenario"] == click
    # 言葉の一覧 → 確認ダイアログ → 答える
    _feed(st, "menu", name="MemoryFragmentListUI", modal=True, buttons=[
        {"name": "MemoryFragmentListSlot(Clone)", "rect": [0.2, 0.2, 0.4, 0.3]},
        {"name": "ReturnButton", "rect": [0.07, 0.9, 0.13, 0.95]},
    ])
    assert _weights(s, st)["click MemoryFragmentListUI/MemoryFragmentListSlot(Clone)"] == click * k
    _feed(st, "menu", name="FragmentSelectDialogUI", modal=True, buttons=[
        {"name": "FragmentSelectButton", "rect": [0.5, 0.6, 0.7, 0.7]},
        {"name": "FragmentCancelButton", "rect": [0.3, 0.6, 0.45, 0.7]},
    ])
    assert _weights(s, st)["click FragmentSelectDialogUI/FragmentSelectButton"] == click * k
    assert run.flow.index == 5
    run.flow.pressed("FragmentSelectDialogUI/FragmentSelectButton", "FragmentSelectButton", True)
    assert run.flow.active is None
    assert [e.data["step"] for e in events if e.kind == "flow"] == [1, 2, 3, 4, 5, 6]
    summary = run.flow.summary()
    assert any("質問に答える」: 1 回始め、1 回最後まで進めた。いちばん先まで進んだのは手順 6/6" in n for n in summary)
    assert any("選択肢で答える」は一度も始まらなかった" in n for n in summary)


def test_choices_flow_and_breaking():
    s, st, run, _ = _session()
    _feed(st, "choices", items=[{"id": "a", "text": "はい", "rect": [0.4, 0.4, 0.6, 0.5]},
                                {"id": "b", "text": "いいえ", "rect": [0.4, 0.5, 0.6, 0.6]}])
    w = _weights(s, st)
    assert run.flow.active.name == "選択肢で答える"
    assert w["click choices/はい"] == FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["flow_factor"]
    assert w["click choices/いいえ"] == FUZZ_DEFAULTS["weights"]["click"]
    run.flow.pressed("choices/はい", "a", True, "はい")
    assert run.flow.active is None and run.flow.stats["選択肢で答える"].completed == 1
    # 流れのどの画面でもない状態が flow_idle_sec 続くと、途切れたとする
    _feed(st, "choices", items=[{"id": "c", "text": "いいえ", "rect": [0.4, 0.4, 0.6, 0.5]}])
    _weights(s, st, 0.0)
    _feed(st, "choices_done")
    _feed(st, "line", line_id="main/s1:1", text="a")
    _weights(s, st, 1.0)
    assert run.flow.active is not None
    _weights(s, st, 1.0 + FUZZ_DEFAULTS["flow_idle_sec"])
    assert run.flow.active is None
    assert any("途切れたところ" in n for n in run.flow.summary())


def test_flow_is_not_used_while_exploring_or_when_disabled():
    for cfg, intent in (({}, "explore"), ({"use_flow": False}, "progress")):
        s, st, run, _ = _session(cfg)
        run.intent = intent
        _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
        assert _weights(s, st)["click MemoryListUI/SelectButton"] == FUZZ_DEFAULTS["weights"]["click"]
        assert run.flow.active is None
