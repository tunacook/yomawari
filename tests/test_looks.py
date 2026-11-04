"""ボタンの見た目（fuzz/looks.py、sensors/screen.py）と、ほかの UI に覆われたボタン（covered）。"""

import asyncio
import random

from yomawari.config import FUZZ_DEFAULTS
from yomawari.events import Event
from yomawari.fuzz.decider import RandomDecider
from yomawari.fuzz.guard import Guard
from yomawari.fuzz.loop import FuzzRun, FuzzSession
from yomawari.fuzz.looks import brightest, dark, inactive, load_inactive_words, measure
from yomawari.fuzz.ops import normal_candidates
from yomawari.input.pointer import Pointer
from yomawari.sensors.screen import Shot, brightness
from yomawari.sensors.window import ScreenRect, WindowInfo
from yomawari.state import GameState, Target

from .test_window import FakeWindows

W = H = 100
CFG = {**FUZZ_DEFAULTS, "settle_sec": 0, "hover_wait_sec": 0}


def _shot(fills, bg=0):
    """fills: [(rect, 明るさ 0〜255)]。四角を塗った BGRA の画面。"""
    data = bytearray([bg, bg, bg, 255] * (W * H))
    for (x0, y0, x1, y1), v in fills:
        for y in range(int(y0 * H), int(y1 * H)):
            for x in range(int(x0 * W), int(x1 * W)):
                p = (y * W + x) * 4
                data[p:p + 3] = bytes([v, v, v])
    return Shot(W, H, W * 4, bytes(data))


def _slot(i, menu="MemoryListUI", name="Slot", w=0.15, nth=None):
    x = 0.05 + 0.2 * i
    return Target("button", menu, name, (x, 0.1, x + w, 0.3), nth=i if nth is None else nth)


def test_brightness_mean_and_peak():
    rect = (0.0, 0.0, 0.5, 0.5)
    mean, peak = brightness(_shot([(rect, 255)]), rect)
    assert abs(mean - 1) < 1e-6 and abs(peak - 1) < 1e-6
    # 文字だけ明るい（背景は暗い）ボタン: 平均は低いが、明るい部分は明るい
    mean, peak = brightness(_shot([((0.0, 0.0, 0.5, 0.1), 255)]), rect)
    assert mean < 0.3 and peak > 0.9
    assert brightness(_shot([]), (1.2, 1.2, 1.5, 1.5)) is None


def _dim(targets, shot, ratio=0.6, gap=0.1):
    out = set()
    for values in measure(targets, shot).values():
        out |= dark(values, brightest(values), ratio, gap)
    return out


def test_dark_slots_are_dim_among_same_name():
    slots = [_slot(i) for i in range(4)]
    # 回想一覧: サムネイルを明るさ 0.2 にしたロック中のスロット（Rachel と同じ）と、そのままのスロット
    shot = _shot([(slots[0].rect, 230), (slots[1].rect, 46), (slots[2].rect, 46), (slots[3].rect, 46)])
    assert list(measure(slots, shot)) == ["MemoryListUI/Slot"]
    assert _dim(slots, shot) == {s.label for s in slots[1:]}


def test_grey_text_buttons_are_dim_among_same_size():
    # タイトル画面: 名前の違う同じ大きさのボタン。「つづきから」だけ文字が灰色（背景は透けている）
    names = ["NewGame", "Continue", "Config", "Quit"]
    btns = [_slot(i, menu="TitleUI", name=n, nth=0) for i, n in enumerate(names)]
    text = lambda r: (r[0], r[1], r[2], r[1] + 0.05)
    shot = _shot([(text(b.rect), 90 if b.name == "Continue" else 240) for b in btns], bg=20)
    assert _dim(btns, shot) == {"TitleUI/Continue"}
    assert next(iter(measure(btns, shot))).startswith("TitleUI/（")


