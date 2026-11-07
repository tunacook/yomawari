"""パフォーマンスの判定（R-01〜R-10、detect/perf.py）を、rules.yaml の値のまま、時刻を与えて確かめる。"""

from __future__ import annotations

import json

from yomawari.detect.engine import Engine
from yomawari.detect.perf import MB, load_history
from yomawari.events import Event
from yomawari.platform.mac import parse_gpu_device, parse_gpu_time


class Sim:
    """1秒ごとに proc_stats と heartbeat を流す。起動から warmup_sec（30 秒）までは計らない。"""

    def __init__(self, rules, perf, launch: int = 0) -> None:
        self.engine = Engine(rules, perf)
        self.t = 0.0
        self.launch = launch
        self.issues = []
        self.feed("harness", "launch")
        self.feed("game", "boot")
        self.feed("game", "scene", name="Main")
        self.feed("game", "line", line_id="Main:1.0")

    def feed(self, source, kind, **data):
        self.issues += self.engine.feed(Event(source, kind, data, launch=self.launch, t=self.t, wall=f"w{self.t}"))

    def step(self, n=1, fps=60.0, frame=17.0, cpu=20.0, gpu=10.0, rss=500.0, total=None, gfx=None, threads=40,
             sys_cpu=10.0, **sysd):
        for _ in range(n):
            self.t += 1
            sys_ = {"cpu": sys_cpu, "ncpu": 8, "swap": 0, "gpu_device": gpu, "thermal": "nominal", "pressure": "normal",
                    "asleep": False, "locked": False, "front": True, "on_screen": True, "ai": False, **sysd}
            self.feed("proc", "proc_stats", cpu=cpu, gpu=gpu, rss=rss * MB, threads=threads, sys=sys_)
            hb = {"fps": fps, "frame_max": frame, "gc_mem": 1}
            if total is not None:
                hb["mem_total"] = total * MB
            if gfx is not None:
                hb["mem_gfx"] = gfx * MB
            self.feed("game", "heartbeat", **hb)
            self.issues += self.engine.tick(self.t, self.launch)
        return self

    def ids(self):
        return [i.rule_id for i in self.issues]


def warm(rules, perf, n=60):
    return Sim(rules, perf).step(30).step(n)


