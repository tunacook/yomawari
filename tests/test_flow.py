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


def test_flow_ending_with_reading_completes_when_leaving_it():
    flows = parse_flows(tomllib.loads("""
[[flow]]
name = "思い出を読む"
start = { screen = "MemoryListUI", by = "harness" }
steps = [
  { screen = "MemoryListUI", press = "MemoryListGridSlot*" },
  { scene = "memories/*", advance = true },
]
""")["flow"])
    tracker = FlowTracker(flows)
    st = GameState()
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
    tracker.update(st, {"MemoryListUI": "RClickMenuUI/ToMemoryList"}, 0, 10)
    _feed(st, "menu_close", name="MemoryListUI")
    _feed(st, "scene", name="memories/m1")
    _feed(st, "line", line_id="memories/m1:1", text="a")
    assert tracker.update(st, {}, 1, 10).advance
    # 読み終えて本編に戻った
    _feed(st, "scene", name="main/s2")
    tracker.update(st, {}, 2, 10)
    assert tracker.active is None and tracker.stats["思い出を読む"].completed == 1


TRIAL = """
[[flow]]
name = "質問に答える"
start = { screen = "MemoryListUI", by = "game" }
steps = [
  { screen = "MemoryListUI", press = "MemoryListGridSlot*" },
  { screen = "MemorySummaryUI", press = "StartToMemoryScenario", once = true },
  { scene = "memories/*", advance = true, once = true },
  { screen = "MemorySummaryUI", press = "ShowFragmentListButton" },
  { screen = "MemoryFragmentListUI", press = "MemoryFragmentListSlot*" },
  { screen = "FragmentSelectDialogUI", press = "Accept" },
]
"""
FRAGS = [{"name": "MemoryFragmentListSlot(Clone)", "rect": [0.2 + 0.2 * i, 0.2, 0.35 + 0.2 * i, 0.3]} for i in range(2)]


def _only(st, name, buttons):
    for m in list(st.menus):
        _feed(st, "menu_close", name=m)
    _feed(st, "menu", name=name, modal=True, buttons=buttons)


def _press(s, st, run, label):
    w = _weights(s, st)
    name = label.split("/", 1)[1].split("#", 1)[0]
    run.flow.pressed(label, name, True)
    return w