def test_even_buttons_and_small_groups_are_not_dim():
    slots = [_slot(i) for i in range(4)]
    shot = _shot([(s.rect, 200 - 10 * i) for i, s in enumerate(slots)])  # 少しずつ違うだけ
    assert _dim(slots, shot) == set()
    two = slots[:2]
    shot = _shot([(two[0].rect, 230), (two[1].rect, 30)])
    assert measure(two, shot) == {}  # 2つだけでは比べない


def test_inactive_words_from_rules():
    from .conftest import RULES_PATH

    words = load_inactive_words(RULES_PATH)
    ts = [Target("button", "SaveLoadUI", "Slot", (0, 0, 0.1, 0.1), "1. Empty", nth=0),
          Target("button", "SaveLoadUI", "Slot", (0, 0, 0.1, 0.1), "2. 2026/11/03 14:00", nth=1),
          Target("button", "SaveLoadUI", "Slot", (0, 0, 0.1, 0.1), "3. 空き", nth=2),
          Target("button", "SaveLoadUI", "Ret", (0, 0, 0.1, 0.1), "EmptyTrash")]
    assert inactive(ts, words) == {"SaveLoadUI/Slot", "SaveLoadUI/Slot#2"}


class _Mouse:
    def __init__(self):
        self.moves = []

    def move(self, x, y):
        self.moves.append((x, y))


def _show(st, slots, hidden=()):
    st.feed(Event("game", "menu", {"name": "MemoryListUI", "modal": True, "buttons": [
        {"name": "Slot", "rect": list(x.rect), "hidden": i in hidden} for i, x in enumerate(slots)]}, 0))


def _list_session(slots, shot):
    st = GameState()
    st.feed(Event("game", "resolution", {"w": 2000, "h": 1000}, 0))
    _show(st, slots)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1, shot=shot)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=CFG, response_timeout=0.1)
    mouse = _Mouse()
    events = []
    s = FuzzSession(run, st, Pointer(windows, mouse, 1, st, activate_wait=0), events.append, 0, lambda _: None)
    return s, st, run, windows, mouse, events


def test_dim_buttons_are_tried_later_and_noted():
    slots = [_slot(i) for i in range(4)]
    shot = _shot([(slots[2].rect, 230)] + [(s.rect, 46) for i, s in enumerate(slots) if i != 2])
    s, st, run, windows, mouse, events = _list_session(slots, shot)
    asyncio.run(s._look())
    assert s._dim == {"MemoryListUI/Slot", "MemoryListUI/Slot#1", "MemoryListUI/Slot#3"}
    # 撮る前に、カーソルをボタンのない場所（右上の隅）へ逃がした
    assert mouse.moves == [(995.0, 2.5)]
    ops = normal_candidates(st, run.guard, CFG["weights"], False, None, s._factors())
    w = {op.target.label: op.weight for op in ops if op.target}
    assert w["MemoryListUI/Slot#2"] == CFG["weights"]["click"]
    assert w["MemoryListUI/Slot#1"] == CFG["weights"]["click"] * CFG["dim_factor"]
    assert len(run.notes) == 1 and "3/4 個" in run.notes[0]
    looks = [e for e in events if e.kind == "looks"]
    assert looks and looks[0].data["groups"]["MemoryListUI/Slot"]["buttons"]["MemoryListUI/Slot#2"][2] is False
    asyncio.run(s._look())
    assert len(run.notes) == 1  # 同じ並びは1回だけ記録する
    windows.shot = None  # 画面収録の権限がないなど
    asyncio.run(s._look())
    assert s._dim == set()


