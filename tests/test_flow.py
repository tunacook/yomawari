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
start = { screen = "AlbumUI", by = "game" }
avoid = ["AlbumUI/SelectButton", "EpisodeUI/CloseEpisodeButton", "EpisodePlaying/OpenDialog"]
steps = [
  { screen = "AlbumUI", press = "AlbumSlot*", note = "開示済みの思い出を選ぶ" },
  { screen = "EpisodeUI", press = "ReadEpisodeButton", note = "思い出を読む" },
  { scene = "episodes/*", advance = true, note = "最後まで読む" },
  { screen = "EpisodeUI", press = "ShowWordsButton", note = "言葉を選ぶ" },
  { screen = "WordListUI", press = "WordSlot*", note = "言葉を選ぶ" },
  { screen = "AnswerDialogUI", press = "AnswerButton", note = "この言葉で答える" },
]

[[flow]]
name = "選択肢で答える"
start = { choices = true }
steps = [{ choose = ["*はい*"], note = "はいと答える" }]
"""

SUMMARY = [
    {"name": "ReadEpisodeButton", "rect": [0.42, 0.76, 0.61, 0.84]},
    {"name": "ShowWordsButton", "rect": [0.64, 0.76, 0.82, 0.83]},
    {"name": "CloseEpisodeButton", "rect": [0.18, 0.77, 0.24, 0.82]},
]
LIST = [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(3)]
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
    assert flows[0].steps[2].advance and flows[0].steps[2].when.scene == "episodes/*"
    assert flows[1].steps[0].when.choices and flows[1].steps[0].choose == ("*はい*",)
    spec = build_spec(tomllib.loads(PROFILE), {})
    assert len(spec.flows) == 2
    with pytest.raises(ConfigError, match="知らない項目"):
        parse_flows([{"name": "x", "start": {"screen": "A"}, "steps": [{"screen": "A", "click": "B"}]}])
    with pytest.raises(ConfigError, match="screen・scene・choices のどれか"):
        parse_flows([{"name": "x", "start": {}, "steps": [{"screen": "A"}]}])
    with pytest.raises(ConfigError, match="steps がありません"):
        parse_flows([{"name": "x", "start": {"screen": "A"}}])


def test_parse_answers():
    # 質問ごとの正解（DESIGN.md §9「答えの選び方」）。選ぶ手順（思い出・言葉）ごとに、画面に見える文字で書く
    raw = tomllib.loads(PROFILE)["flow"]
    raw[0]["answers"] = [{"at": "main/chapter1_04:44.0", "pick": ["No.030*", "屋上"], "note": "屋上の思い出"}]
    flows = parse_flows(raw)
    (ans,) = flows[0].answers
    assert ans.at == ("main/chapter1_04:44.0",) and ans.pick == ("No.030*", "屋上") and ans.note == "屋上の思い出"
    assert flows[1].answers == ()
    # 外れへの返事のあとに出し直す質問のように、同じ質問の行がいくつもあるときは配列で書ける
    raw[0]["answers"] = [{"at": ["main/chapter1_06:27.0", "main/chapter1_06:47.0"], "pick": ["No.030*", "玄関"]}]
    (ans,) = parse_flows(raw)[0].answers
    assert ans.at == ("main/chapter1_06:27.0", "main/chapter1_06:47.0")
    # 何も選ばずに一覧を閉じるのが正解の質問は、pick の代わりに押すボタンを press に書く
    raw[0]["answers"] = [{"at": "main/chapter4_04:303.0", "press": "AlbumUI/SelectButton"}]
    (ans,) = parse_flows(raw)[0].answers
    assert ans.press == "AlbumUI/SelectButton" and ans.pick == ()

    def bad(answers):
        f = {"name": "x", "start": {"choices": True}, "steps": [{"choose": True}], "answers": answers}
        parse_flows([f])
    with pytest.raises(ConfigError, match="知らない項目"):
        bad([{"at": "a:1", "pick": ["x"], "line": "a:1"}])
    with pytest.raises(ConfigError, match="at に質問"):
        bad([{"pick": ["x"]}])
    with pytest.raises(ConfigError, match="at に質問"):
        bad([{"at": [], "pick": ["x"]}])
    with pytest.raises(ConfigError, match="at に質問"):
        bad([{"at": ["a:1", ""], "pick": ["x"]}])
    with pytest.raises(ConfigError, match="pick に"):
        bad([{"at": "a:1", "pick": []}])
    with pytest.raises(ConfigError, match="どちらか一方"):
        bad([{"at": "a:1"}])
    with pytest.raises(ConfigError, match="どちらか一方"):
        bad([{"at": "a:1", "pick": ["x"], "press": "L/Close"}])
    with pytest.raises(ConfigError, match="press に"):
        bad([{"at": "a:1", "press": ""}])
    with pytest.raises(ConfigError, match="選ぶ手順は 1 個"):
        bad([{"at": "a:1", "pick": ["x", "y"]}])


def test_follows_question_flow_and_avoids_leaving():
    s, st, run, events = _session()
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    # ハーネスがボタンで開いた一覧（寄り道）では始めない
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    run.opened_by["AlbumUI"] = "QuickMenuUI/ToAlbum"
    assert _weights(s, st)["click AlbumUI/SelectButton"] == click and run.flow.active is None
    # ゲームが開いた一覧（質問）: 思い出を選ぶのを優先し、「思い出を選ばない」は後回し
    del run.opened_by["AlbumUI"]
    w = _weights(s, st)
    assert run.flow.active.name == "質問に答える" and run.flow.index == 0
    # 選ぶ手順は、候補の順番の先頭（目標）だけを優先し、ほかの候補はふだんの重み
    slots = {k_: v for k_, v in w.items() if "AlbumSlot" in k_}
    goal = next(k_ for k_, v in slots.items() if v == click * k)
    assert sorted(slots.values()) == [click, click, click * k]
    assert w["click AlbumUI/SelectButton"] == pytest.approx(click * low)
    # 押しても反応しなかったスロット（ロック中）は、手順のボタンでも優先しない（目標は次の候補へ移る）
    run.silent.add(goal.removeprefix("click "))
    w = _weights(s, st)
    assert w[goal] == click and sum(1 for k_, v in w.items() if "AlbumSlot" in k_ and v == click * k) == 1
    # 要約が出た（読む前）: 「この思い出を読む」
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 1
    assert w["click EpisodeUI/ReadEpisodeButton"] == click * k
    assert w["click EpisodeUI/ShowWordsButton"] == click
    assert w["click EpisodeUI/CloseEpisodeButton"] == pytest.approx(click * low)
    # 思い出を読んでいる: 本文送りを優先し、「本編へ戻る」は後回し
    _feed(st, "menu_close", name="EpisodeUI")
    _feed(st, "menu_close", name="AlbumUI")
    _feed(st, "scene", name="episodes/episode_11")
    _feed(st, "menu", name="EpisodePlaying", modal=False,
          buttons=[{"name": "OpenDialog", "rect": [0, 0.0, 0.23, 0.08]}])
    _feed(st, "line", line_id="episodes/episode_11:1", text="a")
    w = _weights(s, st)
    assert run.flow.index == 2
    assert w["advance"] == FUZZ_DEFAULTS["weights"]["advance"] * k
    assert w["click EpisodePlaying/OpenDialog"] == pytest.approx(click * low)
    # 読み終えて要約に戻った: 同じ画面でも、手順の順番で「言葉を選ぶ」
    _feed(st, "menu_close", name="EpisodePlaying")
    _feed(st, "scene", name="main/chapter1_04")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click EpisodeUI/ShowWordsButton"] == click * k
    assert w["click EpisodeUI/ReadEpisodeButton"] == click
    # 言葉の一覧 → 確認ダイアログ → 答える
    _feed(st, "menu", name="WordListUI", modal=True, buttons=[
        {"name": "WordSlot(Clone)", "rect": [0.2, 0.2, 0.4, 0.3]},
        {"name": "ReturnButton", "rect": [0.07, 0.9, 0.13, 0.95]},
    ])
    assert _weights(s, st)["click WordListUI/WordSlot(Clone)"] == click * k
    _feed(st, "menu", name="AnswerDialogUI", modal=True, buttons=[
        {"name": "AnswerButton", "rect": [0.5, 0.6, 0.7, 0.7]},
        {"name": "AnswerCancelButton", "rect": [0.3, 0.6, 0.45, 0.7]},
    ])
    assert _weights(s, st)["click AnswerDialogUI/AnswerButton"] == click * k
    assert run.flow.index == 5
    run.flow.pressed("AnswerDialogUI/AnswerButton", "AnswerButton", True)
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
        _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
        assert _weights(s, st)["click AlbumUI/SelectButton"] == FUZZ_DEFAULTS["weights"]["click"]
        assert run.flow.active is None


def test_flow_ending_with_reading_completes_when_leaving_it():
    flows = parse_flows(tomllib.loads("""