def test_retrying_the_same_question_tries_other_answers():
    # 同じ質問に戻ってきたら、外れた組み合わせを避ける。同じ思い出のまだ試していない言葉を先に試し、
    # 言葉をすべて試した思い出は後回しにして、ほかの思い出へ。一度読んだ思い出は読み直さない
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(TRIAL)["flow"]))
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    slot = "MemoryListUI/MemoryListGridSlot (Clone)"
    frag = "MemoryFragmentListUI/MemoryFragmentListSlot(Clone)"

    def ask():
        _feed(st, "scene", name="main/s6")
        _feed(st, "line", line_id="main/s6:70", text="ねぇ、どこだって？")
        _only(st, "MemoryListUI", LIST)

    def answer(memory, fragment, read):
        _press(s, st, run, memory)
        _only(st, "MemorySummaryUI", SUMMARY)
        w = _weights(s, st)
        if read:
            assert w["click MemorySummaryUI/StartToMemoryScenario"] == click * k
            run.flow.pressed("MemorySummaryUI/StartToMemoryScenario", "StartToMemoryScenario", True)
            for m in list(st.menus):
                _feed(st, "menu_close", name=m)
            _feed(st, "scene", name="memories/m1")
            _feed(st, "line", line_id="memories/m1:1", text="a")
            _weights(s, st)
            _feed(st, "scene", name="main/s6")
            _only(st, "MemorySummaryUI", SUMMARY)
            w = _weights(s, st)
        else:
            # 読んだ思い出は読み直さず、言葉を選ぶ
            assert w["click MemorySummaryUI/StartToMemoryScenario"] == pytest.approx(click * low)
        assert w["click MemorySummaryUI/ShowFragmentListButton"] == click * k
        run.flow.pressed("MemorySummaryUI/ShowFragmentListButton", "ShowFragmentListButton", True)
        _only(st, "MemoryFragmentListUI", FRAGS)
        w = _press(s, st, run, fragment)
        _only(st, "FragmentSelectDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
        _press(s, st, run, "FragmentSelectDialogUI/Accept")
        assert run.flow.active is None
        return w

    ask()
    w = _weights(s, st)
    assert all(w[f"click {slot}{n}"] == click * k for n in ("", "#1", "#2"))
    answer(slot, frag, read=True)
    # 外れて同じ質問に戻った: 試しかけの思い出（slot）を先に、外れた言葉は後回し
    ask()
    w = _weights(s, st)
    assert w[f"click {slot}"] == click * k and w[f"click {slot}#1"] == click
    w = answer(slot, frag + "#1", read=False)
    assert w[f"click {frag}"] == pytest.approx(click * low) and w[f"click {frag}#1"] == click * k
    # 思い出 slot の言葉はすべて外れた: slot は後回しにして、ほかの思い出へ
    ask()
    w = _weights(s, st)
    assert w[f"click {slot}"] == pytest.approx(click * low)
    assert w[f"click {slot}#1"] == click * k and w[f"click {slot}#2"] == click * k
    assert run.flow.stats["質問に答える"].retried == 2
    assert any("もう一度始めた（前の組み合わせが外れだった）のは 2 回、試した組み合わせは 2 通り" in n
               for n in run.flow.summary())


def test_same_question_after_different_wrong_replies():
    # run 2026-11-06_1012: 外れへの返事の行が外れ方ごとに違い（318 行目と 323 行目）、同じ質問が別々に覚えられた。
    # また「言葉を選ぶ」を押したときと押さずに一覧が出たときで組み合わせが分かれ、同じ外れを何度も答えた
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(TRIAL)["flow"]))
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    slot = "MemoryListUI/MemoryListGridSlot (Clone)"
    frag = "MemoryFragmentListUI/MemoryFragmentListSlot(Clone)"

    def ask(line_ids):
        _feed(st, "scene", name="main/s6")
        for n, line_id in enumerate(line_ids):
            _feed(st, "line", line_id=line_id, text=f"返事{n}")
        _only(st, "MemoryListUI", LIST)

    def answer(fragment, show_list):
        _press(s, st, run, slot)
        _only(st, "MemorySummaryUI", SUMMARY)
        _weights(s, st)
        if show_list:
            run.flow.pressed("MemorySummaryUI/ShowFragmentListButton", "ShowFragmentListButton", True)
        _only(st, "MemoryFragmentListUI", FRAGS)
        w = _press(s, st, run, fragment)
        _only(st, "FragmentSelectDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
        _press(s, st, run, "FragmentSelectDialogUI/Accept")
        assert run.flow.active is None
        return w

    ask(["main/s6:70"])
    answer(frag, show_list=True)
    ask(["main/s6:75", "main/s6:76"])  # 外れ方 A への返事
    assert run.flow.ctx == ("質問に答える", "main/s6:70")
    w = answer(frag + "#1", show_list=False)
    assert w[f"click {frag}"] == pytest.approx(click * low) and w[f"click {frag}#1"] == click * k
    ask(["main/s6:80", "main/s6:81"])  # 外れ方 B への返事
    w = _weights(s, st)
    # 思い出 slot の言葉は（言葉を選ぶを押したかどうかによらず）すべて外れた
    assert w[f"click {slot}"] == pytest.approx(click * low) and w[f"click {slot}#1"] == click * k
    assert run.flow.stats["質問に答える"].retried == 2
    # 答えてから本文が flow_retry_lines を超えて進んだら、別の質問
    run.flow.active = None
    for n in range(FUZZ_DEFAULTS["flow_retry_lines"] + 1):
        s.on_event(Event("game", "line", {"line_id": f"main/s6:{100 + n}", "text": "続き"}, 0.0, 0))
    _only(st, "MemoryListUI", LIST)
    _weights(s, st)
    assert run.flow.ctx[1] != "main/s6:70"


def test_all_answers_tried_is_noted():
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads("""
[[flow]]
name = "選択肢で答える"
start = { choices = true }
steps = [{ choose = true }]
""")["flow"]))
    items = [{"id": "a", "text": "はい", "rect": [0.4, 0.4, 0.6, 0.5]}, {"id": "b", "text": "いいえ", "rect": [0.4, 0.5, 0.6, 0.6]}]
    for text in ("はい", "いいえ"):
        _feed(st, "line", line_id="main/s1:5", text="行く？")
        _feed(st, "choices", items=items)
        w = _weights(s, st)
        if text == "いいえ":
            assert w["click choices/はい"] == pytest.approx(FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["flow_avoid_factor"])
        run.flow.pressed(f"choices/{text}", "a", True, text)
        _feed(st, "choices_done")
    _feed(st, "line", line_id="main/s1:5", text="行く？")
    _feed(st, "choices", items=items)
    w = _weights(s, st)
    # どちらも外れた: どれも同じ重みに戻し、記録に書く
    assert w["click choices/はい"] == w["click choices/いいえ"] == FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["flow_factor"]
    assert any("2 通り）をすべて試しても先に進めなかった" in n for n in run.flow.summary())


