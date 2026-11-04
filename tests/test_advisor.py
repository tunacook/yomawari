"""本文の文脈から、どれを先に試すかを AI に聞く（fuzz/advisor.py）。"""

import time
import tomllib

import pytest

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.advisor import ChoiceAdvisor, build_prompt
from yomawari.fuzz.flow import FlowTracker, parse_flows
from yomawari.judge.ollama import OllamaError

from .test_flow import FRAGS, LIST, SUMMARY, TRIAL, _feed, _only, _press, _session, _weights


def _wait(advisor, *args):
    for _ in range(200):
        out = advisor.scores(*args)
        if out is not None:
            return out
        time.sleep(0.01)
    raise AssertionError("答えが出ない")


def test_scores_are_asked_once_in_background_and_mapped_to_options():
    prompts = []

    def ask(prompt, schema):
        prompts.append(prompt)
        return {"scores": [{"id": 0, "reason": "-", "score": 2}, {"id": 1, "reason": "屋上の話", "score": 9},
                           {"id": 7, "reason": "-", "score": 10}]}  # 候補にない id は使わない

    adv = ChoiceAdvisor(ask, "m")
    opts = {"a": "図書室の窓", "b": "夕焼けの屋上"}
    assert adv.scores("q", ["ねぇ、どこだって？"], [], opts) is None  # 裏で聞き始める（操作は止めない）
    assert _wait(adv, "q", ["ねぇ、どこだって？"], [], opts) == {"a": 2.0, "b": 9.0}
    assert len(prompts) == 1  # 同じ質問・同じ候補は聞き直さない
    assert "ねぇ、どこだって？" in prompts[0] and "id 1: 夕焼けの屋上" in prompts[0]
    assert "「ねぇ、どこだって？」に「夕焼けの屋上」（9 点）" in adv.summary()[0]


def test_failed_answer_means_no_scores():
    def ask(prompt, schema):
        raise OllamaError("ConnectError: つながらない")

    adv = ChoiceAdvisor(ask, "m")
    assert _wait(adv, "q", [], [], {"a": "x", "b": "y"}) == {}
    assert "失敗 1 回" in adv.summary()[0] and "つながらない" in adv.summary()[0]


def test_prompt_includes_what_was_read():
    p = build_prompt(["どこ？"], ["夕焼けの屋上で猫と話した"], ["屋上", "駅"])
    assert "答えるまでに読んだ本文" in p and "夕焼けの屋上で猫と話した" in p


def test_flow_tries_the_option_ai_favors_first():
    # 言葉の一覧で、文字（画面から読んだもの）と本文の文脈から AI がいちばん合うと答えた言葉を先に試す
    def ask(prompt, schema):
        assert "ねぇ、どこだって？" in prompt
        return {"scores": [{"id": 0, "reason": "-", "score": 1}, {"id": 1, "reason": "屋上", "score": 8}]}

    for enabled in (True, False):
        s, st, run, events = _session({"ai_choice": enabled})
        run.flow = FlowTracker(parse_flows(tomllib.loads(TRIAL)["flow"]))
        run.advisor = ChoiceAdvisor(ask, "m")
        k, ai = FUZZ_DEFAULTS["flow_factor"], FUZZ_DEFAULTS["ai_choice_factor"]
        click = FUZZ_DEFAULTS["weights"]["click"]
        frag = "MemoryFragmentListUI/MemoryFragmentListSlot(Clone)"
        _feed(st, "scene", name="main/s6")
        s.on_event(Event("game", "line", {"line_id": "main/s6:70", "text": "ねぇ、<br>どこだって？"}, 0))
        st.feed(Event("game", "line", {"line_id": "main/s6:70", "text": "ねぇ、どこだって？"}, 0))
        _only(st, "MemoryListUI", LIST)
        _press(s, st, run, "MemoryListUI/MemoryListGridSlot (Clone)")
        _only(st, "MemorySummaryUI", SUMMARY)
        run.flow.done_once.add((run.flow.active.name, 1, tuple(run.flow.path)))  # 読んだことにする
        _press(s, st, run, "MemorySummaryUI/ShowFragmentListButton")
        _only(st, "MemoryFragmentListUI", FRAGS)
        st.set_read_text({frag: "駅前の公園", frag + "#1": "夕焼けの屋上"})
        w = _weights(s, st)
        if enabled:
            for _ in range(200):
                w = _weights(s, st)
                if w[f"click {frag}#1"] != click * k:
                    break
                time.sleep(0.01)
            assert w[f"click {frag}#1"] == pytest.approx(click * k * ai) and w[f"click {frag}"] == click * k
            advice = [e.data for e in events if e.kind == "advice"]
            assert advice and advice[0]["favored"] == ["夕焼けの屋上"]
        else:
            assert w[f"click {frag}#1"] == w[f"click {frag}"] == click * k
            assert run.advisor.asked == 0
        run.advisor.close()
