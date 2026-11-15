"""レポートの「カバレッジ」の節（post/coverage.py）。"""

from yomawari.post.coverage import collect, render


def g(kind, n, **data):
    return {"source": "game", "kind": kind, "data": data, "launch": 0, "t": n, "wall": f"2026-11-15T01:00:{n:02d}"}


def h(kind, n, **data):
    return {"source": "harness", "kind": kind, "data": data, "launch": 0, "t": n, "wall": f"2026-11-15T01:00:{n:02d}"}


def btn(name):
    return {"name": name, "rect": [0.1, 0.1, 0.2, 0.2], "type": "button"}


EVENTS = [
    h("launch", 0, pid=1),
    g("menu", 1, name="TitleUI", modal=False, buttons=[btn("Start"), btn("Settings")]),
    h("op", 2, label="click TitleUI/Start"),
    g("menu_close", 3, name="TitleUI"),
    g("scene", 4, name="main/a"),
    g("line", 5, label="main/a", line_id="main/a:1"),
    h("op", 6, label="advance"),
    g("line", 7, label="main/a", line_id="main/a:2"),
    g("line", 8, label="main/a", line_id="main/a:1"),  # 読み直した行は 1 つに数える
    g("menu", 9, name="PauseUI", modal=True, buttons=[btn("Slot"), btn("Slot"), btn("Close")]),
    h("op", 10, label="click PauseUI/Slot#1"),
    h("op", 11, label="click PauseUI/Slot#1"),
    h("op", 12, label="key esc"),
    g("menu_close", 13, name="PauseUI"),
    g("choices", 14, items=[{"text": "はい"}, {"text": "いいえ"}]),
    h("op", 15, label="click choices/はい"),
    g("line", 16, label="main/a", line_id="main/a:3"),  # 選んだ選択肢の行が choices_done より先に届く
    g("choices", 17, items=[]),
    g("choices_done", 18, index=0),
    h("op", 19, label="advance"),
    g("scene", 20, name="main/b"),
]


def test_collect():
    cov = collect(EVENTS)
    assert cov.scenes == {"main/a": 4.0, "main/b": 20.0}
    assert {k: len(v) for k, v in cov.lines.items()} == {"main/a": 3}
    assert cov.menus["PauseUI"].buttons == {"PauseUI/Slot", "PauseUI/Slot#1", "PauseUI/Close"}
    assert cov.menus["PauseUI"].pressed == {"PauseUI/Slot#1"}
    assert cov.pairs == {
        ("本文", "click TitleUI/Start"): 1,  # モーダルでない画面は状態に入れない
        ("本文", "advance"): 2,
        ("PauseUI", "click PauseUI/Slot#1"): 2,
        ("PauseUI", "key esc"): 1,
        ("選択肢", "click choices/はい"): 1,
    }
    assert cov.choices == {("はい", "いいえ"): {0}}


def test_choices_cleared_by_load():
    events = [g("choices", 1, items=[{"text": "A"}, {"text": "B"}]), g("load", 2, slot="s", phase="start"),
              h("op", 3, label="advance")]
    assert collect(events).pairs == {("本文", "advance"): 1}


def test_render():
    out = "\n".join(render(EVENTS, {}))
    assert "scene: 2 個、本文の行: 3 行" in out
    assert "選択肢: 1 種類が出て、選んだのはのべ 1 通り" in out
    assert "画面: 2 個。ボタン 5 個のうち、押したのは 2 個" in out
    assert "| `PauseUI` | 3 | 1 | `PauseUI/Close`, `PauseUI/Slot` |" in out
    assert "| `main/a` | 3 | 0:00:04 |" in out
    assert "| `本文` | 2 | 3 |" in out
    assert "choices" not in out.split("### 画面とボタン")[1].split("###")[0]  # 選択肢はボタンの表に出さない


def test_render_limits_rows_and_empty():
    events = [g("scene", n, name=f"s{n}") for n in range(5)]
    out = render(events, {"report_coverage_rows": 2})
    assert "| ほか 3 個 | | |" in out
    assert render([h("launch", 0, pid=1)], {}) == []
