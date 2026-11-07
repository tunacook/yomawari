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


def _slot(i, menu="AlbumUI", name="Slot", w=0.15, nth=None):
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
    assert list(measure(slots, shot)) == ["AlbumUI/Slot"]
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
    st.feed(Event("game", "menu", {"name": "AlbumUI", "modal": True, "buttons": [
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
    assert s._dim == {"AlbumUI/Slot", "AlbumUI/Slot#1", "AlbumUI/Slot#3"}
    assert s._disabled == set()  # 暗いだけなので後回しにするが、V-07 の対象にはしない（暗い＝押せないかはデザイン次第）
    # 撮る前に、カーソルをボタンのない場所（右上の隅）へ逃がした
    assert mouse.moves == [(995.0, 2.5)]
    ops = normal_candidates(st, run.guard, CFG["weights"], False, None, s._factors())
    w = {op.target.label: op.weight for op in ops if op.target}
    assert w["AlbumUI/Slot#2"] == CFG["weights"]["click"]
    assert w["AlbumUI/Slot#1"] == CFG["weights"]["click"] * CFG["dim_factor"]
    assert len(run.notes) == 1 and "3/4 個" in run.notes[0]
    looks = [e for e in events if e.kind == "looks"]
    assert looks and looks[0].data["groups"]["AlbumUI/Slot"]["buttons"]["AlbumUI/Slot#2"][2] is False
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
    assert run.newly_bright == {"AlbumUI/Slot#3"}
    # 一覧の目標にして先に押す（DESIGN.md §9「一覧の台帳と目標」）
    goal = [op for op in s._list_ops([]) if op.goal is not None]
    assert [op.label for op in goal] == ["click AlbumUI/Slot#3"] and goal[0].by == "list"
    assert goal[0].weight == CFG["weights"]["click"] * CFG["list_factor"]
    assert sum("明るくなった" in n for n in run.notes) == 1
    # 押したら外し、また暗くなって明るくなっても、ランで2回目は先に押さない
    run.newly_bright.discard("AlbumUI/Slot#3")
    windows.shot = _shot([(slots[0].rect, 230)] + [(x.rect, 46) for x in slots[1:]])
    asyncio.run(s._look())
    windows.shot = _shot([(slots[0].rect, 230), (slots[3].rect, 230)] + [(x.rect, 46) for x in slots[1:3]])
    asyncio.run(s._look())
    assert run.newly_bright == set() and sum("明るくなった" in n for n in run.notes) == 1


def test_covered_buttons_are_not_targets():
    st = GameState()
    st.feed(Event("game", "menu", {"name": "QuickMenuUI", "modal": True, "buttons": [
        {"name": "ToSave", "rect": [0.1, 0.1, 0.2, 0.2], "covered": True},
        {"name": "CloseButton", "rect": [0.1, 0.3, 0.2, 0.4]},
    ]}, 0))
    assert [t.name for t in st.targets()] == ["CloseButton"]
    before = st.signature()
    st.feed(Event("game", "menu", {"name": "QuickMenuUI", "modal": True, "buttons": [
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
    ok = {"kind": "click", "target": "AlbumUI/Slot#3", "dim": True, "disabled_look": True, "responded": True, "screen_changed": True,
          "interfered": False, "heartbeat_age": 0.5}
    out = engine.feed(Event("harness", "op_result", ok, 0))
    assert [i.rule_id for i in out] == ["V-07"] and "Slot#3" in out[0].detail
    assert engine.feed(Event("harness", "op_result", ok, 0)) == []  # 同じボタンは1回だけ
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/B", "responded": False, "screen_changed": False}, 0)) == []
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/C", "dim": False, "disabled_look": False}, 0)) == []
    # run 2026-11-05_2230: ほかより暗いだけ（絵柄が暗い言葉・セーブスロット）では出さない（暗い＝押せないかはデザイン次第）
    assert engine.feed(Event("harness", "op_result", {**ok, "target": "A/D", "disabled_look": False}, 0)) == []


def test_v07_needs_another_screen_not_feedback():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    base = {"kind": "click", "target": "AlbumUI/Slot#5", "dim": True, "disabled_look": True, "responded": True,
            "interfered": False, "heartbeat_age": 0.5}
    # 押せないことを知らせる演出（通知が出た）で画面の状態は変わったが、別の画面は出ていない
    assert engine.feed(Event("harness", "op_result", {**base, "screen_changed": False}, 0)) == []


def test_v07_skips_saving_into_empty_slot():
    from yomawari.detect.engine import Engine

    from .conftest import scaled_rules

    engine = Engine(scaled_rules(1.0))
    # 空きのセーブスロットはサムネイルがなく暗く写る。押してセーブが始まり、操作を止める幕（ボタンのないモーダル）が出た
    d = {"kind": "click", "target": "SaveLoadUI/SaveLoadSlot (Clone)#1", "dim": True, "disabled_look": True, "responded": True,
         "screen_changed": True, "saved": True, "interfered": False, "heartbeat_age": 0.5}
    assert engine.feed(Event("harness", "op_result", d, 0)) == []
    st = GameState()
    st.feed(Event("game", "save", {"phase": "start"}, 0))
    st.feed(Event("game", "save", {"phase": "end"}, 0))
    assert st.saves == 1


def test_screens_ignore_toasts_and_line():
    st = GameState()
    feed = lambda kind, **d: st.feed(Event("game", kind, d, 0))
    feed("menu", name="AlbumUI", modal=True, buttons=[{"name": "Slot", "rect": [0.1, 0.1, 0.2, 0.2]}])
    before = st.screens()
    feed("menu", name="WordNotifyUI", modal=False, buttons=[])  # ボタンのない通知
    feed("line", line_id="s1:2")
    assert st.screens() == before
    feed("menu", name="EpisodeUI", modal=True, buttons=[{"name": "Play", "rect": [0.4, 0.4, 0.6, 0.5]}])
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
    feed("menu", name="AlbumUI", modal=True, buttons=slots)
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    labels = ["AlbumUI/Slot"] + [f"AlbumUI/Slot#{i}" for i in range(1, 6)]
    rects = {t.label: t.rect for t in st.menus["AlbumUI"].buttons}
    front = [(0.056, 0.128), (0.067, 0.176), (0.107, 0.186), (0.134, 0.214), (0.113, 0.186), (0.242, 0.584)]
    later = [(0.02, 0.084), (0.036, 0.137), (0.111, 0.533), (0.321, 0.768), (0.173, 0.658), (0.084, 0.208)]
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, front))}, rects)
    assert st.behind == set()
    # 手前に出てきたものがなければ、見た目が変わっても（背景が動いただけかもしれない）後ろに下がったとはしない
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, later))}, rects)
    assert st.behind == set()
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, front))}, rects)
    # 要約が出た。一覧のボタンは同じ位置のまま、見た目がまとめて変わった
    feed("menu", name="EpisodeUI", modal=False, buttons=[
        {"name": "ReadEpisodeButton", "rect": [0.42, 0.76, 0.61, 0.84]},
        {"name": "CloseEpisodeButton", "rect": [0.18, 0.77, 0.24, 0.82]},
    ])
    assert st.top_modal().name == "AlbumUI"  # ゲームが知らせる印だけでは、一覧が手前のまま
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, later))}, rects)
    assert st.behind == {"AlbumUI"}
    assert st.top_modal() is None
    assert {t.label for t in st.targets()} == {"EpisodeUI/ReadEpisodeButton", "EpisodeUI/CloseEpisodeButton"}
    assert any("`AlbumUI` のボタンが、位置は変わらないのに見た目がまとめて変わった" in n for n in run.notes)
    # 要約を閉じて、見た目が元に戻れば、一覧が手前に戻る
    feed("menu_close", name="EpisodeUI")
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, front))}, rects)
    assert st.behind == set() and st.top_modal().name == "AlbumUI"
    # スクロールして位置が変わったボタンは比べない（見た目が変わっても、後ろに下がったとはしない）
    moved = {k: (r[0], r[1] + 0.1, r[2], r[3] + 0.1) for k, r in rects.items()}
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, later))}, moved)
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
    st.feed(Event("game", "menu", {"name": "AlbumUI", "modal": True, "buttons": slots}, 0))
    st.feed(Event("game", "menu", {"name": "EpisodeUI", "modal": False,
                                   "buttons": [{"name": "Start", "rect": [0.42, 0.76, 0.61, 0.84]}]}, 0))
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    labels = ["AlbumUI/Slot"] + [f"AlbumUI/Slot#{i}" for i in range(1, 4)]
    now = {t.label: t.rect for t in st.menus["AlbumUI"].buttons}
    earlier = {k: (r[0], r[1] + 0.017, r[2], r[3] + 0.017) for k, r in now.items()}
    run.look_base = {k: (earlier[k], v) for k, v in zip(labels, [(0.134, 0.232), (0.13, 0.21), (0.095, 0.169), (0.076, 0.181)])}
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, [(0.261, 0.683), (0.199, 0.752), (0.053, 0.102), (0.024, 0.124)]))}, now)
    assert st.behind == {"AlbumUI"}
    # 大きく動いた（スクロールした）ものは比べない
    st.behind = set()
    far = {k: (r[0], r[1] + 0.1, r[2], r[3] + 0.1) for k, r in now.items()}
    run.look_base = {k: (far[k], v) for k, v in zip(labels, [(0.134, 0.232), (0.13, 0.21), (0.095, 0.169), (0.076, 0.181)])}
    s._judge_behind({"AlbumUI/Slot": dict(zip(labels, [(0.261, 0.683), (0.199, 0.752), (0.053, 0.102), (0.024, 0.124)]))}, now)
    assert st.behind == set()


