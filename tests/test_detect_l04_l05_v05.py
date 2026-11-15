"""L-04（同じ警告が続けて大量に出る）・L-05（[YOMAWARI] 行の取りこぼし）・V-05（ボタン・選択肢が画面の外にある）。"""

from yomawari.detect.engine import Engine
from yomawari.events import Event


def ev(kind, t, launch=0, source="game", **data):
    return Event(source, kind, data, launch=launch, t=t, wall=f"w{t}")


def engine(repo_rules, *ids):
    return Engine({i: repo_rules[i] for i in ids})


def feed(e, events, ticks=()):
    out = []
    timeline = sorted([(x.t, 0, x) for x in events] + [(t, 1, t) for t in ticks], key=lambda x: (x[0], x[1]))
    for _, kind, item in timeline:
        out += e.feed(item) if kind == 0 else e.tick(item, 0)
    return out


# --- L-05 ---

def test_l05_seq_gap(repo_rules):
    e = engine(repo_rules, "L-05")
    issues = feed(e, [ev("boot", 0, seq=0), ev("line", 1, seq=1), ev("line", 2, seq=4), ev("line", 3, seq=5)])
    assert [(i.rule_id, i.severity, i.evidence["missing"]) for i in issues] == [("L-05", "info", 2)]


def test_l05_seq_restarts_per_launch(repo_rules):
    e = engine(repo_rules, "L-05")
    events = [ev("boot", 0, seq=0), ev("line", 1, seq=1),
              ev("boot", 10, launch=1, seq=0), ev("line", 11, launch=1, seq=1)]
    assert feed(e, events) == []


def test_l05_ignores_harness_events(repo_rules):
    e = engine(repo_rules, "L-05")
    assert feed(e, [ev("boot", 0, seq=0), ev("op", 1, source="harness", seq=9), ev("line", 2, seq=1)]) == []


# --- L-04 ---

def _warn(t, msg):
    return ev("log_warning", t, message=msg)


def test_l04_flood_of_same_kind(repo_rules):
    e = engine(repo_rules, "L-04")
    count = repo_rules["L-04"].param("count")
    # 名前と数値だけ違う警告は同じ種類
    events = [_warn(i * 0.1, f"Missing glyph {i} in 'Font{i}'") for i in range(count + 20)]
    issues = feed(e, events)
    assert [(i.rule_id, i.severity) for i in issues] == [("L-04", "minor")]  # 種類ごとに起動につき1件


def test_l04_slow_warnings_do_not_flood(repo_rules):
    e = engine(repo_rules, "L-04")
    window = repo_rules["L-04"].param("window_sec")
    count = repo_rules["L-04"].param("count")
    step = window / (count - 1) * 1.01  # 窓の中には count 回まで入らない
    assert feed(e, [_warn(i * step, "Same warning") for i in range(count * 2)]) == []


# --- V-05 ---

def _menu(t, buttons, scroll=()):
    return ev("menu", t, name="PauseUI", modal=True, buttons=buttons, scroll=list(scroll))


OUT = [0.1, -0.3, 0.3, -0.2]
IN = [0.1, 0.1, 0.3, 0.2]


def test_v05_button_stays_offscreen(repo_rules):
    e = engine(repo_rules, "V-05")
    issues = feed(e, [_menu(0, [{"name": "A", "rect": IN}, {"name": "B", "rect": OUT}])], ticks=[1, 4, 5])
    assert [(i.rule_id, i.evidence["target"]) for i in issues] == [("V-05", "PauseUI/B")]


def test_v05_slide_in_animation_is_ignored(repo_rules):
    e = engine(repo_rules, "V-05")
    events = [_menu(0, [{"name": "B", "rect": OUT}]), _menu(0.4, [{"name": "B", "rect": IN}])]
    assert feed(e, events, ticks=[1, 4, 10]) == []


def test_v05_closed_menu_is_forgotten(repo_rules):
    e = engine(repo_rules, "V-05")
    events = [_menu(0, [{"name": "B", "rect": OUT}]), ev("menu_close", 1, name="PauseUI")]
    assert feed(e, events, ticks=[4, 10]) == []


def test_v05_scroll_hidden_and_old_package_are_ignored(repo_rules):
    for menu in (
        _menu(0, [{"name": "B", "rect": OUT}], scroll=[{"rect": [0, 0, 1, 1]}]),  # スクロールする画面
        _menu(0, [{"name": "B", "rect": OUT, "hidden": True}]),  # マスクで隠れている
        ev("menu", 0, name="PauseUI", modal=True, buttons=[{"name": "B", "rect": OUT}]),  # scroll を出さない古いパッケージ
    ):
        assert feed(engine(repo_rules, "V-05"), [menu], ticks=[4, 10]) == []


def test_v05_choice_offscreen(repo_rules):
    e = engine(repo_rules, "V-05")
    issues = feed(e, [ev("choices", 0, items=[{"id": "c1", "rect": [1.2, 0.4, 1.5, 0.5]}])], ticks=[4])
    assert [i.evidence["target"] for i in issues] == ["choice:c1"]
    e = engine(repo_rules, "V-05")
    events = [ev("choices", 0, items=[{"id": "c1", "rect": [1.2, 0.4, 1.5, 0.5]}]), ev("choices_done", 1)]
    assert feed(e, events, ticks=[4]) == []
