"""キーで出たボタンのないモーダルから、同じキーで抜ける（fuzz/loop.py の _track_toggle）。"""

import asyncio
import time

from yomawari.events import Event
from yomawari.fuzz.ops import Op

from .test_fuzz import _locale_session
from .test_looks import CFG

_PANEL = {"name": "HidePanel", "modal": True, "buttons": []}


def _game(s, st, kind, **d):
    ev = Event("game", kind, d, 0)
    st.feed(ev)
    s.on_event(ev)


def _session():
    st, run, s = _locale_session("ja-JP")
    run.cfg = {**CFG, "stuck_modal_sec": 0, "irregular_prob": 0}
    st.feed(Event("game", "boot", {}, 0))
    done = []

    async def do(op, **k):
        done.append(op.label)
        if op.kind in ("key", "rapid_key"):
            s._key_ops.append((time.monotonic(), str(op.arg)))
        return True

    async def look():
        return None

    s._do, s._look = do, look
    return st, run, s, done


def _stuck_step(s):
    asyncio.run(s._step())  # 画面が変わった（落ち着くのを待つ）
    asyncio.run(s._step())


def test_modal_opened_by_a_key_is_closed_with_the_same_key_first():
    # run 2026-11-16_1001: スペースの連打のあと UI を隠す機能のパネルが残り、Esc・右クリック・本文送りでは戻らなかった
    st, run, s, done = _session()
    s._key_ops.append((time.monotonic(), "space"))
    _game(s, st, "menu", **_PANEL)
    assert s._toggle_keys == {"HidePanel": "space"}
    _stuck_step(s)
    assert done == ["key space"]
    assert any("`HidePanel` から抜けられなかったので" in n and "`space`" in n for n in run.notes)
    # 1 回で戻らなければ、ほかの抜け方と一緒に試す（同じキーも候補に残す）
    toggle = s._toggle_op("escape")
    assert toggle is not None and toggle.label == "key space"
    s._sig = None
    _stuck_step(s)
    assert len(done) == 2 and done[1] in ("key esc", "right_click", "advance", "key space")
    # 閉じたら忘れる
    _game(s, st, "menu_close", name="HidePanel")
    assert s._toggle_keys == {} and s._toggle_tried == set()


def test_modal_without_a_key_just_before_uses_the_usual_escape():
    st, run, s, done = _session()
    s._key_ops.append((time.monotonic() - 10, "space"))  # toggle_key_sec より前に押したキーは関係ない
    _game(s, st, "menu", **_PANEL)
    assert s._toggle_keys == {}
    _stuck_step(s)
    assert done and done[0] in ("key esc", "right_click", "advance")
    assert s._toggle_op("escape") is None


def test_toggle_key_is_a_recovery_candidate():
    st, run, s, done = _session()
    s._key_ops.append((time.monotonic(), "space"))
    _game(s, st, "menu", **_PANEL)
    s._recovery = {"rule": "S-02", "left": 50, "since": st.last_progress_t, "ops": [], "on_fail": lambda: None}
    for _ in range(50):
        if "key space" in done:
            break
        asyncio.run(s._recover_step())
    assert "key space" in done


def test_key_is_remembered_before_the_presses_finish():
    # 連打の途中でモーダルが出て、そのイベントが押し終える前に届いても、そのキーで出たとみなす
    st, run, s = _locale_session("ja-JP")
    st.feed(Event("game", "boot", {}, 0))

    def press(*a, **k):
        _game(s, st, "menu", **_PANEL)

    s.pointer.key = press
    asyncio.run(s._do(Op("rapid_key", arg="space", irregular=True)))
    assert s._toggle_keys == {"HidePanel": "space"}