[[flow]]
name = "思い出を読む"
start = { screen = "AlbumUI", by = "harness" }
steps = [
  { screen = "AlbumUI", press = "AlbumSlot*" },
  { scene = "episodes/*", advance = true },
]
""")["flow"])
    tracker = FlowTracker(flows)
    st = GameState()
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    tracker.update(st, {"AlbumUI": "QuickMenuUI/ToAlbum"}, 0, 10)
    _feed(st, "menu_close", name="AlbumUI")
    _feed(st, "scene", name="episodes/m1")
    _feed(st, "line", line_id="episodes/m1:1", text="a")
    assert tracker.update(st, {}, 1, 10).advance
    # 読み終えて本編に戻った
    _feed(st, "scene", name="main/s2")
    tracker.update(st, {}, 2, 10)
    assert tracker.active is None and tracker.stats["思い出を読む"].completed == 1


TRIAL = """
[[flow]]
name = "質問に答える"
start = { screen = "AlbumUI", by = "game" }
steps = [
  { screen = "AlbumUI", press = "AlbumSlot*" },
  { screen = "EpisodeUI", press = "ReadEpisodeButton", once = true },
  { scene = "episodes/*", advance = true, once = true },
  { screen = "EpisodeUI", press = "ShowWordsButton" },
  { screen = "WordListUI", press = "WordSlot*" },
  { screen = "AnswerDialogUI", press = "Accept" },
]
"""
FRAGS = [{"name": "WordSlot(Clone)", "rect": [0.2 + 0.2 * i, 0.2, 0.35 + 0.2 * i, 0.3]} for i in range(2)]


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
    slot = "AlbumUI/AlbumSlot (Clone)"
    frag = "WordListUI/WordSlot(Clone)"

    def ask():
        _feed(st, "scene", name="main/s6")
        _feed(st, "line", line_id="main/s6:70", text="ねぇ、どこだって？")
        _only(st, "AlbumUI", LIST)

    def answer(memory, fragment, read):
        _press(s, st, run, memory)
        _only(st, "EpisodeUI", SUMMARY)
        w = _weights(s, st)
        if read:
            assert w["click EpisodeUI/ReadEpisodeButton"] == click * k
            run.flow.pressed("EpisodeUI/ReadEpisodeButton", "ReadEpisodeButton", True)
            for m in list(st.menus):
                _feed(st, "menu_close", name=m)
            _feed(st, "scene", name="episodes/m1")
            _feed(st, "line", line_id="episodes/m1:1", text="a")
            _weights(s, st)
            _feed(st, "scene", name="main/s6")
            _only(st, "EpisodeUI", SUMMARY)
            w = _weights(s, st)
        else:
            # 読んだ思い出は読み直さず、言葉を選ぶ
            assert w["click EpisodeUI/ReadEpisodeButton"] == pytest.approx(click * low)
        assert w["click EpisodeUI/ShowWordsButton"] == click * k
        run.flow.pressed("EpisodeUI/ShowWordsButton", "ShowWordsButton", True)
        _only(st, "WordListUI", FRAGS)
        w = _press(s, st, run, fragment)
        _only(st, "AnswerDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
        _press(s, st, run, "AnswerDialogUI/Accept")
        assert run.flow.active is None
        return w

    ask()
    w = _weights(s, st)
    # どれもまだ試していない: 目標（順番の先頭）の1つだけを優先する
    assert sorted(w[f"click {slot}{n}"] for n in ("", "#1", "#2")) == [click, click, click * k]
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
    assert sorted(w[f"click {slot}{n}"] for n in ("#1", "#2")) == [click, click * k]
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
    slot = "AlbumUI/AlbumSlot (Clone)"
    frag = "WordListUI/WordSlot(Clone)"

    def ask(line_ids):
        _feed(st, "scene", name="main/s6")
        for n, line_id in enumerate(line_ids):
            _feed(st, "line", line_id=line_id, text=f"返事{n}")
        _only(st, "AlbumUI", LIST)

    def answer(fragment, show_list):
        _press(s, st, run, slot)
        _only(st, "EpisodeUI", SUMMARY)
        _weights(s, st)
        if show_list:
            run.flow.pressed("EpisodeUI/ShowWordsButton", "ShowWordsButton", True)
        _only(st, "WordListUI", FRAGS)
        w = _press(s, st, run, fragment)
        _only(st, "AnswerDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
        _press(s, st, run, "AnswerDialogUI/Accept")
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
    assert w[f"click {slot}"] == pytest.approx(click * low)
    assert sorted(w[f"click {slot}{n}"] for n in ("#1", "#2")) == [click, click * k]
    assert run.flow.stats["質問に答える"].retried == 2
    # 答えてから本文が flow_retry_lines を超えて進んだら、別の質問
    run.flow.active = None
    for n in range(FUZZ_DEFAULTS["flow_retry_lines"] + 1):
        s.on_event(Event("game", "line", {"line_id": f"main/s6:{100 + n}", "text": "続き"}, 0.0, 0))
    _only(st, "AlbumUI", LIST)
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
    # どちらも外れた: どれも同じ扱いに戻して（目標の1つだけを優先し）、記録に書く
    click = FUZZ_DEFAULTS["weights"]["click"]
    assert sorted([w["click choices/はい"], w["click choices/いいえ"]]) == [click, click * FUZZ_DEFAULTS["flow_factor"]]
    assert any("2 通り）をすべて試しても先に進めなかった" in n for n in run.flow.summary())


def test_all_wrong_answers_are_rotated_not_repeated():
    # run 2026-11-07_1343: 2 つとも外れと覚えた選択肢で、先頭の「話す」を 16 回選んでバッドエンドを繰り返した。
    # 試せる組み合わせがないときは、このランで選んだ回数の少ないものから選び直す
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads("""
[[flow]]
name = "選択肢で答える"
start = { choices = true }
steps = [{ choose = true }]
""")["flow"]))
    q = ("選択肢で答える", "main/s1:5")
    run.flow.remember({(q, ("はい",)), (q, ("いいえ",))})
    items = [{"id": "a", "text": "はい", "rect": [0.4, 0.4, 0.6, 0.5]}, {"id": "b", "text": "いいえ", "rect": [0.4, 0.5, 0.6, 0.6]}]
    boost = FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["flow_factor"]
    picks = []
    for n in range(4):
        _feed(st, "line", line_id="main/s1:5", text="行く？")
        # 選択肢の id は出るたびに変わる
        items = [{**it, "id": f"{it['id']}{n}"} for it in items]
        _feed(st, "choices", items=items)
        w = _weights(s, st)
        text = next(t for t in ("はい", "いいえ") if w[f"click choices/{t}"] == pytest.approx(boost))
        picks.append(text)
        run.flow.pressed(f"choices/{text}", "a", True, text)
        _feed(st, "choices_done", id=items[0 if text == "はい" else 1]["id"])
    assert sorted(picks) == ["いいえ", "いいえ", "はい", "はい"]
    assert picks[0] != picks[1] and picks[2] != picks[3]


def test_screen_step_wins_over_lingering_scene_step():
    # run 0946: 思い出を読み終えて要約に戻っても、答えるまで scene が episodes/… のまま。「思い出を読む」手順（scene だけ）に
    # とどまらず、要約・言葉の一覧・確認ダイアログの手順に進み、答えたら最後まで進めたとする（試した組み合わせも覚える）
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(TRIAL)["flow"]))
    _feed(st, "scene", name="main/s4")
    _feed(st, "line", line_id="main/s4:44", text="どこ？")
    _only(st, "AlbumUI", LIST)
    _press(s, st, run, "AlbumUI/AlbumSlot (Clone)")
    _only(st, "EpisodeUI", SUMMARY)
    _press(s, st, run, "EpisodeUI/ReadEpisodeButton")
    for m in list(st.menus):
        _feed(st, "menu_close", name=m)
    _feed(st, "scene", name="episodes/m1")
    _feed(st, "line", line_id="episodes/m1:1", text="a")
    _weights(s, st)
    assert run.flow.index == 2
    _only(st, "EpisodeUI", SUMMARY)  # scene は episodes/m1 のまま
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click EpisodeUI/ShowWordsButton"] == FUZZ_DEFAULTS["weights"]["click"] * FUZZ_DEFAULTS["flow_factor"]
    run.flow.pressed("EpisodeUI/ShowWordsButton", "ShowWordsButton", True)
    _only(st, "WordListUI", FRAGS)
    _press(s, st, run, "WordListUI/WordSlot(Clone)#1")
    _only(st, "AnswerDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
    _press(s, st, run, "AnswerDialogUI/Accept")
    assert run.flow.active is None and run.flow.stats["質問に答える"].completed == 1
    assert len(run.flow.leaves) == 1


def test_steps_can_name_buttons_by_visible_text():
    # 手順のボタンを、GameObject の名前ではなく、画面に見える文字（画面から読んだもの）で書く。空白は除いて比べる
    flows = parse_flows(tomllib.loads("""