def test_compare_within_one_shot_and_newly_bright_goes_first():
    slots = [_slot(i) for i in range(4)]
    s, st, run, windows, mouse, events = _list_session(slots, _shot([(slots[0].rect, 230)] +
                                                                    [(x.rect, 46) for x in slots[1:]]))
    asyncio.run(s._look())
    assert s._dim == {x.label for x in slots[1:]}
    # スクロールして明るい Slot が隠れ、見えている分がすべて同じ暗さになった。同じ画像の中で比べるので、暗いとはしない
    # （半透明の UI は後ろの絵で明るさが変わるので、前に撮った画像とは比べない）
    _show(st, slots, hidden={0})
    windows.shot = _shot([(x.rect, 46) for x in slots])
    asyncio.run(s._look())
    assert s._dim == set() and run.newly_bright == set()  # ほかに暗いものがないので「明るくなった」ともしない
    # 思い出を手に入れたあとに戻ると、暗かった #3 が明るい（ほかはまだ暗い）。先に押す
    _show(st, slots)
    windows.shot = _shot([(slots[0].rect, 230), (slots[3].rect, 230)] + [(x.rect, 46) for x in slots[1:3]])
    asyncio.run(s._look())
    assert run.newly_bright == {"MemoryListUI/Slot#3"}
    assert s._factors()["MemoryListUI/Slot#3"] == CFG["new_bright_factor"]
    assert sum("明るくなった" in n for n in run.notes) == 1
    # 押したら外し、また暗くなって明るくなっても、ランで2回目は先に押さない
    run.newly_bright.discard("MemoryListUI/Slot#3")
    windows.shot = _shot([(slots[0].rect, 230)] + [(x.rect, 46) for x in slots[1:]])
    asyncio.run(s._look())
    windows.shot = _shot([(slots[0].rect, 230), (slots[3].rect, 230)] + [(x.rect, 46) for x in slots[1:3]])
    asyncio.run(s._look())
    assert run.newly_bright == set() and sum("明るくなった" in n for n in run.notes) == 1


def test_covered_buttons_are_not_targets():
    st = GameState()
    st.feed(Event("game", "menu", {"name": "RClickMenuUI", "modal": True, "buttons": [
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "CloseButton", "rect": [0.1, 0.3, 0.2, 0.4]},
    ]}, 0))
    assert [t.name for t in st.targets()] == ["CloseButton"]
    before = st.signature()
    st.feed(Event("game", "menu", {"name": "RClickMenuUI", "modal": True, "buttons": [
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "CloseButton", "rect": [0.1, 0.3, 0.2, 0.4], "covered": True},
    ]}, 0))
    assert st.signature() != before and st.targets() == []
    # 全部覆われたモーダルは手前に出ていないので、その画面に向けた動かす・抜ける操作（Esc など）はしない。
    # 覆っている UI に押せるボタンもなければ、本文を読んでいるときと同じ操作になる
    assert st.top_modal() is None
    ops = normal_candidates(st, Guard({}), FUZZ_DEFAULTS["weights"], False)
    assert ops and all(op.target is None for op in ops) and not {op.group for op in ops} & {"move", "leave"}


def test_v07_dim_button_that_responds():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    ok = {"kind": "click", "target": "MemoryListUI/Slot#3", "dim": True, "responded": True, "screen_changed": True,
          "interfered": False, "heartbeat_age": 0.5}
    out = engine.feed(Event("harness", "op_result", ok, 0))
    assert [i.rule_id for i in out] == ["V-07"] and "Slot#3" in out[0].detail
    assert engine.feed(Event("harness", "op_result", ok, 0)) == []  # 同じボタンは1回だけ
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/B", "responded": False, "screen_changed": False}, 0)) == []
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/C", "dim": False}, 0)) == []


def test_v07_needs_another_screen_not_feedback():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    base = {"kind": "click", "target": "MemoryListUI/Slot#5", "dim": True, "responded": True,
            "interfered": False, "heartbeat_age": 0.5}
    # 押せないことを知らせる演出（通知が出た）で画面の状態は変わったが、別の画面は出ていない
    assert engine.feed(Event("harness", "op_result", {**base, "screen_changed": False}, 0)) == []


def test_v07_skips_saving_into_empty_slot():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    # 空きのセーブスロットはサムネイルがなく暗く写る。押してセーブが始まり、操作を止める幕（ボタンのないモーダル）が出た
    d = {"kind": "click", "target": "SaveLoadUI/SaveLoadSlot (Clone)#1", "dim": True, "responded": True,
         "screen_changed": True, "saved": True, "interfered": False, "heartbeat_age": 0.5}
    assert engine.feed(Event("harness", "op_result", d, 0)) == []
    st = GameState()
    st.feed(Event("game", "save", {"phase": "start"}, 0))
    st.feed(Event("game", "save", {"phase": "end"}, 0))
    assert st.saves == 1