def test_screen_step_wins_over_lingering_scene_step():
    # run 0946: 思い出を読み終えて要約に戻っても、答えるまで scene が memories/… のまま。「思い出を読む」手順（scene だけ）に
    # とどまらず、要約・言葉の一覧・確認ダイアログの手順に進み、答えたら最後まで進めたとする（試した組み合わせも覚える）
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(TRIAL)["flow"]))
    _feed(st, "scene", name="main/s4")
    _feed(st, "line", line_id="main/s4:44", text="どこ？")
    _only(st, "MemoryListUI", LIST)
    _press(s, st, run, "MemoryListUI/MemoryListGridSlot (Clone)")
    _only(st, "MemorySummaryUI", SUMMARY)
    _press(s, st, run, "MemorySummaryUI/StartToMemoryScenario")
    for m in list(st.menus):
        _feed(st, "menu_close", name=m)
    _feed(st, "scene", name="memories/m1")
    _feed(st, "line", line_id="memories/m1:1", text="a")
    _weights(s, st)
    assert run.flow.index == 2
    _only(st, "MemorySummaryUI", SUMMARY)  # scene は memories/m1 のまま
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["flow_factor"]
    run.flow.pressed("MemorySummaryUI/ShowFragmentListButton", "ShowFragmentListButton", True)
    _only(st, "MemoryFragmentListUI", FRAGS)
    _press(s, st, run, "MemoryFragmentListUI/MemoryFragmentListSlot(Clone)#1")
    _only(st, "FragmentSelectDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
    _press(s, st, run, "FragmentSelectDialogUI/Accept")
    assert run.flow.active is None and run.flow.stats["質問に答える"].completed == 1
    assert len(run.flow.leaves) == 1


def test_steps_can_name_buttons_by_visible_text():
    # 手順のボタンを、GameObject の名前ではなく、画面に見える文字（画面から読んだもの）で書く。空白は除いて比べる
    flows = parse_flows(tomllib.loads("""
[[flow]]
name = "読む"
start = { screen = "MemorySummaryUI" }
avoid_text = ["閉じる"]
steps = [
  { screen = "MemorySummaryUI", text = "*読む*" },
  { scene = "memories/*", advance = true },
]
""")["flow"])
    s, st, run, _ = _session()
    run.flow = FlowTracker(flows)
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    _only(st, "MemorySummaryUI", SUMMARY)
    st.set_read_text({"MemorySummaryUI/StartToMemoryScenario": "この 思い出を 読む", "MemorySummaryUI/CloseMemorySummary": "閉じる",
                      "MemorySummaryUI/ShowFragmentListButton": "言葉を選ぶ"})
    w = _weights(s, st)
    assert w["click MemorySummaryUI/StartToMemoryScenario"] == click * k
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == click
    assert w["click MemorySummaryUI/CloseMemorySummary"] == pytest.approx(click * low)
    # 文字がまだ読めていないボタンは、手順のボタンとみなさない
    st.read_text.clear()
    _only(st, "MemorySummaryUI", SUMMARY)
    assert _weights(s, st)["click MemorySummaryUI/StartToMemoryScenario"] == click
    with pytest.raises(ConfigError, match="知らない項目"):
        parse_flows([{"name": "x", "start": {"screen": "A"}, "steps": [{"screen": "A"}], "avoid_txt": ["a"]}])


ONCE_PROFILE = """
[[flow]]
name = "質問に言葉で答える"
start = { screen = "MemoryListUI", by = "game" }
steps = [
  { screen = "MemoryListUI", press = "MemoryListGridSlot*", note = "開示済みの思い出を選ぶ" },
  { screen = "MemorySummaryUI", press = "StartToMemoryScenario", once = true, note = "この思い出を読む" },
  { scene = "memories/*", advance = true, once = true, note = "最後まで読む" },
  { screen = "MemorySummaryUI", press = "ShowFragmentListButton", note = "言葉を選ぶ" },
  { screen = "MemoryFragmentListUI", press = "MemoryFragmentListSlot*", note = "開示済みの言葉を選ぶ" },
]
"""
FRAG = "MemoryFragmentListUI/MemoryFragmentListSlot(Clone)"
SLOT = "MemoryListUI/MemoryListGridSlot (Clone)"


def _to_fragments(s, st, run, slots):
    """質問の一覧で 1 つめの思い出を選び、言葉の一覧まで進める（読むのは済ませたものとして飛ばす）。"""
    _feed(st, "line", line_id="main/scenarioB_004:323.0", text="アカウント名を選ぼう。")
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
    _weights(s, st)
    run.flow.pressed(SLOT, "MemoryListGridSlot (Clone)", True)
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    _weights(s, st)
    run.flow.pressed("MemorySummaryUI/StartToMemoryScenario", "StartToMemoryScenario", True)
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    run.flow.index = 3
    _weights(s, st)
    run.flow.pressed("MemorySummaryUI/ShowFragmentListButton", "ShowFragmentListButton", True)
    _feed(st, "menu", name="MemoryFragmentListUI", modal=True, buttons=[
        *[{"name": "MemoryFragmentListSlot(Clone)", "rect": [0.1 + 0.15 * i, 0.4, 0.2 + 0.15 * i, 0.5]} for i in range(slots)],
        {"name": "ReturnButton", "rect": [0.07, 0.9, 0.13, 0.95]},
    ])


def test_dim_options_are_tried_after_bright_ones():
    # run 2026-11-05_1346: 「No.009 こわくない場所」の言葉の一覧で、下の3枚（答えの「rachel0612」を含む）が暗く写り、
    # 押せないとみなして選ばずに、上の3枚を試し終えたら「すべて試した」としていた。暗いものも、明るいものの次に試す
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    _to_fragments(s, st, run, 4)
    labels = [FRAG, f"{FRAG}#1", f"{FRAG}#2", f"{FRAG}#3"]
    s._dim = {labels[2], labels[3]}
    w = _weights(s, st)
    assert run.flow.index == 4
    assert [w[f"click {x}"] for x in labels] == [click * k, click * k, click, click]
    # 明るい2枚は外れだった
    ctx, path = run.flow.ctx, tuple(run.flow.path)
    run.flow.leaves |= {(ctx, (*path, labels[0])), (ctx, (*path, labels[1]))}
    w = _weights(s, st)
    assert [w[f"click {x}"] for x in labels[2:]] == [click * k, click * k]
    assert not any("すべて試しても" in e for e in run.flow.events)
    # 押して反応がなかったもの（押して確かめた）は、暗いものの番になっても選ばない
    run.silent.add(labels[3])
    assert _weights(s, st)[f"click {labels[3]}"] == click
    run.flow.leaves.add((ctx, (*path, labels[2])))
    _weights(s, st)
    assert any("すべて試しても" in e for e in run.flow.events)


def test_empty_fragment_list_is_dead_end_and_reading_is_remembered():
    # run 2026-11-05_1346: 言葉が1つも開示されていない思い出「あの計画」（一覧には戻るボタンだけ）を、選ぶたびに
    # 読み直していた。選べるものがない一覧は行き止まりとし、手順の順番から外れて読んだときも読んだと覚える
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    _to_fragments(s, st, run, 0)
    _weights(s, st)
    assert (run.flow.ctx, (SLOT,)) in run.flow.leaves
    # 言葉の一覧から戻って、要約で「この思い出を読む」を押した（いまの手順は言葉を選ぶ）
    run.flow.done_once.clear()
    run.flow.pressed("MemorySummaryUI/StartToMemoryScenario", "StartToMemoryScenario", True)
    assert run.flow.is_done(1)
    # 同じ質問で同じ思い出を選び直したときも、読んだ思い出は読み直さない（「この思い出を読む」は後回し）
    run.flow.path = [SLOT]
    run.flow.index = 1
    assert run.flow.is_done(1)


def test_read_text_is_forgotten_when_menu_closes():
    # 言葉のスロットの名前は並びの番号なので、別の思い出の一覧でも同じ名前になる。閉じたら読んだ文字を忘れる
    st = GameState()
    _feed(st, "menu", name="MemoryFragmentListUI", modal=True,
          buttons=[{"name": "MemoryFragmentListSlot(Clone)", "rect": [0.1, 0.4, 0.2, 0.5]}])
    st.set_read_text({FRAG: "階段", "MemoryListUI/X": "No.009"})
    _feed(st, "menu_close", name="MemoryFragmentListUI")
    assert st.read_text == {"MemoryListUI/X": "No.009"} and st.closed == {"MemoryFragmentListUI"}
    _feed(st, "menu", name="MemoryFragmentListUI", modal=True,
          buttons=[{"name": "MemoryFragmentListSlot(Clone)", "rect": [0.1, 0.4, 0.2, 0.5]}])
    assert st.find(FRAG).text is None


def test_dim_options_wait_while_bright_one_is_off_screen():
    # run 2026-11-05_1811: 質問の一覧で、開示済みの思い出（明るい）が画面の外にあり、見えているのはロック中（暗い）
    # ものだけだった。明るいものを試し終えたと誤って、ロック中の思い出を次々に押していた。画面の外の明るいものを先に探す
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    click = FUZZ_DEFAULTS["weights"]["click"]
    slots = [{"name": "MemoryListGridSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(3)]
    slots.append({"name": "MemoryListGridSlot (Clone)", "rect": [0.1, 1.3, 0.25, 1.5], "hidden": True})
    _feed(st, "line", line_id="main/scenarioA_004:44.0", text="あの時——")
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=slots)
    s._dim = {SLOT, f"{SLOT}#1", f"{SLOT}#2"}
    w = _weights(s, st)
    assert run.flow.index == 0
    assert [w[f"click {x}"] for x in (SLOT, f"{SLOT}#1", f"{SLOT}#2")] == [click] * 3
    assert not any("すべて試しても" in e for e in run.flow.events)


def test_going_back_restarts_from_that_step():
    # run 2026-11-05_1856: まだ読んでいない思い出で「言葉を選ぶ」を押し、空の言葉の一覧から要約・思い出の一覧へ戻った。
    # 流れが「言葉を選ぶ」のまま戻れず、思い出の一覧で何も優先せず、思い出を読まなくなった
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    _feed(st, "line", line_id="main/scenarioA_004:44.0", text="あの時——")
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
    _weights(s, st)
    run.flow.pressed(SLOT, "MemoryListGridSlot (Clone)", True)
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    assert _weights(s, st)["click MemorySummaryUI/StartToMemoryScenario"] == click * k
    # 読まずに「言葉を選ぶ」を押した。言葉の一覧は空（読む前なので当たり前）で、行き止まりにはしない
    run.flow.pressed("MemorySummaryUI/ShowFragmentListButton", "ShowFragmentListButton", True)
    _feed(st, "menu", name="MemoryFragmentListUI", modal=True,
          buttons=[{"name": "ReturnButton", "rect": [0.07, 0.9, 0.13, 0.95]}])
    _weights(s, st)
    assert run.flow.index == 4 and not run.flow.leaves
    # 要約に戻った: まだ読んでいないので「この思い出を読む」からやり直す
    _feed(st, "menu_close", name="MemoryFragmentListUI")
    w = _weights(s, st)
    assert run.flow.index == 1 and run.flow.path == [SLOT]
    assert w["click MemorySummaryUI/StartToMemoryScenario"] == click * k
    # 要約を閉じて思い出の一覧に戻った: 思い出を選ぶところからやり直す
    _feed(st, "menu_close", name="MemorySummaryUI")
    w = _weights(s, st)
    assert run.flow.index == 0 and run.flow.path == []
    assert w[f"click {SLOT}#1"] == click * k
    # 一覧のまま選び直したら、選んだものを置き換える
    run.flow.pressed(f"{SLOT}#1", "MemoryListGridSlot (Clone)", True)
    run.flow.pressed(f"{SLOT}#2", "MemoryListGridSlot (Clone)", True)
    assert run.flow.path == [f"{SLOT}#2"]
    # 読んだ思い出の要約に、言葉の一覧から戻ったときは「言葉を選ぶ」
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    _weights(s, st)
    run.flow.pressed("MemorySummaryUI/StartToMemoryScenario", "StartToMemoryScenario", True)
    run.flow.index = 4
    _feed(st, "menu_close", name="MemorySummaryUI")
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == click * k


def test_tried_all_visible_options_looks_off_screen():
    # run 2026-11-05_2230: 質問の一覧が最後のほうで開き、見えている思い出 2 つ（No.050・No.052）の言葉をすべて試しても、
    # 一覧を動かさずに同じ 2 つを 1 時間試し続けた。見えているものを試し終えたら、画面の外のまだ試していないものを探す
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    k, low, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"], FUZZ_DEFAULTS["weights"]["click"]
    slots = [{"name": "MemoryListGridSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(2)]
    slots += [{"name": "MemoryListGridSlot (Clone)", "rect": [0.1, -0.5, 0.25, -0.3], "hidden": True},
              {"name": "MemoryListGridSlot (Clone)", "rect": [0.3, -0.5, 0.45, -0.3], "hidden": True}]
    _feed(st, "line", line_id="main/scenarioB_004:323.0", text="アカウント名を選ぼう。")
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=slots)
    _weights(s, st)
    ctx = run.flow.ctx
    run.flow.leaves |= {(ctx, (SLOT,)), (ctx, (f"{SLOT}#1",))}
    run.masked_chars = "?？"
    st.set_read_text({f"{SLOT}#3": "？？？"})  # 伏せ字のものは探さない（#2 だけを探す）
    w = _weights(s, st)
    assert w[f"click {SLOT}"] == w[f"click {SLOT}#1"] == pytest.approx(click * low)
    assert w["wheel_up 3"] == w["drag down"] == click * k
    assert w["wheel_down 3"] < click * k  # 上へ探す（下へはふだんの動かす操作のまま）
    assert not any("すべて試しても" in e for e in run.flow.events)
    assert any("画面の外のまだ試していない項目を探した" in n for n in run.notes)
    # 画面の外のものも試し終えたら（残りは伏せ字の #3 だけ）、すべて試したと記録する
    run.flow.leaves.add((ctx, (f"{SLOT}#2",)))
    w = _weights(s, st)
    assert w["wheel_up 3"] < click * k
    assert any("すべて試しても" in e for e in run.flow.events)


def test_wrong_answers_are_remembered_across_runs(tmp_path):
    # 外れと分かった組み合わせ（同じ質問にまた戻ってきた、その前の答え）を runs/_experience に覚え、次のランで避ける。
    # 最後の答え（そのあと同じ質問に戻らなかった）は正解かもしれないので覚えない
    from yomawari.fuzz.experience import Experience

    slot = "MemoryListUI/MemoryListGridSlot (Clone)"
    frag = "MemoryFragmentListUI/MemoryFragmentListSlot(Clone)"
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    path = tmp_path / "game.json"

    def new_run():
        s, st, run, _ = _session()
        run.flow = FlowTracker(parse_flows(tomllib.loads(TRIAL)["flow"]))
        exp = Experience(path)
        run.flow.remember(exp.flow_wrong)
        return s, st, run, exp

    def ask(s, st):
        _feed(st, "scene", name="main/s6")
        _feed(st, "line", line_id="main/s6:70", text="ねぇ、どこだって？")
        _only(st, "MemoryListUI", LIST)

    def answer(s, st, run, fragment):
        _press(s, st, run, slot)
        _only(st, "MemorySummaryUI", SUMMARY)
        _weights(s, st)
        run.flow.pressed("MemorySummaryUI/ShowFragmentListButton", "ShowFragmentListButton", True)
        _only(st, "MemoryFragmentListUI", FRAGS)
        w = _press(s, st, run, fragment)
        _only(st, "FragmentSelectDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
        _press(s, st, run, "FragmentSelectDialogUI/Accept")
        return w

    s, st, run, exp = new_run()
    ask(s, st)
    answer(s, st, run, frag)
    ask(s, st)  # 同じ質問に戻った: 前の答えは外れ
    answer(s, st, run, frag + "#1")  # これが正解だった（このあと同じ質問に戻らない）
    exp.save()
    assert exp.flow_wrong == {(("質問に答える", "main/s6:70"), (slot, frag))}

    s, st, run, exp = new_run()
    ask(s, st)
    w = answer(s, st, run, frag + "#1")
    assert w[f"click {frag}"] == pytest.approx(click * low) and w[f"click {frag}#1"] == click * k
    assert any("前のランまでに外れと分かっていた組み合わせ 1 通りを避けた" in n for n in run.flow.summary())


def test_flow_step_ops_are_not_worn_down_by_repeats():
    # run 2026-11-06_1418: 同じ質問に答え直すたびに同じボタンを押すので、押した回数で重みを下げると、
    # 答え直すほどドラッグ・Esc などの寄り道が増えた。流れの手順の操作は回数で下げない
    s, st, run, events = _session()
    _feed(st, "menu", name="MemoryListUI", modal=True, buttons=LIST)
    cands = normal_candidates(st, Guard({}), s.cfg["weights"], False)
    out = {op.label: op for op in s._apply_flow(cands, 0.0)}
    slot = out["click MemoryListUI/MemoryListGridSlot (Clone)"]
    assert not slot.novelty
    assert out["click MemoryListUI/SelectButton"].novelty  # 手順の外のボタンは今までどおり
    dec = run.decider
    for _ in range(50):
        dec.tries[("k", slot.label)] += 1
    assert dec.weight("k", slot) == slot.weight


def test_spent_choice_goes_back_instead_of_reopening_empty_list():
    # run 2026-11-06_1646: 言葉のない思い出で、「言葉を選ぶ」と空の言葉の一覧を閉じるのを繰り返した。
    # 選んだ思い出を試し終えたら（行き止まり）、流れから外れるボタン（要約を閉じる）で思い出の一覧へ戻るのを優先する
    s, st, run, _ = _session()
    profile = ONCE_PROFILE.replace('by = "game" }', 'by = "game" }\navoid = ["MemorySummaryUI/CloseMemorySummary"]', 1)
    run.flow = FlowTracker(parse_flows(tomllib.loads(profile)["flow"]))
    k, low, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"], FUZZ_DEFAULTS["weights"]["click"]
    _to_fragments(s, st, run, 0)
    _weights(s, st)
    assert run.flow.spent()
    _feed(st, "menu_close", name="MemoryFragmentListUI")
    _feed(st, "menu", name="MemorySummaryUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click MemorySummaryUI/CloseMemorySummary"] == click * k
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == click
    # 試し終えていない思い出なら、今までどおり「言葉を選ぶ」を優先し、要約を閉じるのは後回し
    run.flow.leaves.clear()
    w = _weights(s, st)
    assert w["click MemorySummaryUI/ShowFragmentListButton"] == click * k
    assert w["click MemorySummaryUI/CloseMemorySummary"] == pytest.approx(click * low)


def test_reveals_are_forgotten_when_progress_is_replaced():
    # run 2026-11-06_1646: 思い出を読んだあと起動し直して、はじめから遊んだ（開示が消えた）のに、読んだと覚えていた
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    _to_fragments(s, st, run, 0)
    _weights(s, st)
    tr = run.flow
    dead = (tr.ctx, (SLOT,))
    wrong = (tr.ctx, (f"{SLOT}#1", FRAG))
    tr.leaves.add(wrong)
    tr.wrong.add(wrong)
    assert tr.done_once and dead in tr.leaves
    # 流れの中のロード（思い出から本編に戻る）では忘れない
    s.on_event(Event("game", "load", {"slot": "MemoryReturn", "phase": "start"}, 0))
    assert tr.done_once and dead in tr.leaves
    # 流れの外でセーブをロードしたら、読んだ思い出と行き止まりを忘れる。外れと分かった答えは覚えておく
    tr.active = None
    s.on_event(Event("game", "load", {"slot": "GameSave004", "phase": "start"}, 0))
    assert not tr.done_once and dead not in tr.leaves and wrong in tr.leaves
    assert any("GameSave004" in e and "忘れた" in e for e in tr.events)


def test_answered_option_that_keeps_missing_is_no_longer_favored():
    # run 2026-11-06_2050: 前に押して読めた思い出のサムネが、起動し直してロードしたあとはロックされていた。押して反応した
    # ことがあるので「反応しないボタン」とせず、暗いものの番で流れの選び方として優先し続け、反応しないまま押し続けた
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    _to_fragments(s, st, run, 2)
    labels = [FRAG, f"{FRAG}#1"]
    s._dim = {labels[1]}
    ctx, path = run.flow.ctx, tuple(run.flow.path)
    run.flow.leaves.add((ctx, (*path, labels[0])))  # 明るいほうは外れだった
    run.answered.add(labels[1])
    run.silent.add(labels[1])
    run.miss_streak[labels[1]] = 1
    assert _weights(s, st)[f"click {labels[1]}"] == click * k  # 1回反応しなかっただけなら、まだ選ぶ
    run.miss_streak[labels[1]] = FUZZ_DEFAULTS["answered_miss_limit"]
    assert _weights(s, st)[f"click {labels[1]}"] == click  # 続けて反応しなければ、選ばない
