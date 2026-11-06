"""進めなかったところ（post/stalls.py）。"""

from yomawari.post.stalls import find_stalls, render_stalls


def _ev(t, ev_kind, source="game", **data):
    return {"source": source, "kind": ev_kind, "data": data, "launch": 0, "t": float(t), "wall": f"T{t}"}


def test_stall_ignores_new_lines_inside_flow_and_counts_rereads():
    # run 2026-11-06_1012: 同じ質問で思い出を読み続けた（思い出の本文は新しいが、本編は進んでいない）
    ev = [
        _ev(0, "launch", "harness"),
        _ev(1, "scene", name="main/s4"),
        _ev(2, "line", line_id="main/s4:281", text="それは……"),
        _ev(10, "flow", "harness", flow="質問に答える", step=1, **{"from": {"flow": None, "step": 1}}),
        _ev(20, "line", line_id="memories/m1:1", text="思い出"),
        _ev(30, "op", "harness", kind="click", label="click MemoryListUI/Slot#1"),
        _ev(31, "op_result", "harness", responded=False),
        _ev(40, "flow", "harness", flow=None, step=None, **{"from": {"flow": "質問に答える", "step": 7}}),
        _ev(50, "load", slot="Save1", phase="start"),
        _ev(60, "line", line_id="main/s4:281", text="それは……"),
        _ev(400, "op", "harness", kind="advance", label="advance"),
        _ev(401, "line", line_id="main/s5:1", text="次の章"),
        _ev(500, "line", line_id="main/s5:2", text="つづき"),
    ]
    stalls = find_stalls(ev, 300)
    assert len(stalls) == 1
    st = stalls[0]
    assert st.sec == 399 and st.scene == "main/s4" and st.line_id == "main/s4:281"
    assert st.flow_new == 1 and st.reread == 1 and st.loads == 1
    assert st.ops == 2 and st.no_response == 1 and st.flows["質問に答える"] == 1
    assert st.ended == "`advance` のあと、新しいところへ進んだ"
    text = "\n".join(render_stalls(stalls, 300))
    assert "## 進めなかったところ" in text and "7 分" in text and "思い出など" in text


def test_stall_until_run_end_and_none():
    ev = [_ev(0, "launch", "harness"), _ev(1, "line", line_id="a:1", text="x"),
          _ev(2, "op", "harness", kind="advance", label="advance"), _ev(700, "heartbeat", "harness")]
    stalls = find_stalls(ev, 300)
    assert stalls[0].ended == "ランが終わるまで進まなかった"
    assert find_stalls(ev, 0) == []
    assert render_stalls([], 300)[-2] == "なし"