def test_behind_judgment_is_undone_when_front_does_not_respond():
    # run 2026-11-06_0937: 右クリックメニューがスライドして出てくる途中に測り、見た目が変わったので後ろに下がったと誤った。
    # メニューの下に隠れた選択肢を 30 分押し続けた（押しても画面が変わらないので、測り直すきっかけもなかった）。
    # 手前のものを続けて押しても反応がなければ、判定を取り消し、閉じるまで後ろに下がったとはしない
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
    items = [{"name": f"To{i}", "rect": [0.8, 0.15 + 0.08 * i, 0.9, 0.19 + 0.08 * i]} for i in range(4)]
    feed("menu", name="QuickMenuUI", modal=True, buttons=items)
    cfg = {**FUZZ_DEFAULTS, "hover_wait_sec": 0}
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=cfg, response_timeout=0.1)
    windows = FakeWindows(WindowInfo(1, ScreenRect(0, 0, 1000, 500), True), front=1)
    s = FuzzSession(run, st, Pointer(windows, None, 1, st, activate_wait=0), lambda ev: None, 0, lambda _: None)
    labels = [f"QuickMenuUI/To{i}" for i in range(4)]
    rects = {t.label: t.rect for t in st.menus["QuickMenuUI"].buttons}
    opening = [(0.05, 0.1)] * 4
    shown = [(0.4, 0.3)] * 4
    s._judge_behind({"QuickMenuUI/To": dict(zip(labels, opening))}, rects)
    feed("choices", items=[{"id": "a", "text": "話す", "rect": [0.03, 0.3, 0.97, 0.44]},
                           {"id": "b", "text": "話さない", "rect": [0.03, 0.55, 0.97, 0.69]}])
    s._judge_behind({"QuickMenuUI/To": dict(zip(labels, shown))}, rects)
    assert st.behind == {"QuickMenuUI"}  # 開いている途中に測ったものと比べて、見た目が変わった
    choice = st.choices[0]
    s._verify_behind(choice, False)
    assert st.behind == {"QuickMenuUI"}
    s._verify_behind(choice, False)
    assert st.behind == set() and st.top_modal().name == "QuickMenuUI"
    assert any("判定を取り消した" in n for n in run.notes)
    # 閉じるまでは、見た目が変わっても後ろに下がったとはしない
    s._judge_behind({"QuickMenuUI/To": dict(zip(labels, opening))}, rects)
    assert st.behind == set()
    # 閉じて開き直したら、また見た目で判定する
    feed("menu_close", name="QuickMenuUI")
    s._judge_behind({}, {})
    assert run.behind_refuted == set()