def test_screens_ignore_toasts_and_line():
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("menu", name="MemoryListUI", modal=True, buttons=[{"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2]}])
    before = st.screens()
    feed("menu", name="MemoryFragmentNotifyUI", modal=False, buttons=[])  # ボタンのない通知
    feed("line", line_id="s1:2")
    assert st.screens() == before
    feed("menu", name="MemorySummaryUI", modal=True, buttons=[{"name": "Play", "rect": [0.4, 0.4, 0.6, 0.5]}])
    assert st.screens() != before


def test_op_result_tells_screen_changed():
    from .test_fuzz import run_sim

    _, _, _, events, _ = run_sim("random", 1.5)
    res = [e.data for e in events if e.kind == "op_result" and e.data.get("target")]
    # タイトルのボタン（設定・ニューゲーム）と設定の戻るボタンは、どれも画面を切り替える
    assert [d for d in res if d["responded"]] and all(d["screen_changed"] for d in res if d["responded"])
    assert all(not d["screen_changed"] for d in res if not d["responded"])


def test_s05_skips_quiet_buttons():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    base = {"kind": "click", "target": "SaveLoadUI/Slot#7", "type": "button", "interfered": False,
            "heartbeat_age": 0.5}
    engine.feed(Event("harness", "op_result", {**base, "responded": True}, 0))  # セーブ画面: 空きにセーブできた
    out = []
    for _ in range(4):  # ロード画面: 「8. Empty」は押しても何も起きない
        out += engine.feed(Event("harness", "op_result", {**base, "responded": False, "quiet": True}, 0))
    assert out == []


def test_quiet_buttons_wait_shorter():
    from yomawari.fuzz.ops import Op

    cfg = {**CFG, "quiet_wait_sec": 0.05}
    st = GameState()
    st.feed(Event("game", "resolution", {"w": 2000, "h": 1000}, 0))
    st.feed(Event("game", "menu", {"name": "SaveLoadUI", "modal": True, "buttons": [
        {"name": "Slot", "rect": [0.1, 0.1 + 0.2 * i, 0.3, 0.25 + 0.2 * i]} for i in range(3)]}, 0))
    # 画面から読んだ文字（OCR）で空と分かる
    st.set_read_text({"SaveLoadUI/Slot": "1. Empty", "SaveLoadUI/Slot#1": "2. Empty", "SaveLoadUI/Slot#2": "3. Empty"})
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=5.0, inactive_words=["Empty"])
    events = []
    s = FuzzSession(run, st, Pointer(windows, _Mouse(), 1, st, activate_wait=0), events.append, 0, lambda _: None)
    s.pointer.driver.click = lambda *a, **k: None
    t = st.targets()[0]
    asyncio.run(s._normal(Op("click", t), "modal:SaveLoadUI", False))
    res = [e.data for e in events if e.kind == "op_result"][0]
    assert res["quiet"] and not res["responded"] and res["waited"] < 1.0


def test_screen_that_changed_look_in_place_is_behind():
    # run 2203: 回想を読み終えると、回想一覧（モーダル）の上に要約（モーダルと知らされない）が出た。一覧のスロットは
    # 位置が同じまま、暗幕で暗くなったり要約のパネルが重なったりした。見た目から一覧が後ろに下がったと判定し、要約を操作する
    import random

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.events import Event
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun, FuzzSession
    from yomawari.input.pointer import Pointer
    from yomawari.sensors.window import ScreenRect, WindowInfo
    from yomawari.state import GameState

    from .test_window import FakeWindows

    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))  # noqa: E731
    slots = [{"name": "Slot", "rect": [0.1 * i, 0.3, 0.1 * i + 0.08, 0.4]} for i in range(6)]
    feed("menu", name="MemoryListUI", modal=True, buttons=slots)
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    labels = ["MemoryListUI/Slot"] + [f"MemoryListUI/Slot#{i}" for i in range(1, 6)]
    rects = {t.label: t.rect for t in st.menus["MemoryListUI"].buttons}
    front = [(0.056, 0.128), (0.067, 0.176), (0.107, 0.186), (0.134, 0.214), (0.113, 0.186), (0.242, 0.584)]
    later = [(0.02, 0.084), (0.036, 0.137), (0.111, 0.533), (0.321, 0.768), (0.173, 0.658), (0.084, 0.208)]
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, front))}, rects)
    assert st.behind == set()
    # 手前に出てきたものがなければ、見た目が変わっても（背景が動いただけかもしれない）後ろに下がったとはしない
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, later))}, rects)
    assert st.behind == set()
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, front))}, rects)
    # 要約が出た。一覧のボタンは同じ位置のまま、見た目がまとめて変わった
    feed("menu", name="MemorySummaryUI", modal=False, buttons=[
        {"name": "StartToMemoryScenario", "rect": [0.42, 0.76, 0.61, 0.84]},
        {"name": "CloseMemorySummary", "rect": [0.18, 0.77, 0.24, 0.82]},
    ])
    assert st.top_modal().name == "MemoryListUI"  # ゲームが知らせる印だけでは、一覧が手前のまま
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, later))}, rects)
    assert st.behind == {"MemoryListUI"}
    assert st.top_modal() is None
    assert {t.label for t in st.targets()} == {"MemorySummaryUI/StartToMemoryScenario", "MemorySummaryUI/CloseMemorySummary"}
    assert any("`MemoryListUI` のボタンが、位置は変わらないのに見た目がまとめて変わった" in n for n in run.notes)
    # 要約を閉じて、見た目が元に戻れば、一覧が手前に戻る
    feed("menu_close", name="MemorySummaryUI")
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, front))}, rects)
    assert st.behind == set() and st.top_modal().name == "MemoryListUI"
    # スクロールして位置が変わったボタンは比べない（見た目が変わっても、後ろに下がったとはしない）
    moved = {k: (r[0], r[1] + 0.1, r[2], r[3] + 0.1) for k, r in rects.items()}
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, later))}, moved)
    assert st.behind == set()