def test_r02_low_fps_needs_ten_seconds_outside_transition(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.step(9, fps=15)
    assert "R-02" not in s.ids()
    s.step(1, fps=60)  # 途切れたら数え直す
    s.feed("game", "transition", phase="start", name="Fade")
    s.step(20, fps=10)
    assert "R-02" not in s.ids()  # 遷移中は数えない
    s.feed("game", "transition", phase="end", name="Fade")
    s.step(5)  # 遷移のあとは settle_sec のあいだ数えない
    s.step(10, fps=12)
    issues = [i for i in s.issues if i.rule_id == "R-02"]
    assert len(issues) == 1 and issues[0].severity == "major" and "Main:1.0" in issues[0].detail


def test_low_fps_is_skipped_when_the_cause_is_outside_the_game(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.step(15, fps=10, ai=True)  # AI に聞いている
    s.step(15, fps=10, cpu=20, sys_cpu=90.0)  # ほかのプロセスが CPU を使っている（全体 90% のうちゲームは 20/8%）
    s.step(15, fps=10, front=False)  # ゲームが前面にない
    s.step(15, fps=10, thermal="serious")
    s.step(15, fps=10, pressure="warning")
    s.step(15, fps=10, asleep=True)
    assert "R-02" not in s.ids()
    note = s.engine.perf.excluded_note()
    for words in ("AI", "ほかのプロセス", "前面", "熱", "逼迫", "画面が消えて"):
        assert words in note


def test_game_cpu_spike_is_judged_even_with_other_load(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    # ほかのプロセスが重くても、ゲームの CPU はプロセス単位なので判定する（ふだん 20%、+50 かつ 2 倍以上が 3 秒）
    s.step(2, cpu=90, sys_cpu=95.0)
    assert "R-04" not in s.ids()
    s.step(1, cpu=90, sys_cpu=95.0)
    assert s.ids().count("R-04") == 1
    # AI に聞いているあいだは外す
    s2 = warm(repo_rules, repo_perf)
    s2.step(5, gpu=80, ai=True)
    assert "R-05" not in s2.ids()
    s2.step(3, gpu=80)
    assert s2.ids().count("R-05") == 1


def test_r06_memory_jump_that_stays(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.step(5, rss=900)  # 300MB 以上増えた
    s.step(60, rss=800)  # 60 秒たっても、増えた分の半分以上が残る
    assert s.ids().count("R-06") == 1
    s2 = warm(repo_rules, repo_perf)
    s2.step(5, rss=900)
    s2.step(60, rss=550)  # ほとんど戻った
    assert "R-06" not in s2.ids()


def test_r07_hitch_after_the_same_command_twice(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.feed("game", "command", name="spawn", target="Rain", line_id="Main:3.0")
    s.step(1, frame=150)
    assert "R-07" not in s.ids()
    s.step(10)
    s.feed("game", "line", line_id="Main:9.0")
    s.feed("game", "command", name="spawn", target="Rain", line_id="Main:9.0")
    s.step(1, frame=180)
    issues = [i for i in s.issues if i.rule_id == "R-07"]
    assert len(issues) == 1 and "@spawn Rain" in issues[0].detail


def test_r08_command_that_drops_fps_twice(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    for n in range(2):
        s.feed("game", "command", name="spawn", target="Rain", line_id=f"Main:{n}.0")
        s.step(4, fps=30, gpu=60)
        s.step(6)
    issues = [i for i in s.issues if i.rule_id == "R-08"]
    assert {i.key for i in issues} == {"R-08:@spawn Rain|fps", "R-08:@spawn Rain|gpu"}
    # 軽いコマンドは出さないが、レポートの一覧には載る
    s.feed("game", "command", name="char", target="Kaori", line_id="Main:5.0")
    s.step(10)
    stats = s.engine.perf.command_stats()
    assert stats["@char Kaori"]["n"] == 1 and stats["@spawn Rain"]["fps"] > 0.4
    lines = "\n".join(s.engine.perf.report_lines())
    assert lines.index("@spawn Rain") < lines.index("@char Kaori")


def test_r01_memory_growth_and_r03_threads(repo_rules, repo_perf):
    s = Sim(repo_rules, repo_perf).step(30)
    for n in range(700):
        s.step(1, rss=500 + n * 0.1, threads=40 + n // 30)  # 1 時間あたり 360MB、スレッドは 10 分で 20 増える
    assert s.ids().count("R-01") == 1
    assert s.ids().count("R-03") == 1


def test_r09_scene_heavier_than_the_rest_and_r10_history(repo_rules, repo_perf, tmp_path):
    s = warm(repo_rules, repo_perf, n=100)
    s.feed("game", "scene", name="Rain")
    s.step(40, fps=30)
    issues = s.engine.finish(s.t)
    assert [i.rule_id for i in issues] == ["R-09"] and "Rain" in issues[0].detail

    # 前回までのラン（同じビルド名・同じマシン）と比べる
    runs = tmp_path / "runs"
    prev = runs / "2026-11-01_0000_game"
    prev.mkdir(parents=True)
    other = runs / "2026-11-02_0000_game"
    other.mkdir()
    s2 = warm(repo_rules, repo_perf, n=100)  # 前回: Main は 60fps
    (prev / "perf.json").write_text(json.dumps(s2.engine.perf.summary_json("game", "Mac15,8", "g1")))
    (other / "perf.json").write_text(json.dumps({"build": "game", "machine": "OtherMac", "scenes": {}}))
    hist = load_history(runs, "game", "Mac15,8", None, 5)
    assert [h["run"] for h in hist] == [prev.name]

    s3 = warm(repo_rules, repo_perf, n=100)
    for x in s3.engine.perf.samples:
        x.fps = 40.0  # 今回: Main がまんべんなく 40fps（ラン全体と同じなので R-09 は出ない）
    s3.engine.perf.history = hist
    issues = s3.engine.finish(s3.t)
    assert [i.rule_id for i in issues] == ["R-10"] and "60.0 → 40.0" in issues[0].detail


def test_report_lines_table(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    lines = s.engine.perf.report_lines()
    assert lines[0] == "## パフォーマンス"
    assert any(line.startswith("| `Main` | 61 | 60.0") for line in lines)


def test_without_perf_settings_r_rules_are_listed_as_unimplemented(repo_rules):
    e = Engine(repo_rules)
    assert e.perf is None and "R-01" in e.unimplemented


def test_parse_ioreg_gpu():
    import plistlib

    clients = plistlib.dumps([
        {"IOUserClientCreator": "pid 42, Game", "AppUsage": [{"accumulatedGPUTime": 100}, {"accumulatedGPUTime": 5}]},
        {"IOUserClientCreator": "pid 42, Game", "AppUsage": [{"accumulatedGPUTime": 10}]},
        {"IOUserClientCreator": "pid 7, WindowServer", "AppUsage": [{"accumulatedGPUTime": 999}]},
    ])
    assert parse_gpu_time(clients, 42) == 115
    assert parse_gpu_time(clients, 1) is None
    acc = plistlib.dumps([{"PerformanceStatistics": {"Device Utilization %": 17}}])
    assert parse_gpu_device(acc) == 17.0
    assert parse_gpu_device(b"broken") is None