def test_repressing_a_never_answered_button_waits_short():
    # 押しても一度も反応しなかったボタンを押し直すときは、quiet_wait_sec だけ待つ（S-05 の対象でないので）。
    # 一度でも反応したことのあるボタンは、反応しなくなったかを確かめるので、今までどおり待つ
    slots = [_slot(i) for i in range(3)]
    s, st, run, *_ = _list_session(slots, _shot([(x.rect, 120) for x in slots]))
    run.silent.add("AlbumUI/Slot#1")
    assert "AlbumUI/Slot#1" in s._quiet()
    ops = normal_candidates(st, run.guard, CFG["weights"], False, None, s._factors())
    w = {op.target.label: op.weight for op in ops if op.target}
    assert w["AlbumUI/Slot#1"] == CFG["weights"]["click"] * CFG["dim_factor"]
    run.silent.add("AlbumUI/Slot#2")
    run.answered.add("AlbumUI/Slot#2")
    assert "AlbumUI/Slot#2" not in s._quiet()


def test_assign_read_text_to_buttons():
    from yomawari.fuzz.reading import assign

    slots = [_slot(i) for i in range(3)]
    outer = Target("button", "AlbumUI", "Panel", (0.0, 0.0, 1.0, 1.0))
    r = slots[1].rect
    boxes = [
        ("夕方の", 0.9, (r[0] + 0.01, r[1] + 0.01, r[0] + 0.05, r[1] + 0.03)),
        ("帰り道", 0.9, (r[0] + 0.06, r[1] + 0.01, r[0] + 0.1, r[1] + 0.03)),
        ("ノイズ", 0.1, (r[0] + 0.01, r[1] + 0.05, r[0] + 0.05, r[1] + 0.07)),  # 確からしさが低い
        ("見出し", 0.9, (0.9, 0.95, 0.95, 0.98)),  # どのスロットにも入らない
    ]
    texts = assign(boxes, slots + [outer], 0.3)
    # 重なっているボタンでは小さい方（並びのスロット）に割り当てる。同じ行は左から
    assert texts == {"AlbumUI/Slot#1": "夕方の 帰り道", "AlbumUI/Panel": "見出し"}