[[flow]]
name = "読む"
start = { screen = "EpisodeUI" }
avoid_text = ["閉じる"]
steps = [
  { screen = "EpisodeUI", text = "*読む*" },
  { scene = "episodes/*", advance = true },
]
""")["flow"])
    s, st, run, _ = _session()
    run.flow = FlowTracker(flows)
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    _only(st, "EpisodeUI", SUMMARY)
    st.set_read_text({"EpisodeUI/ReadEpisodeButton": "この 思い出を 読む", "EpisodeUI/CloseEpisodeButton": "閉じる",
                      "EpisodeUI/ShowWordsButton": "言葉を選ぶ"})
    w = _weights(s, st)
    assert w["click EpisodeUI/ReadEpisodeButton"] == click * k
    assert w["click EpisodeUI/ShowWordsButton"] == click
    assert w["click EpisodeUI/CloseEpisodeButton"] == pytest.approx(click * low)
    # 文字がまだ読めていないボタンは、手順のボタンとみなさない
    st.read_text.clear()
    _only(st, "EpisodeUI", SUMMARY)
    assert _weights(s, st)["click EpisodeUI/ReadEpisodeButton"] == click
    with pytest.raises(ConfigError, match="知らない項目"):
        parse_flows([{"name": "x", "start": {"screen": "A"}, "steps": [{"screen": "A"}], "avoid_txt": ["a"]}])


ONCE_PROFILE = """
[[flow]]
name = "質問に言葉で答える"
start = { screen = "AlbumUI", by = "game" }
steps = [
  { screen = "AlbumUI", press = "AlbumSlot*", note = "開示済みの思い出を選ぶ" },
  { screen = "EpisodeUI", press = "ReadEpisodeButton", once = true, note = "この思い出を読む" },
  { scene = "episodes/*", advance = true, once = true, note = "最後まで読む" },
  { screen = "EpisodeUI", press = "ShowWordsButton", note = "言葉を選ぶ" },
  { screen = "WordListUI", press = "WordSlot*", note = "開示済みの言葉を選ぶ" },
]
"""
FRAG = "WordListUI/WordSlot(Clone)"
SLOT = "AlbumUI/AlbumSlot (Clone)"


def _to_fragments(s, st, run, slots):
    """質問の一覧で 1 つめの思い出を選び、言葉の一覧まで進める（読むのは済ませたものとして飛ばす）。"""
    _feed(st, "line", line_id="main/chapter2_04:323.0", text="アカウント名を選ぼう。")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    _weights(s, st)
    run.flow.pressed(SLOT, "AlbumSlot (Clone)", True)
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    _weights(s, st)
    run.flow.pressed("EpisodeUI/ReadEpisodeButton", "ReadEpisodeButton", True)
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    run.flow.index = 3
    _weights(s, st)
    run.flow.pressed("EpisodeUI/ShowWordsButton", "ShowWordsButton", True)
    _feed(st, "menu", name="WordListUI", modal=True, buttons=[
        *[{"name": "WordSlot(Clone)", "rect": [0.1 + 0.15 * i, 0.4, 0.2 + 0.15 * i, 0.5]} for i in range(slots)],
        {"name": "ReturnButton", "rect": [0.07, 0.9, 0.13, 0.95]},
    ])


def test_dim_options_are_tried_after_bright_ones():
    # run 2026-11-05_1346: 「No.012 雨の日の約束」の言葉の一覧で、下の3枚（答えの「sample0101」を含む）が暗く写り、
    # 押せないとみなして選ばずに、上の3枚を試し終えたら「すべて試した」としていた。暗いものも、明るいものの次に試す
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    _to_fragments(s, st, run, 4)
    labels = [FRAG, f"{FRAG}#1", f"{FRAG}#2", f"{FRAG}#3"]
    s._dim = {labels[2], labels[3]}
    w = _weights(s, st)
    assert run.flow.index == 4
    # 明るいものを先に（目標は明るい2枚のどちらか）
    assert sorted(w[f"click {x}"] for x in labels[:2]) == [click, click * k]
    assert [w[f"click {x}"] for x in labels[2:]] == [click, click]
    # 明るい2枚は外れだった: 暗いものの番（目標は暗い2枚のどちらか）
    ctx, path = run.flow.ctx, tuple(run.flow.path)
    run.flow.leaves |= {(ctx, (*path, labels[0])), (ctx, (*path, labels[1]))}
    w = _weights(s, st)
    assert sorted(w[f"click {x}"] for x in labels[2:]) == [click, click * k]
    assert not any("すべて試しても" in e for e in run.flow.events)
    # 押して反応がなかったもの（押して確かめた）は、暗いものの番になっても選ばない
    run.silent.add(labels[3])
    assert _weights(s, st)[f"click {labels[3]}"] == click
    run.flow.leaves.add((ctx, (*path, labels[2])))
    _weights(s, st)
    assert any("すべて試しても" in e for e in run.flow.events)


def test_empty_fragment_list_is_dead_end_and_reading_is_remembered():
    # run 2026-11-05_1346: 言葉が1つも開示されていない思い出（一覧には戻るボタンだけ）を、選ぶたびに
    # 読み直していた。選べるものがない一覧は行き止まりとし、手順の順番から外れて読んだときも読んだと覚える
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    _to_fragments(s, st, run, 0)
    _weights(s, st)
    assert (run.flow.ctx, (SLOT,)) in run.flow.leaves
    # 言葉の一覧から戻って、要約で「この思い出を読む」を押した（いまの手順は言葉を選ぶ）
    run.flow.done_once.clear()
    run.flow.pressed("EpisodeUI/ReadEpisodeButton", "ReadEpisodeButton", True)
    assert run.flow.is_done(1)
    # 同じ質問で同じ思い出を選び直したときも、読んだ思い出は読み直さない（「この思い出を読む」は後回し）
    run.flow.path = [SLOT]
    run.flow.index = 1
    assert run.flow.is_done(1)


def test_read_text_is_forgotten_when_menu_closes():
    # 言葉のスロットの名前は並びの番号なので、別の思い出の一覧でも同じ名前になる。閉じたら読んだ文字を忘れる
    st = GameState()
    _feed(st, "menu", name="WordListUI", modal=True,
          buttons=[{"name": "WordSlot(Clone)", "rect": [0.1, 0.4, 0.2, 0.5]}])
    st.set_read_text({FRAG: "階段", "AlbumUI/X": "No.012"})
    _feed(st, "menu_close", name="WordListUI")
    assert st.read_text == {"AlbumUI/X": "No.012"} and st.closed == {"WordListUI"}
    _feed(st, "menu", name="WordListUI", modal=True,
          buttons=[{"name": "WordSlot(Clone)", "rect": [0.1, 0.4, 0.2, 0.5]}])
    assert st.find(FRAG).text is None


def test_dim_options_wait_while_bright_one_is_off_screen():
    # run 2026-11-05_1811: 質問の一覧で、開示済みの思い出（明るい）が画面の外にあり、見えているのはロック中（暗い）
    # ものだけだった。明るいものを試し終えたと誤って、ロック中の思い出を次々に押していた。画面の外の明るいものを先に探す
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    click = FUZZ_DEFAULTS["weights"]["click"]
    slots = [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(3)]
    slots.append({"name": "AlbumSlot (Clone)", "rect": [0.1, 1.3, 0.25, 1.5], "hidden": True})
    _feed(st, "line", line_id="main/chapter1_04:44.0", text="あの時——")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=slots)
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
    _feed(st, "line", line_id="main/chapter1_04:44.0", text="あの時——")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    _weights(s, st)
    run.flow.pressed(SLOT, "AlbumSlot (Clone)", True)
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    assert _weights(s, st)["click EpisodeUI/ReadEpisodeButton"] == click * k
    # 読まずに「言葉を選ぶ」を押した。言葉の一覧は空（読む前なので当たり前）で、行き止まりにはしない
    run.flow.pressed("EpisodeUI/ShowWordsButton", "ShowWordsButton", True)
    _feed(st, "menu", name="WordListUI", modal=True,
          buttons=[{"name": "ReturnButton", "rect": [0.07, 0.9, 0.13, 0.95]}])
    _weights(s, st)
    assert run.flow.index == 4 and not run.flow.leaves
    # 要約に戻った: まだ読んでいないので「この思い出を読む」からやり直す
    _feed(st, "menu_close", name="WordListUI")
    w = _weights(s, st)
    assert run.flow.index == 1 and run.flow.path == [SLOT]
    assert w["click EpisodeUI/ReadEpisodeButton"] == click * k
    # 要約を閉じて思い出の一覧に戻った: 思い出を選ぶところからやり直す
    _feed(st, "menu_close", name="EpisodeUI")
    w = _weights(s, st)
    assert run.flow.index == 0 and run.flow.path == []
    assert sorted(w[f"click {x}"] for x in (SLOT, f"{SLOT}#1", f"{SLOT}#2")) == [click, click, click * k]
    # 一覧のまま選び直したら、選んだものを置き換える
    run.flow.pressed(f"{SLOT}#1", "AlbumSlot (Clone)", True)
    run.flow.pressed(f"{SLOT}#2", "AlbumSlot (Clone)", True)
    assert run.flow.path == [f"{SLOT}#2"]
    # 読んだ思い出の要約に、言葉の一覧から戻ったときは「言葉を選ぶ」
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    _weights(s, st)
    run.flow.pressed("EpisodeUI/ReadEpisodeButton", "ReadEpisodeButton", True)
    run.flow.index = 4
    _feed(st, "menu_close", name="EpisodeUI")
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click EpisodeUI/ShowWordsButton"] == click * k


def test_flow_fills_the_ledger_then_goes_to_an_off_screen_option():
    # run 2026-11-05_2230: 質問の一覧が最後のほうで開き、見えている思い出 2 つの言葉をすべて試しても、一覧を動かさずに
    # 同じ 2 つを 1 時間試し続けた。台帳（DESIGN.md §9「一覧の台帳と目標」）では、まず見えていない項目を見にいって
    # 台帳を埋め、候補の順番の先頭（目標）へまっすぐ向かう
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    k, low, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"], FUZZ_DEFAULTS["weights"]["click"]
    lf = FUZZ_DEFAULTS["list_factor"]

    def slots(top):
        # top: 上の 2 つ（#2・#3）が見えているか。見えていなければ、上の 2 つは画面の上、下の 2 つが見えている
        y = (0.3, 1.3) if top else (-0.7, 0.3)
        out = [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, y[0] + (0 if top else 1.0),
                                                              0.25 + 0.2 * i, y[0] + 0.2 + (0 if top else 1.0)]}
               for i in range(2)]
        out += [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, y[0] if top else -0.7,
                                                               0.25 + 0.2 * i, (y[0] + 0.2) if top else -0.5]}
                for i in range(2)]
        for b in out:
            cy = (b["rect"][1] + b["rect"][3]) / 2
            if not 0 <= cy <= 1:
                b["hidden"] = True
        return out

    _feed(st, "line", line_id="main/chapter2_04:323.0", text="アカウント名を選ぼう。")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=slots(top=False))
    w = _weights(s, st)
    # 上の 2 つがまだ見えていない: 台帳を埋めるために上へ動かす（押す目標はまだ決めない）
    assert w["wheel_up 3"] == w["drag down"] == click * lf
    assert w[f"click {SLOT}"] == w[f"click {SLOT}#1"] == click
    assert any("台帳を埋め始めた" in n for n in run.notes)
    ctx = run.flow.ctx
    run.flow.leaves |= {(ctx, (SLOT,)), (ctx, (f"{SLOT}#1",))}
    # 上へ動かした: 上の 2 つが見えた（#3 は伏せ字）
    run.masked_chars = "?？"
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=slots(top=True))
    st.set_read_text({f"{SLOT}#3": "？？？"})
    w = _weights(s, st)
    assert run.ledger.items[f"{SLOT}#3"].status == "masked"
    assert w[f"click {SLOT}#2"] == click * k  # 目標: まだ試していない #2（伏せ字の #3 は選ばない）
    assert w[f"click {SLOT}#3"] == click
    # 下へ戻っても、目標の #2 へ向かう（上へ動かす）
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=slots(top=False))
    w = _weights(s, st)
    ops = {op.label: op for op in s._apply_flow(normal_candidates(st, Guard({}), s.cfg["weights"], False), 0.0)}
    assert ops["wheel_up 3"].goal == f"{SLOT}#2" and ops["wheel_up 3"].by == "flow"
    assert w["wheel_down 3"] < click * lf
    assert w[f"click {SLOT}"] == w[f"click {SLOT}#1"] == pytest.approx(click * low)
    assert not any("すべて試しても" in e for e in run.flow.events)
    # 画面の外のものも試し終えたら（残りは伏せ字の #3 だけ）、すべて試したと記録する
    run.flow.leaves.add((ctx, (f"{SLOT}#2",)))
    _weights(s, st)
    assert any("すべて試しても" in e for e in run.flow.events)


def test_flow_goal_keeps_one_direction_and_gives_up():
    # run 2026-11-06_2334・2026-11-07_0849: 縦にも横にも広い思い出の一覧で、まだ試していない思い出が上にも下にも右にもあり、
    # 上下左右へ動かす操作を混ぜて候補に入れた。逆の向きへ動かして打ち消し合い、行ったり来たりした。目標は 1 つだけにし、
    # その向きへだけ動かす。続けて近づかなければ、その目標はあきらめて次の目標にする
    s, st, run, _ = _session()
    run.flow = FlowTracker(parse_flows(tomllib.loads(ONCE_PROFILE)["flow"]))
    click, lf = FUZZ_DEFAULTS["weights"]["click"], FUZZ_DEFAULTS["list_factor"]
    give_up = FUZZ_DEFAULTS["list_move_give_up"]
    slots = [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(2)]
    slots += [{"name": "AlbumSlot (Clone)", "rect": [0.1, -0.5, 0.25, -0.3], "hidden": True},  # 上（遠い）
              {"name": "AlbumSlot (Clone)", "rect": [0.1, 1.1, 0.25, 1.3], "hidden": True},  # 下（近い）
              {"name": "AlbumSlot (Clone)", "rect": [1.25, 0.3, 1.4, 0.5], "hidden": True}]  # 右（中くらい）
    _feed(st, "line", line_id="main/chapter2_04:323.0", text="アカウント名を選ぼう。")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=slots)
    _weights(s, st)
    for item in run.ledger.items.values():
        item.shown = True  # 台帳は埋め終えた（前に見た）
    ctx = run.flow.ctx
    run.flow.leaves |= {(ctx, (SLOT,)), (ctx, (f"{SLOT}#1",))}

    def boosted():
        # 目標へ向かう操作（goal の付いた、動かす操作）
        ops = s._apply_flow(normal_candidates(st, Guard({}), s.cfg["weights"], False), 0.0)
        return {op.label for op in ops if op.goal is not None and op.target is None and not op.label.startswith("key")}

    assert boosted() == {"wheel_down 3", "drag up"}  # いちばん近い下の #3 だけ
    w = _weights(s, st)
    assert w["drag up"] == click * lf
    goal = run.ledger.goals["AlbumUI/AlbumSlot (Clone)"]
    assert goal.label == f"{SLOT}#3"
    # 下へ動かしても目標が近づかない（端で跳ね返るだけ）: give_up 回までは下のまま、そのあとは次に近い右
    # （近づかなかった手段のホイールは後回しにし、ドラッグを続ける）
    for n in range(give_up):
        now = boosted()
        assert now == {"wheel_down 3", "drag up"}, n
        s._moving = ("AlbumUI/AlbumSlot (Clone)", f"{SLOT}#3", "drag up", goal.dist)
    assert boosted() == {"drag left"}
    assert f"{SLOT}#3" in run.ledger.unreachable
    assert any("近づかなかった" in n for n in run.notes)
    # 一覧を閉じたら、届かなかった目標も忘れる
    _feed(st, "menu_close", name="AlbumUI")
    s._sync_lists()
    assert not run.ledger.unreachable and not run.ledger.goals


def test_wrong_answers_are_remembered_across_runs(tmp_path):
    # 外れと分かった組み合わせ（同じ質問にまた戻ってきた、その前の答え）を runs/_experience に覚え、次のランで避ける。
    # 最後の答え（そのあと同じ質問に戻らなかった）は正解かもしれないので覚えない
    from yomawari.fuzz.experience import Experience

    slot = "AlbumUI/AlbumSlot (Clone)"
    frag = "WordListUI/WordSlot(Clone)"
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
        _only(st, "AlbumUI", LIST)

    def answer(s, st, run, fragment):
        _press(s, st, run, slot)
        _only(st, "EpisodeUI", SUMMARY)
        _weights(s, st)
        run.flow.pressed("EpisodeUI/ShowWordsButton", "ShowWordsButton", True)
        _only(st, "WordListUI", FRAGS)
        w = _press(s, st, run, fragment)
        _only(st, "AnswerDialogUI", [{"name": "Accept", "rect": [0.5, 0.6, 0.7, 0.7]}])
        _press(s, st, run, "AnswerDialogUI/Accept")
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
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    cands = normal_candidates(st, Guard({}), s.cfg["weights"], False)
    out = {op.label: op for op in s._apply_flow(cands, 0.0)}
    slot = next(op for op in out.values() if op.goal is not None)  # 手順の目標の思い出
    assert not slot.novelty and slot.by == "flow"
    assert out["click AlbumUI/SelectButton"].novelty  # 手順の外のボタンは今までどおり
    dec = run.decider
    for _ in range(50):
        dec.tries[("k", slot.label)] += 1
    assert dec.weight("k", slot) == slot.weight


def test_spent_choice_goes_back_instead_of_reopening_empty_list():
    # run 2026-11-06_1646: 言葉のない思い出で、「言葉を選ぶ」と空の言葉の一覧を閉じるのを繰り返した。
    # 選んだ思い出を試し終えたら（行き止まり）、流れから外れるボタン（要約を閉じる）で思い出の一覧へ戻るのを優先する
    s, st, run, _ = _session()
    profile = ONCE_PROFILE.replace('by = "game" }', 'by = "game" }\navoid = ["EpisodeUI/CloseEpisodeButton"]', 1)
    run.flow = FlowTracker(parse_flows(tomllib.loads(profile)["flow"]))
    k, low, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"], FUZZ_DEFAULTS["weights"]["click"]
    _to_fragments(s, st, run, 0)
    _weights(s, st)
    assert run.flow.spent()
    _feed(st, "menu_close", name="WordListUI")
    _feed(st, "menu", name="EpisodeUI", modal=True, buttons=SUMMARY)
    w = _weights(s, st)
    assert run.flow.index == 3
    assert w["click EpisodeUI/CloseEpisodeButton"] == click * k
    assert w["click EpisodeUI/ShowWordsButton"] == click
    # 試し終えていない思い出なら、今までどおり「言葉を選ぶ」を優先し、要約を閉じるのは後回し
    run.flow.leaves.clear()
    w = _weights(s, st)
    assert w["click EpisodeUI/ShowWordsButton"] == click * k
    assert w["click EpisodeUI/CloseEpisodeButton"] == pytest.approx(click * low)


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
    s.on_event(Event("game", "load", {"slot": "EpisodeReturn", "phase": "start"}, 0))
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


def test_flow_records_its_factor_on_ops():
    # 流れが重みを変えた操作には、why に ("flow", 倍率) が残る（events.jsonl の op で、なぜ選ばれたかを見る）
    s, st, run, _ = _session()
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=LIST)
    cands = normal_candidates(st, Guard({}), s.cfg["weights"], False)
    ops = {op.label: op for op in s._apply_flow(cands, 0.0)}
    goal = next(op for op in ops.values() if op.goal is not None)
    assert goal.why == (("flow", k),) and goal.target.name == "AlbumSlot (Clone)"
    assert ops["click AlbumUI/SelectButton"].why == (("flow", low),)


def test_read_memory_is_remembered_across_flows_and_outside_them():
    # 質問の流れで読んだ思い出（once の手順）は、ほかの流れ（once を書いていない「思い出を読む」）や流れの外でも
    # 読み直さない（run 2026-11-07_1511: episode_11 を 4 回、7021 を 3 回読んだ）
    flows = parse_flows(tomllib.loads("""