def test_behind_tolerates_small_slide():
    # run 2327: 回想に入る前に一覧を測ったとき、一覧はまだ少し動いていた（高さの 7%）。同じ位置として比べ、後ろに下がったと判定する
    import random

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.events import Event
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun, FuzzSession
    from yomawari.input.pointer import Pointer
    from yomawari.sensors.window import ScreenRect, WindowInfo
    from yomawari.state import GameState

    from .test_window import FakeWindows

    st = GameState()
    slots = [{"name": "Slot", "rect": [0.208 + 0.25 * i, 0.259, 0.443 + 0.25 * i, 0.49]} for i in range(4)]
    st.feed(Event("game", "menu", {"name": "MemoryListUI", "modal": True, "buttons": slots}, 0))
    st.feed(Event("game", "menu", {"name": "MemorySummaryUI", "modal": False,
                                   "buttons": [{"name": "Start", "rect": [0.42, 0.76, 0.61, 0.84]}]}, 0))
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    labels = ["MemoryListUI/Slot"] + [f"MemoryListUI/Slot#{i}" for i in range(1, 4)]
    now = {t.label: t.rect for t in st.menus["MemoryListUI"].buttons}
    earlier = {k: (r[0], r[1] + 0.017, r[2], r[3] + 0.017) for k, r in now.items()}
    run.look_base = {k: (earlier[k], v) for k, v in zip(labels, [(0.134, 0.232), (0.13, 0.21), (0.095, 0.169), (0.076, 0.181)])}
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, [(0.261, 0.683), (0.199, 0.752), (0.053, 0.102), (0.024, 0.124)]))}, now)
    assert st.behind == {"MemoryListUI"}
    # 大きく動いた（スクロールした）ものは比べない
    st.behind = set()
    far = {k: (r[0], r[1] + 0.1, r[2], r[3] + 0.1) for k, r in now.items()}
    run.look_base = {k: (far[k], v) for k, v in zip(labels, [(0.134, 0.232), (0.13, 0.21), (0.095, 0.169), (0.076, 0.181)])}
    s._judge_behind({"MemoryListUI/Slot": dict(zip(labels, [(0.261, 0.683), (0.199, 0.752), (0.053, 0.102), (0.024, 0.124)]))}, now)
    assert st.behind == set()