def test_look_reads_button_text_from_screen_until_look_changes():
    slots = [_slot(i) for i in range(3)]
    shot = _shot([(x.rect, 120) for x in slots])
    s, st, run, windows, mouse, events = _list_session(slots, shot)
    r = slots[0].rect
    windows.texts = [("屋上の猫", 0.9, (r[0] + 0.01, r[1] + 0.01, r[2] - 0.01, r[1] + 0.05))]
    asyncio.run(s._look())
    assert st.menus["AlbumUI"].buttons[0].text == "屋上の猫"
    assert [e.data["texts"] for e in events if e.kind == "read_text"] == [{"AlbumUI/Slot": "屋上の猫"}]
    assert any("屋上の猫" in n for n in run.notes)
    # 出し直された menu にも引き継ぎ、見た目が変わらなければ読み直さない
    _show(st, slots)
    assert st.menus["AlbumUI"].buttons[0].text == "屋上の猫"
    asyncio.run(s._look())
    assert len([e for e in events if e.kind == "read_text"]) == 1
    # 見た目が変わった（開示された）ボタンは読み直す
    windows.shot = _shot([(slots[0].rect, 250)] + [(x.rect, 120) for x in slots[1:]])
    windows.texts = [("夕方の帰り道", 0.9, (r[0] + 0.01, r[1] + 0.01, r[2] - 0.01, r[1] + 0.05))]
    asyncio.run(s._look())
    assert st.menus["AlbumUI"].buttons[0].text == "夕方の帰り道"