[[flow]]
name = "質問"
start = { screen = "AlbumUI", by = "game" }
steps = [
  { screen = "AlbumUI", press = "AlbumSlot*" },
  { screen = "EpisodeUI", press = "ReadEpisodeButton", once = true },
  { screen = "EpisodeUI", press = "ShowWordsButton" },
]

[[flow]]
name = "思い出を読む"
start = { screen = "AlbumUI", by = "harness" }
steps = [
  { screen = "AlbumUI", press = "AlbumSlot*" },
  { screen = "EpisodeUI", press = "ReadEpisodeButton" },
]
""")["flow"])
    tr = FlowTracker(flows)
    read = ("EpisodeUI/ReadEpisodeButton", "ReadEpisodeButton", None)
    # 流れの外で思い出 #1 を選んで読んだ
    tr.seen("AlbumUI/AlbumSlot (Clone)#1", "AlbumSlot (Clone)", None)
    assert not tr.read_before(*read)
    tr.seen(*read)
    assert tr.read_before(*read)
    # ほかの思い出を選んだら、まだ読んでいない
    tr.seen("AlbumUI/AlbumSlot (Clone)#2", "AlbumSlot (Clone)", None)
    assert not tr.read_before(*read)
    # #1 に戻れば読んだもの
    tr.seen("AlbumUI/AlbumSlot (Clone)#1", "AlbumSlot (Clone)", None)
    assert tr.read_before(*read)
    # 質問の流れの中でも、同じ選び方なら済ませたとみなす（流れの名前を鍵に入れない）
    tr.active, tr.index, tr.path = flows[0], 1, ["AlbumUI/AlbumSlot (Clone)#1"]
    assert tr.is_done(1)


ANSWER_PROFILE = """
[[flow]]
name = "選択肢で答える"
start = { choices = true }
steps = [{ choose = [], note = "選択肢で答える" }]
answers = [{ at = "main/s1:1*", pick = ["いいえ"], note = "いいえが正解" }]
"""
YES_NO = [{"id": "a", "text": "はい", "rect": [0.4, 0.4, 0.6, 0.5]},
          {"id": "b", "text": "いいえ", "rect": [0.4, 0.5, 0.6, 0.6]}]


def _answer_session(cfg):
    s, st, run, _ = _session({"ai_context_lines": 3, **cfg})
    run.flow = FlowTracker(parse_flows(tomllib.loads(ANSWER_PROFILE)["flow"]))
    return s, st, run


def _line(s, st, line_id, text):
    # 本文の行は、状態とハーネスの両方に流す（ハーネスは行を覚えて、プロファイルの正解の質問と照らす）
    ev = Event("game", "line", {"line_id": line_id, "text": text}, 0)
    st.feed(ev)
    s.on_event(ev)


def _ask_yes_no(s, st, line_id="main/s1:10"):
    _feed(st, "choices_done")
    _line(s, st, line_id, "どうする？")
    _feed(st, "choices", items=YES_NO)


def _choose(s, run, text, cid):
    run.flow.pressed(f"choices/{text}", cid, True, text)
    s._answered()


def test_profile_answer_is_tried_first():
    # プロファイルの正解（質問の行が at に当たる）を、ほかの候補より先に選ぶ
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    s, st, run = _answer_session({"wrong_answer_prob": 0.0})
    _ask_yes_no(s, st)
    w = _weights(s, st)
    assert w["click choices/いいえ"] == click * k and w["click choices/はい"] == click
    # 行が当たらない質問では、正解が分からない
    s, st, run = _answer_session({"wrong_answer_prob": 0.0})
    _ask_yes_no(s, st, "main/s2:10")
    _weights(s, st)
    assert s._answer_options({"はい", "いいえ"}, {"はい": "はい", "いいえ": "いいえ"}) is None
    # answer_source が learn なら、プロファイルの正解を使わない
    s, st, run = _answer_session({"wrong_answer_prob": 0.0, "answer_source": "learn"})
    _ask_yes_no(s, st)
    _weights(s, st)
    assert s._answer_options({"はい", "いいえ"}, {"はい": "はい", "いいえ": "いいえ"}) is None


def test_deliberate_wrong_answer_then_the_right_one():
    # wrong_answer_prob の確率で、正解の前にわざと外れを答える。外れは wrong_answers_per_question 回まで、そのあと正解
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]
    s, st, run = _answer_session({"wrong_answer_prob": 1.0})
    _ask_yes_no(s, st)
    w = _weights(s, st)
    assert w["click choices/はい"] == click * k and w["click choices/いいえ"] == click
    _choose(s, run, "はい", "a")
    assert any("わざと外れを 1 回答えた" in n for n in run.flow.summary())
    # 外れへの返事を読んで、同じ質問に戻った: 今度は正解
    _feed(st, "choices_done")
    _line(s, st, "main/s1:11", "ちがうよ")
    _feed(st, "choices", items=YES_NO)
    w = _weights(s, st)
    # 外れと分かった「はい」は後回し
    assert w["click choices/いいえ"] == click * k and w["click choices/はい"] == pytest.approx(click * low)
    assert (("選択肢で答える", "main/s1:10"), ("はい",)) in run.flow.wrong
    # 正解は、このランで一度答えて試し終えた枝でも選ぶ（タイトルから遊び直したなど）
    _choose(s, run, "いいえ", "b")
    assert (("選択肢で答える", "main/s1:10"), ("いいえ",)) in run.flow.leaves
    s.cfg["wrong_answer_prob"] = 0.0
    run.wrong_left.clear()  # タイトルに戻ったときと同じ
    run.flow.title_returned()
    _ask_yes_no(s, st)
    w = _weights(s, st)
    assert w["click choices/いいえ"] == click * k


def test_right_answer_is_learned_and_unexpected_progress_is_noted():
    # 答えたあと、同じ質問に戻らずに、ゲームが出した別の質問に着いたら、その答えを正解として覚える。
    # わざと答えた外れでそうなったら、「ハーネスの記録」に書く（プロファイルの正解が古いか、ゲームの判定の不具合）
    s, st, run = _answer_session({"wrong_answer_prob": 1.0})
    _ask_yes_no(s, st)
    _weights(s, st)
    _choose(s, run, "はい", "a")
    _feed(st, "choices_done")
    for n in range(FUZZ_DEFAULTS["flow_retry_lines"] + 1):
        _line(s, st, f"main/s2:{n}", "つづき")
    _feed(st, "choices", items=YES_NO)
    _weights(s, st)
    assert run.flow.ctx == ("選択肢で答える", "main/s2:6")
    assert (("選択肢で答える", "main/s1:10"), ("はい",)) in run.flow.right
    notes = run.flow.summary()
    assert any("外れのはずの答え（はい）のあと、同じ質問に戻らずに先へ進んだ" in n for n in notes)
    assert any("このランで新しく正解と分かったのは 1 通り" in n for n in notes)


def test_answer_is_not_judged_after_returning_to_title():
    # 答えのあとタイトルに戻った（バッドエンドなど）なら、当たり外れは分からないので覚えない
    s, st, run = _answer_session({"wrong_answer_prob": 0.0})
    _ask_yes_no(s, st)
    _weights(s, st)
    _choose(s, run, "いいえ", "b")
    run.flow.title_returned()
    _feed(st, "choices_done")
    for n in range(FUZZ_DEFAULTS["flow_retry_lines"] + 1):
        _line(s, st, f"main/s2:{n}", "つづき")
    _feed(st, "choices", items=YES_NO)
    _weights(s, st)
    assert not run.flow.right


def test_learned_answer_is_used_when_source_is_learn():
    # answer_source が learn なら、覚えた正解（flow_right）だけを使う
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    s, st, run = _answer_session({"wrong_answer_prob": 0.0, "answer_source": "learn"})
    run.flow.right.add((("選択肢で答える", "main/s9:1"), ("はい",)))
    _ask_yes_no(s, st, "main/s9:1")
    w = _weights(s, st)
    assert w["click choices/はい"] == click * k and w["click choices/いいえ"] == click


def test_profile_answer_by_visible_text_and_press_answer():
    # pick は、選ぶものの画面に見える文字で照らす。press は、選ぶ代わりに押すボタン（avoid に書いたものでも押す）
    k, low = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["flow_avoid_factor"]
    click = FUZZ_DEFAULTS["weights"]["click"]

    def ask(answers):
        s, st, run, _ = _session({"wrong_answer_prob": 0.0})
        doc = tomllib.loads(PROFILE)
        doc["flow"][0]["answers"] = answers
        run.flow = FlowTracker(parse_flows(doc["flow"]))
        _feed(st, "scene", name="main/s6")
        _line(s, st, "main/s6:70", "ねぇ、どこだって？")
        _only(st, "AlbumUI", LIST)
        st.set_read_text({f"{SLOT}#1": "No.002 屋上 の 昼", SLOT: "No.001 教室", f"{SLOT}#2": "No.003 公園"})
        return _weights(s, st)

    w = ask([{"at": "main/s6:7*", "pick": ["*屋上の*", "空"]}])
    assert w[f"click {SLOT}#1"] == click * k
    assert w[f"click {SLOT}"] == click and w[f"click {SLOT}#2"] == click
    w = ask([{"at": "main/s6:7*", "press": "AlbumUI/SelectButton"}])
    assert w["click AlbumUI/SelectButton"] == click * k
    w = ask([])
    assert w["click AlbumUI/SelectButton"] == pytest.approx(click * low)


def test_right_answers_are_remembered_across_runs(tmp_path):
    from yomawari.fuzz.experience import Experience

    path = tmp_path / "game.json"
    exp = Experience(path)
    tr = FlowTracker(_flows())
    tr.remember(exp.flow_wrong, exp.flow_right)
    tr.right.add((("選択肢で答える", "main/s1:10"), ("いいえ",)))
    exp.save()
    exp = Experience(path)
    assert exp.flow_right == {(("選択肢で答える", "main/s1:10"), ("いいえ",))}
    tr = FlowTracker(_flows())
    tr.remember(exp.flow_wrong, exp.flow_right)
    assert tr.known_right(("選択肢で答える", "main/s1:10")) == [("いいえ",)]
    assert any("前のランまでに正解と分かっていた組み合わせは 1 通り" in n for n in tr.summary())


def test_profile_answer_survives_ocr_noise_and_goes_before_filling_the_ledger():
    # 画面から読んだ文字の前後のごみ（「•」など）があっても正解に当て、正解が見えていれば台帳を埋め終えるのを待たずに
    # そこへ向かう（run 2026-11-07_1909: 「•No.002 …」が "No.002*" に当たらず、正解が見えているのに一覧を見て回った）
    from yomawari.fuzz.flow import Answer

    s, st, run, _ = _session({"wrong_answer_prob": 0.0})
    doc = tomllib.loads(ONCE_PROFILE)
    doc["flow"][0]["answers"] = [{"at": "main/chapter2_04:3*", "pick": ["No.002*", "空"]}]
    run.flow = FlowTracker(parse_flows(doc["flow"]))
    k, click = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["weights"]["click"]
    slots = [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i, 0.5]} for i in range(2)]
    slots += [{"name": "AlbumSlot (Clone)", "rect": [0.1 + 0.2 * i, -0.7, 0.25 + 0.2 * i, -0.5], "hidden": True}
              for i in range(2)]
    _line(s, st, "main/chapter2_04:323.0", "どこだっけ？")
    _feed(st, "menu", name="AlbumUI", modal=True, buttons=slots)
    st.set_read_text({SLOT: "No.001 教室", f"{SLOT}#1": "•No.002 屋上 の 昼"})
    w = _weights(s, st)
    assert run.ledger.unshown(next(iter(s._rows.values())))  # 画面の外の 2 つはまだ見ていない
    assert w[f"click {SLOT}#1"] == click * k and w[f"click {SLOT}"] == click
    # 前に選んだ思い出の文字が番号なしで読めた（読み落とし）: いまの手順の文字だけで照らす
    run.flow.path = [f"{SLOT}#1"]
    st.set_read_text({SLOT: "No.001 教室", f"{SLOT}#1": "屋上の昼"})
    run.ledger.items[f"{SLOT}#1"].text = "屋上の昼"
    assert s._answer_options({FRAG, f"{FRAG}#1"}, {FRAG: "海", f"{FRAG}#1": "空"}) == {f"{FRAG}#1"}
    # 前に選んだものが正解の思い出と読めれば、その思い出の正解だけ（ほかの思い出の正解の言葉は使わない）
    run.ctx_answers[run.flow.ctx] = (Answer(("x",), ("No.002*", "空")), Answer(("x",), ("No.009*", "海")))
    run.ledger.items[f"{SLOT}#1"].text = "No.002 屋上の昼"
    assert s._answer_options({FRAG, f"{FRAG}#1"}, {FRAG: "海", f"{FRAG}#1": "空"}) == {f"{FRAG}#1"}