def test_repressing_a_never_answered_button_waits_short():
    # 押しても一度も反応しなかったボタンを押し直すときは、quiet_wait_sec だけ待つ（S-05 の対象でないので）。
    # 一度でも反応したことのあるボタンは、反応しなくなったかを確かめるので、今までどおり待つ
    slots = [_slot(i) for i in range(3)]
    s, st, run, *_ = _list_session(slots, _shot([(x.rect, 120) for x in slots]))
    run.silent.add("MemoryListUI/Slot#1")
    assert "MemoryListUI/Slot#1" in s._quiet()
    ops = normal_candidates(st, run.guard, CFG["weights"], False, None, s._factors())
    w = {op.target.label: op.weight for op in ops if op.target}
    assert w["MemoryListUI/Slot#1"] == CFG["weights"]["click"] * CFG["dim_factor"]
    run.silent.add("MemoryListUI/Slot#2")
    run.answered.add("MemoryListUI/Slot#2")
    assert "MemoryListUI/Slot#2" not in s._quiet()


def test_assign_read_text_to_buttons():
    from yomawari.fuzz.reading import assign

    slots = [_slot(i) for i in range(3)]
    outer = Target("button", "MemoryListUI", "Panel", (0.0, 0.0, 1.0, 1.0))
    r = slots[1].rect
    boxes = [
        ("夕方の", 0.9, (r[0] + 0.01, r[1] + 0.01, r[0] + 0.05, r[1] + 0.03)),
        ("帰り道", 0.9, (r[0] + 0.06, r[1] + 0.01, r[0] + 0.1, r[1] + 0.03)),
        ("ノイズ", 0.1, (r[0] + 0.01, r[1] + 0.05, r[0] + 0.05, r[1] + 0.07)),  # 確からしさが低い
        ("見出し", 0.9, (0.9, 0.95, 0.95, 0.98)),  # どのスロットにも入らない
    ]
    texts = assign(boxes, slots + [outer], 0.3)
    # 重なっているボタンでは小さい方（並びのスロット）に割り当てる。同じ行は左から
    assert texts == {"MemoryListUI/Slot#1": "夕方の 帰り道", "MemoryListUI/Panel": "見出し"}


def test_look_reads_button_text_from_screen_until_look_changes():
    slots = [_slot(i) for i in range(3)]
    shot = _shot([(x.rect, 120) for x in slots])
    s, st, run, windows, mouse, events = _list_session(slots, shot)
    r = slots[0].rect
    windows.texts = [("屋上の猫", 0.9, (r[0] + 0.01, r[1] + 0.01, r[2] - 0.01, r[1] + 0.05))]
    asyncio.run(s._look())
    assert st.menus["MemoryListUI"].buttons[0].text == "屋上の猫"
    assert [e.data["texts"] for e in events if e.kind == "read_text"] == [{"MemoryListUI/Slot": "屋上の猫"}]
    assert any("屋上の猫" in n for n in run.notes)
    # 出し直された menu にも引き継ぎ、見た目が変わらなければ読み直さない
    _show(st, slots)
    assert st.menus["MemoryListUI"].buttons[0].text == "屋上の猫"
    asyncio.run(s._look())
    assert len([e for e in events if e.kind == "read_text"]) == 1
    # 見た目が変わった（開示された）ボタンは読み直す
    windows.shot = _shot([(slots[0].rect, 250)] + [(x.rect, 120) for x in slots[1:]])
    windows.texts = [("夕方の帰り道", 0.9, (r[0] + 0.01, r[1] + 0.01, r[2] - 0.01, r[1] + 0.05))]
    asyncio.run(s._look())
    assert st.menus["MemoryListUI"].buttons[0].text == "夕方の帰り道"