def test_masked_text_decides_lock_over_brightness():
    # run 2026-11-05_0921: 回想一覧で、サムネイルの絵が暗いだけの開放済みの思い出（下に題名がある）を、暗いので押せないと
    # 見て V-07 にしていた。並びに「？？？」（未開放）があれば、明るさではなく文字で押せるかを決める
    import random

    from yomawari.config import FUZZ_DEFAULTS
    from yomawari.fuzz.decider import RandomDecider
    from yomawari.fuzz.guard import Guard
    from yomawari.fuzz.loop import FuzzRun, FuzzSession
    from yomawari.fuzz.looks import load_masked_chars, masked, text_inactive

    from .conftest import RULES_PATH

    chars = load_masked_chars(RULES_PATH)
    assert masked("？？？", chars) and masked("? ? ?", chars) and not masked("夕焼けの屋上？", chars) and not masked("", chars)
    # run 2026-11-05_1811: Rachel の回想一覧は番号付きで読める。OCR が「？」の一部を読み違えることもある
    assert masked("No.021 ？？？", chars) and masked("INo.015 ？？", chars) and masked("No.014 2？2", chars)
    assert not masked("No.030 青い傘", chars) and not masked("0ゆまどうぶつえん ？！ ？", chars)
    assert text_inactive("？？？", [], chars) and not text_inactive("駅前の公園", ["空き"], chars)

    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=FUZZ_DEFAULTS, response_timeout=0.1, masked_chars=chars)
    s = FuzzSession(run, GameState(), None, lambda ev: None, 0, lambda _: None)
    values = {f"M/Slot#{i}": (0.5, 0.5) for i in range(4)}
    dim = {"M/Slot#1", "M/Slot#2", "M/Slot#3"}  # 明るさでは #1〜#3 が暗い
    texts = {"M/Slot#0": "駅前の公園", "M/Slot#1": "夕焼けの屋上", "M/Slot#2": "？？？"}  # #3 は文字を読めていない
    assert s._by_text("M/Slot", values, dim, texts) == {"M/Slot#2", "M/Slot#3"}
    assert any("文字で押せるかを決めた" in n and "？？？" in n for n in run.notes)
    # 並びに伏せ字がなければ、明るさのまま（灰色にした「続きから」、「空き」のセーブスロットなど）
    assert s._by_text("T/Btn", values, dim, {"M/Slot#1": "続きから"}) == dim
    run.inactive_words = ["空き"]
    assert s._by_text("S/Slot", values, dim, {"M/Slot#0": "1. 2026/11/05", "M/Slot#1": "2. 空き"}) == dim


def _color_shot(cards):
    """cards: [(rect, [(R, G, B), ...])]。四角の中を、色を縦の縞で順に塗った BGRA の画面（背景は灰色）。"""
    data = bytearray([128, 128, 128, 255] * (W * H))
    for (x0, y0, x1, y1), colors in cards:
        for y in range(int(y0 * H), int(y1 * H)):
            for x in range(int(x0 * W), int(x1 * W)):
                r, g, b = colors[x % len(colors)]
                p = (y * W + x) * 4
                data[p:p + 3] = bytes([b, g, r])
    return Shot(W, H, W * 4, bytes(data))


