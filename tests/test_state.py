from yomawari.events import Event
from yomawari.state import GameState


def game(kind, **data):
    return Event("game", kind, {"ev": kind, **data}, t=1.0)


def test_menu_and_modal_targets():
    s = GameState()
    s.feed(game("menu", name="TitleUI", modal=False, buttons=[
        {"name": "NewGameButton", "rect": [0.1, 0.5, 0.3, 0.6]},
        {"name": "Broken", "rect": [0.3, 0.5, 0.1, 0.6]},  # 幅が負なので捨てる
    ]))
    assert [t.key for t in s.targets()] == ["TitleUI/NewGameButton"]

    s.feed(game("menu", name="RClickMenuUI", modal=True, buttons=[{"name": "ToSave", "rect": [0.8, 0.1, 0.9, 0.2]}]))
    assert s.top_modal().name == "RClickMenuUI"
    assert [t.key for t in s.targets()] == ["RClickMenuUI/ToSave"]
    assert s.find("TitleUI/NewGameButton") is not None  # モーダルの下にあっても find では見つかる

    s.feed(game("menu_close", name="RClickMenuUI"))
    assert [t.key for t in s.targets()] == ["TitleUI/NewGameButton"]


def test_menu_update_keeps_order():
    s = GameState()
    s.feed(game("menu", name="A", modal=True, buttons=[]))
    s.feed(game("menu", name="B", modal=True, buttons=[]))
    s.feed(game("menu", name="A", modal=True, buttons=[{"name": "x", "rect": [0, 0, 1, 1]}]))  # A の出し直し
    assert s.top_modal().name == "B"


def test_modal_without_buttons_blocks_targets():
    # Rachel の CurtainUI（暗転）はボタン0個のモーダルとして出る。その間は押せるものがない
    s = GameState()
    s.feed(game("menu", name="TitleUI", modal=False, buttons=[{"name": "b", "rect": [0, 0, 1, 1]}]))
    s.feed(game("menu", name="CurtainUI", modal=True, buttons=[]))
    assert s.targets() == []


def test_line_choices_transition_save():
    s = GameState()
    s.feed(game("scene", name="main/start"))
    s.feed(game("line", label="main/start", line_id="main/start:3.0", speaker=None, text="本文"))
    assert s.line["line_id"] == "main/start:3.0" and not s.line_done
    s.feed(game("line_done", line_id="main/start:3.0"))
    assert s.line_done

    s.feed(game("choices", items=[
        {"id": "a", "text": "はい", "rect": [0.4, 0.4, 0.6, 0.45], "locked": False},
        {"id": "b", "text": "いいえ", "rect": [0.4, 0.5, 0.6, 0.55], "locked": True},
    ]))
    assert [(t.key, t.index) for t in s.targets()] == [("choices/a", 0)]
    s.feed(game("choices_done", id="a", index=0))
    assert s.choices == []

    s.feed(game("transition", phase="start", name="SceneTransitionUI"))
    assert s.busy
    s.feed(game("transition", phase="end", name="SceneTransitionUI"))
    s.feed(game("save", slot="1", type="Normal", phase="start"))
    assert s.busy
    s.feed(game("save", slot="1", type="Normal", phase="end"))
    assert not s.busy
    assert s.snapshot()["scene"] == "main/start"


def test_launch_resets_state():
    s = GameState()
    s.feed(game("scene", name="x"))
    s.feed(game("menu", name="TitleUI", modal=False, buttons=[]))
    s.feed(Event("harness", "launch", {"pid": 1}, launch=2))
    assert s.scene is None and s.menus == {} and s.launch == 2
