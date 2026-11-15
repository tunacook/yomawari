"""A-01（BGM の重なり）・A-02（タイトルに戻ってもボイス）・A-03（音量の設定が音に効かない）。"""

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


# --- A-01 ---

def test_a01_overlap_lasting(repo_rules):
    e = Engine({"A-01": repo_rules["A-01"]})
    events = [ev("bgm_play", 0, id="Music/A"), ev("bgm_play", 10, id="Music/B")]
    issues = run(e, events, ticks=range(0, 30))
    assert [(i.rule_id, i.evidence["bgm"]) for i in issues] == [("A-01", ["Music/A", "Music/B"])]


def test_a01_crossfade_and_switch_are_ignored(repo_rules):
    e = Engine({"A-01": repo_rules["A-01"]})
    # 前の曲を止めてから次へ（止めた時点で外れる）。重なりが短い
    events = [ev("bgm_play", 0, id="Music/A"), ev("bgm_play", 10, id="Music/B"), ev("bgm_stop", 12, id="Music/A"),
              ev("bgm_play", 20, id="Music/C"), ev("bgm_stop", 20, id="Music/B")]
    assert run(e, events, ticks=range(0, 40)) == []
    # 起動し直したら鳴っているものを忘れる
    e = Engine({"A-01": repo_rules["A-01"]})
    events = [ev("bgm_play", 0, id="Music/A"), ev("proc_exit", 1, source="proc", code=0, by_harness=True),
              ev("bgm_play", 2, id="Music/B")]
    assert run(e, events, ticks=range(0, 20)) == []


# --- A-02 ---

def menu(t, name, buttons=("Start",)):
    return ev("menu", t, name=name, modal=False, buttons=[{"name": b, "rect": [0, 0, 0.1, 0.1]} for b in buttons])


def test_a02_voice_continues_after_title(repo_rules):
    e = Engine({"A-02": repo_rules["A-02"]})
    events = [menu(0, "TitleUI"), ev("menu_close", 1, name="TitleUI"), ev("voice", 20, id="Voice/001"),
              menu(21, "TitleUI")]
    issues = run(e, events, ticks=range(0, 40))
    assert [(i.rule_id, i.evidence["voice"]) for i in issues] == [("A-02", "Voice/001")]


def test_a02_ignored_cases(repo_rules):
    # ボイスがタイトルに戻ってすぐ止まった
    e = Engine({"A-02": repo_rules["A-02"]})
    events = [menu(0, "TitleUI"), ev("menu_close", 1, name="TitleUI"), ev("voice", 20, id="Voice/001"),
              menu(21, "TitleUI"), ev("voice_stop", 22, id="Voice/001")]
    assert run(e, events, ticks=range(0, 40)) == []
    # タイトルで鳴り始めたボイス（タイトルの演出）と、最初のタイトル
    e = Engine({"A-02": repo_rules["A-02"]})
    events = [menu(0, "TitleUI"), ev("voice", 1, id="Voice/title"), ev("menu_close", 5, name="TitleUI"),
              menu(6, "TitleUI"), ev("voice", 7, id="Voice/title2")]
    assert run(e, events, ticks=range(0, 40)) == []


# --- A-03 ---

def hb(t, db):
    return ev("heartbeat", t, fps=60, audio_db=db)


def settings(t, master=1.0, bgm=1.0):
    return ev("settings", t, locale="ja-JP", master_volume=master, bgm_volume=bgm, sfx_volume=1, voice_volume=1)


def test_a03_volume_not_effective(repo_rules):
    e = Engine({"A-03": repo_rules["A-03"]})
    events = [settings(0), ev("bgm_play", 0, id="Music/A")]
    events += [hb(t, -20.0 + (t % 2) * 0.5) for t in range(1, 30)]
    events += [settings(15, master=0.25)]  # 見込みは -12 dB
    issues = run(e, events, ticks=range(0, 30))
    assert [(i.rule_id, i.evidence["setting"], i.evidence["expected_db"]) for i in issues] == [
        ("A-03", "master_volume", -12.0)]


def test_a03_volume_effective_or_not_judged(repo_rules):
    # 効いている（-12 dB 下がった）
    e = Engine({"A-03": repo_rules["A-03"]})
    events = [settings(0), ev("bgm_play", 0, id="Music/A"), settings(15, bgm=0.25)]
    events += [hb(t, -20.0 if t < 15 else -32.0) for t in range(1, 30)]
    assert run(e, events, ticks=range(0, 30)) == []
    # 変えた量が小さい（-1.9 dB）
    e = Engine({"A-03": repo_rules["A-03"]})
    events = [settings(0), settings(15, master=0.8)] + [hb(t, -20.0) for t in range(1, 30)]
    assert run(e, events, ticks=range(0, 30)) == []
    # 比べるあいだに BGM が変わった・ボイスが鳴った・音が小さすぎる
    for extra, db in (([ev("bgm_play", 17, id="Music/B")], -20.0), ([ev("voice", 13, id="V/1")], -20.0), ([], -80.0)):
        e = Engine({"A-03": repo_rules["A-03"]})
        events = [settings(0), settings(15, master=0.1), *extra] + [hb(t, db) for t in range(1, 30)]
        assert run(e, events, ticks=range(0, 30)) == [], extra
    # 古いパッケージ（audio_db を出さない）
    e = Engine({"A-03": repo_rules["A-03"]})
    events = [settings(0), settings(15, master=0.1)] + [ev("heartbeat", t, fps=60) for t in range(1, 30)]
    assert run(e, events, ticks=range(0, 30)) == []