def test_faded_buttons_are_found_when_brightness_is_even():
    # 明るさは同じだが、半透明で後ろの灰色が透けている（色が薄く、濃淡が小さい）ボタンを見分ける
    from yomawari.fuzz.looks import disabled_looks
    from yomawari.sensors.screen import vividness

    def faded(ts, labels, shot, ratio, gap):
        return {k for k, v in disabled_looks(ts, labels, shot, ratio, 0.3, gap).items() if v == "faded"}

    slots = [_slot(i) for i in range(4)]
    vivid = [(230, 40, 40), (40, 40, 230), (240, 220, 60), (30, 160, 60)]
    # 同じ色を灰色（128）と半々に混ぜた（半透明）
    washed = [tuple((c + 128) // 2 for c in col) for col in vivid]
    shot = _color_shot([(slots[0].rect, vivid), (slots[1].rect, vivid), (slots[2].rect, washed), (slots[3].rect, washed)])
    m = measure(slots, shot)["AlbumUI/Slot"]
    assert dark(m, brightest(m), 0.6, 0.1) == set()  # 明るさでは差がない
    c0, s0 = vividness(shot, slots[0].rect)
    c2, s2 = vividness(shot, slots[2].rect)
    assert c2 < c0 and s2 < s0
    labels = [s.label for s in slots]
    assert faded(slots, labels, shot, 0.6, 0.05) == {labels[2], labels[3]}
    # 全部同じ見え方なら、どれも半透明としない。3つに満たなければ比べない
    same = _color_shot([(s.rect, vivid) for s in slots])
    assert faded(slots, labels, same, 0.6, 0.05) == set()
    assert faded(slots[:2], labels[:2], shot, 0.6, 0.05) == set()


def test_groups_that_looks_could_not_split_are_reported():
    # 見た目（明るさ・半透明・文字）で差が出なかったのに、押して反応したものとしなかったものが混じっていた並びは、
    # 推定できなかったとして「ハーネスの記録」に書く
    run = FuzzRun(mode="random", decider=RandomDecider(random.Random(1), 0.2), guard=Guard({}),
                  rng=random.Random(1), cfg=FUZZ_DEFAULTS, response_timeout=0.1)
    run.look_groups = {"A/Slot": {"A/Slot", "A/Slot#1", "A/Slot#2"}, "B/Slot": {"B/Slot", "B/Slot#1", "B/Slot#2"},
                       "C/Slot": {"C/Slot", "C/Slot#1", "C/Slot#2"}}
    run.look_split = {"B/Slot"}  # B は暗いもので見分けられた
    run.answered = {"A/Slot", "B/Slot", "C/Slot", "C/Slot#1"}
    run.silent = {"A/Slot#1", "B/Slot#1"}
    notes = run.looks_notes()
    assert len(notes) == 1 and "`A/Slot` の並びは、見た目" in notes[0] and "反応した 1 個と反応しなかった 1 個" in notes[0]


def test_faded_buttons_are_tried_later_and_noted():
    slots = [_slot(i) for i in range(4)]
    vivid = [(230, 40, 40), (40, 40, 230), (240, 220, 60), (30, 160, 60)]
    washed = [tuple((c + 128) // 2 for c in col) for col in vivid]
    shot = _color_shot([(slots[0].rect, vivid), (slots[1].rect, washed), (slots[2].rect, vivid), (slots[3].rect, washed)])
    s, st, run, windows, mouse, events = _list_session(slots, shot)
    asyncio.run(s._look())
    assert s._dim == s._disabled == {"AlbumUI/Slot#1", "AlbumUI/Slot#3"}
    assert any("半透明に見える" in n and "2/4 個" in n for n in run.notes)
    looks = [e for e in events if e.kind == "looks"][0].data["groups"]["AlbumUI/Slot"]
    assert looks["disabled_looks"] == {"AlbumUI/Slot#1": "faded", "AlbumUI/Slot#3": "faded"}
    assert run.look_split == {"AlbumUI/Slot"}
    # 画面を撮れなかったら、見た目で推定できなかったと1回書く
    windows.shot = None
    asyncio.run(s._look())
    asyncio.run(s._look())
    assert sum("画面を撮れなかった" in n for n in run.notes) == 1


def test_grey_buttons_are_found_when_brightness_is_even():
    # 色を抜いた（グレーアウトした）ボタン: 濃淡は残るが色がない。並びがもともと色のないボタンなら灰色としない
    from yomawari.fuzz.looks import disabled_looks

    slots = [_slot(i) for i in range(4)]
    labels = [s.label for s in slots]
    vivid = [(230, 40, 40), (40, 40, 230), (240, 220, 60), (30, 160, 60)]
    grey = [(v, v, v) for v in (int(0.299 * r + 0.587 * g + 0.114 * b) for r, g, b in vivid)]
    shot = _color_shot([(slots[0].rect, vivid), (slots[1].rect, vivid), (slots[2].rect, grey), (slots[3].rect, vivid)])
    m = measure(slots, shot)["AlbumUI/Slot"]
    assert dark(m, brightest(m), 0.6, 0.1) == set()
    assert disabled_looks(slots, labels, shot, 0.6, 0.3, 0.05) == {labels[2]: "grey"}
    mono = _color_shot([(s.rect, grey) for s in slots])
    assert disabled_looks(slots, labels, mono, 0.6, 0.3, 0.05) == {}


def test_empty_slot_is_not_newly_bright():
    # run 2026-11-06_1830: 空のセーブスロットの明るさが揺れて「明るくなった」と見なし、空のスロットを次々に先に押した。
    # 文字で空と分かるものは、明るく見えても先に押さない
    slots = [_slot(i) for i in range(4)]
    s, st, run, windows, mouse, events = _list_session(slots, _shot([(slots[0].rect, 230)] +
                                                                    [(x.rect, 46) for x in slots[1:]]))
    run.inactive_words = ["Empty"]
    asyncio.run(s._look())
    st.set_read_text({"AlbumUI/Slot#3": "4. Empty"})
    windows.shot = _shot([(slots[0].rect, 230), (slots[3].rect, 230)] + [(x.rect, 46) for x in slots[1:3]])
    asyncio.run(s._look())
    assert run.newly_bright == set() and not any("明るくなった" in n for n in run.notes)


def test_silent_slot_comes_back_when_revealed_by_look():
    # 開示前に押して反応しなかった思い出が、あとで開示されて明るくなったら、反応しないボタンから外して候補に戻す
    # （run 2026-11-07_1511: No.012 が候補から外れたままで、質問に答えられなかった）
    from yomawari.fuzz.listing import NO, UNKNOWN

    slots = [_slot(i) for i in range(3)]
    s, st, run, *_ = _list_session(slots, _shot([(x.rect, 120) for x in slots]))
    label = "AlbumUI/Slot#1"
    s._values = {"AlbumUI/Slot": {label: (0.13, 0.19)}}
    target = next(t for t in st.targets() if t.label == label)
    run.silent.add(label)
    s._note_silent(target, False)
    run.ledger.sync(st, run.masked_chars)
    run.ledger.items[label].status = NO
    # 後ろの絵の揺れで少し明るく見えただけでは戻さない（いちばん明るい見た目を更新する）
    s._revive_by_look({label: (0.14, 0.30)})
    assert label in run.silent and run.silent_look[label] == (0.14, 0.30)
    # はっきり明るくなった（開示された）
    s._revive_by_look({label: (0.41, 0.72)})
    assert label not in run.silent
    assert run.ledger.items[label].status == UNKNOWN
    assert any("開示されたとみなして" in n for n in run.notes)


def test_silent_slot_comes_back_when_title_becomes_readable():
    slots = [_slot(i) for i in range(3)]
    s, st, run, *_ = _list_session(slots, _shot([(x.rect, 120) for x in slots]))
    run.masked_chars = "？?"
    label = "AlbumUI/Slot#2"
    st.set_read_text({label: "No.012 ？？？"})
    run.silent.add(label)
    s._note_silent(next(t for t in st.targets() if t.label == label), False)
    st.set_read_text({label: "No.012 2？2"})  # 読み取りの揺れ（読める文字は増えていない）
    s._revive_by_text()
    assert label in run.silent
    st.set_read_text({label: "？？？ アカウント名を選ぼ"})  # 後ろの本文を一緒に読んだ（伏せ字のまま）
    s._revive_by_text()
    assert label in run.silent
    st.set_read_text({label: "No.012 雨の日の約束"})
    s._revive_by_text()
    assert label not in run.silent
    # 反応したら、反応しなかったときの記録を忘れる
    run.silent.add(label)
    s._note_silent(next(t for t in st.targets() if t.label == label), True)
    assert label not in run.silent_letters
