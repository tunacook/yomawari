"""V-03（ウィンドウが見えない・前面に戻らない）・V-04（解像度とウィンドウの大きさが合わない）。"""

from yomawari.detect.engine import Engine
from yomawari.events import Event


def ev(kind, t, source="game", **data):
    return Event(source, kind, data, launch=0, t=t, wall=f"w{t}")


def run(e, events, ticks=()):
    out = []
    timeline = sorted([(x.t, 0, x) for x in events] + [(t, 1, t) for t in ticks], key=lambda x: (x[0], x[1]))
    for _, kind, item in timeline:
        out += e.feed(item) if kind == 0 else e.tick(item, 0)
    return out


def stats(t, **sysd):
    return ev("proc_stats", t, source="proc", cpu=1.0, sys={"locked": False, "asleep": False, **sysd})


# --- V-04 ---

def res(t, w=1280, h=720, mode="Windowed"):
    return ev("resolution", t, w=w, h=h, mode=mode)


def test_v04_window_matches(repo_rules):
    e = Engine({"V-04": repo_rules["V-04"]})
    # Retina 2 倍: 1280x720 ピクセルは 640x360 ポイント。外枠はタイトルバー 28 ポイントの分だけ高い
    events = [res(0)] + [stats(t, window=[100, 100, 640, 388], backing=2.0) for t in range(1, 20)]
    assert run(e, events) == []


def test_v04_mismatch_lasting(repo_rules):
    e = Engine({"V-04": repo_rules["V-04"]})
    events = [res(0, 1920, 1080)] + [stats(t, window=[100, 100, 640, 388], backing=2.0) for t in range(1, 20)]
    issues = run(e, events)
    assert [(i.rule_id, i.severity, i.evidence["resolution"]) for i in issues] == [("V-04", "major", [1920, 1080])]
    assert "1920x1080" in issues[0].detail and "1280x776" in issues[0].detail


def test_v04_resize_in_progress_and_fullscreen_are_ignored(repo_rules):
    # 解像度を変えた直後、ウィンドウが追いつくまでの数秒は数えない
    e = Engine({"V-04": repo_rules["V-04"]})
    events = [res(0, 1920, 1080)] + [stats(t, window=[100, 100, 640, 388], backing=2.0) for t in (1, 2, 3)]
    events += [stats(t, window=[100, 100, 960, 568], backing=2.0) for t in range(4, 20)]
    assert run(e, events) == []
    # フルスクリーンでは、ゲームが描画を引き伸ばすので見ない
    e = Engine({"V-04": repo_rules["V-04"]})
    events = [res(0, 1280, 720, "FullScreenWindow")] + [stats(t, window=[0, 0, 1512, 982], backing=2.0)
                                                         for t in range(1, 20)]
    assert run(e, events) == []
    # 外枠か倍率が分からない（古いハーネスの記録など）
    e = Engine({"V-04": repo_rules["V-04"]})
    assert run(e, [res(0, 1920, 1080)] + [stats(t) for t in range(1, 20)]) == []


# --- V-03 ---

def blocked(t, error="ゲームを前面にできません"):
    return ev("input_blocked", t, source="harness", label="advance", error=error)


def test_v03_window_stays_hidden_after_harness_tried(repo_rules):
    e = Engine({"V-03": repo_rules["V-03"]})
    events = [stats(t, on_screen=False, front=False) for t in range(0, 40)] + [blocked(1)]
    issues = run(e, events, ticks=range(0, 40))
    assert [(i.rule_id, i.evidence["window"]) for i in issues] == [("V-03", "画面に出ていない（最小化など）")]


def test_v03_not_front(repo_rules):
    e = Engine({"V-03": repo_rules["V-03"]})
    events = [stats(t, on_screen=True, front=False) for t in range(0, 40)] + [blocked(2)]
    assert [i.evidence["window"] for i in run(e, events, ticks=range(0, 40))] == ["前面に戻らない"]


def test_v03_ignored_cases(repo_rules):
    # すぐ戻った
    e = Engine({"V-03": repo_rules["V-03"]})
    events = [stats(t, on_screen=True, front=t >= 10) for t in range(0, 40)] + [blocked(1)]
    assert run(e, events, ticks=range(0, 40)) == []
    # ハーネスが操作しようとしていない（操作しないモードなど）
    e = Engine({"V-03": repo_rules["V-03"]})
    assert run(e, [stats(t, on_screen=True, front=False) for t in range(0, 40)], ticks=range(0, 40)) == []
    # 画面のロック中
    e = Engine({"V-03": repo_rules["V-03"]})
    events = [ev("proc_stats", t, source="proc", sys={"locked": True, "front": False, "on_screen": True})
              for t in range(0, 40)] + [blocked(1, "画面がロックされています")]
    assert run(e, events, ticks=range(0, 40)) == []
