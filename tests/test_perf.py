"""パフォーマンスの判定（R-01〜R-12、detect/perf.py）を、rules.yaml の値のまま、時刻を与えて確かめる。"""

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
    s2 = warm(repo_rules, repo_perf, n=300)
    s2.step(10, fps=40, ai=True)
    assert "R-05" not in s2.ids()


def test_r05_fps_drop_from_usual_and_gpu_is_only_a_clue(repo_rules, repo_perf):
    # ふだん 60 から 2 割以上落ちたまま 5 秒。そのときの GPU は内容に書く
    s = warm(repo_rules, repo_perf, n=300)
    s.step(4, gpu=80, fps=45)
    assert "R-05" not in s.ids()
    s.step(1, gpu=80, fps=45)
    issues = [i for i in s.issues if i.rule_id == "R-05"]
    assert len(issues) == 1 and "GPU 80%" in issues[0].detail and "Main:1.0" in issues[0].detail
    # 落ち方が小さければ出さない（GPU をどれだけ使っていても）
    s2 = warm(repo_rules, repo_perf, n=300)
    s2.step(20, gpu=90, fps=52)
    assert "R-05" not in s2.ids()
    # ふだんの値は 300 サンプルたまってから決める（始めの場面だけで決めない）
    s3 = warm(repo_rules, repo_perf, n=100)
    s3.step(10, fps=40)
    assert "R-05" not in s3.ids()
    # FPS が 20 未満まで落ちても、同じ落ち込みは R-05 の 1 件（R-02 は出さない）
    s4 = warm(repo_rules, repo_perf, n=300)
    s4.step(15, fps=10)
    assert s4.ids().count("R-05") == 1 and "R-02" not in s4.ids()


def test_r11_cpu_heavy_without_gpu(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.step(9, cpu=150, gpu=5)
    assert "R-11" not in s.ids()
    s.step(1, cpu=150, gpu=5)
    issues = [i for i in s.issues if i.rule_id == "R-11"]
    assert len(issues) == 1 and "Main:1.0" in issues[0].detail
    # GPU も使っていれば出さない
    s2 = warm(repo_rules, repo_perf)
    s2.step(20, cpu=150, gpu=40)
    assert "R-11" not in s2.ids()


def test_memory_jump_that_is_released_later_is_not_reported(repo_rules, repo_perf):
    # 短いあいだの増減（読み込みで増えて、あとで解放される）は見ない（R-06 は消した。R-08 もメモリを見ない）
    s = warm(repo_rules, repo_perf)
    for n in range(3):
        s.feed("game", "command", name="back", target="hall", line_id=f"Main:{n}.0")
        s.step(5, rss=1300)
        s.step(60, rss=1200)
    assert not [i for i in s.ids() if i in ("R-06", "R-08", "R-01")]


def test_r07_hitch_after_the_same_command_twice(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.feed("game", "command", name="spawn", target="Rain", line_id="Main:3.0")
    s.step(1, frame=150)
    assert "R-07" not in s.ids()
    s.step(10)
    s.feed("game", "line", line_id="Main:9.0")
    s.feed("game", "command", name="spawn", target="Rain", line_id="Main:9.0")
    s.step(1, frame=180)
    s.step(3)  # 深刻度は、そのあと 3 秒の FPS の中央値を見てから決める
    issues = [i for i in s.issues if i.rule_id == "R-07"]
    assert len(issues) == 1 and "@spawn Rain" in issues[0].detail
    assert issues[0].severity == "minor" and "ほとんど変わらなかった" in issues[0].detail


def test_r07_is_major_when_fps_median_drops(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    for n in range(2):
        s.feed("game", "line", line_id="Main:3.0")
        s.step(1, frame=300, fps=30)
        s.step(3, fps=40)  # 重いフレームのあとも FPS が落ちたまま（中央値 40、ふだん 60）
        s.step(10)
    issues = [i for i in s.issues if i.rule_id == "R-07"]
    assert len(issues) == 1 and issues[0].severity == "major" and "ふだん 60.0" in issues[0].detail


def test_r07_one_heavy_frame_is_one_issue(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    for _ in range(3):
        s.feed("game", "line", line_id="Main:3.0")
        s.feed("game", "command", name="camera", line_id="Main:3.0")
        s.feed("game", "command", name="wait", target="1.0", line_id="Main:3.0")
        s.step(1, frame=150)
        s.step(10)
    issues = [i for i in s.issues if i.rule_id == "R-07"]
    assert len(issues) == 1
    assert "行 `Main:3.0`" in issues[0].detail and "`@camera` のあと" in issues[0].detail


def test_r08_command_that_drops_fps_twice(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    for n in range(2):
        s.feed("game", "command", name="spawn", target="Rain", line_id=f"Main:{n}.0")
        s.step(4, fps=30, gpu=60)
        s.step(6)
    issues = [i for i in s.issues if i.rule_id == "R-08"]
    assert {i.key for i in issues} == {"R-08:@spawn Rain|fps", "R-08:@spawn Rain|gpu"}
    # 軽いコマンドは出さないが、レポートの一覧には載る
    s.feed("game", "command", name="char", target="Aoi", line_id="Main:5.0")
    s.step(10)
    stats = s.engine.perf.command_stats()
    assert stats["@char Aoi"]["n"] == 1 and stats["@spawn Rain"]["fps"] > 0.4
    lines = "\n".join(s.engine.perf.report_lines())
    assert lines.index("@spawn Rain") < lines.index("@char Aoi")


def test_r08_one_slowdown_is_one_issue_for_commands_run_together(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    for n in range(3):  # 同じ場所を 3 回通る（3 回目で 2 回目になるコマンドがあっても、同じ場所なので出さない）
        s.feed("game", "command", name="hidePrinter", line_id="Main:3.0")
        s.step(1)
        s.feed("game", "command", name="goto", target="main/next", line_id="Main:3.1")
        s.feed("game", "command", name="wait", target="0", line_id="Main:3.2")
        if n == 1:
            s.feed("game", "command", name="stopBgm", line_id="Main:3.3")
        s.step(4, cpu=120)
        s.step(60)
    issues = [i for i in s.issues if i.rule_id == "R-08"]
    assert len(issues) == 1 and "同じときに悪くなったコマンド" in issues[0].detail
    # GPU が上がっても FPS が落ちなければ出さない
    s2 = warm(repo_rules, repo_perf)
    for n in range(2):
        s2.feed("game", "command", name="camera", target="", line_id=f"Main:{n}.0")
        s2.step(4, gpu=80)
        s2.step(6)
    assert "R-08" not in s2.ids()


def test_r01_memory_growth_and_r03_threads(repo_rules, repo_perf):
    s = Sim(repo_rules, repo_perf).step(30)
    for n in range(700):
        s.step(1, threads=40 + n // 30)  # スレッドは 10 分で 20 増える
    assert s.ids().count("R-03") == 1
    # 下限が下がらずに増え続ける（読み込みで上下しながら、下限が 5 分で 40MB ずつ上がる）
    s = Sim(repo_rules, repo_perf).step(30)
    for n in range(3300):
        s.step(1, rss=500 + n * 40 / 300 + (300 if n % 60 < 10 else 0))
    assert "R-01" not in s.ids()  # 55 分では、まだ 60 分に満たない
    for n in range(3300, 3900):
        s.step(1, rss=500 + n * 40 / 300 + (300 if n % 60 < 10 else 0))
    issues = [i for i in s.issues if i.rule_id == "R-01"]
    assert len(issues) == 1 and "下がらずに増え続けて" in issues[0].detail


def test_r01_memory_that_is_released_is_not_reported(repo_rules, repo_perf):
    # run 2026-11-07_1511 の RSS のように、上がっても途中で下限が戻る（解放される）なら出さない
    s = Sim(repo_rules, repo_perf).step(30)
    floors = [1200, 1230, 1350, 1660, 1360, 1290, 1460, 1500, 1560]
    for f in floors:
        for n in range(300):
            s.step(1, rss=f + (300 if n % 60 < 10 else 0))
    assert "R-01" not in s.ids()
    # 戻ったあとから、また 60 分以上増え続ければ出す
    for k in range(10):
        for n in range(300):
            s.step(1, rss=1600 + 40 * k + (300 if n % 60 < 10 else 0))
    assert s.ids().count("R-01") == 1


def test_r12_fps_drop_with_memory_growth_in_a_launch(repo_rules, repo_perf):
    # 起動し始め（計り始めて 5 分後の 5 分間）より終わり（最後の 15 分）のほうが、FPS が 10% 以上落ち、メモリの下限も
    # 200MB 以上増えていたら出す。メモリが増えただけ・FPS が落ちただけでは出さない（実害があるときだけ）。
    # 起動直後（タイトル画面）の低いメモリとは比べない
    def run(totals, fps_end=60.0):
        s = Sim(repo_rules, repo_perf).step(30)
        n_all = len(totals)
        for n, v in enumerate(totals):
            fps = fps_end if n >= n_all - 900 else 60.0
            s.step(1, fps=fps, total=v + (150 if n % 60 < 10 else 0))  # 読み込みで一時的に上がる
        return s, [i for i in s.engine.finish(s.t) if i.rule_id == "R-12"]

    title, play = [200] * 290, [330] * 600
    grow = title + play + [330 + n * 0.1 for n in range(2400)] + [580] * 1200
    s, issues = run(grow, fps_end=50.0)
    assert len(issues) == 1 and "60.0 → 50.0" in issues[0].detail and "330 → 580MB" in issues[0].detail
    rows = s.engine.perf.launch_memory()
    assert rows[0]["total"] == (330, 580) and rows[0]["fps"] == (60.0, 50.0)
    lines = s.engine.perf.report_lines()
    assert "### 起動ごとのメモリと FPS" in lines
    assert any(line.startswith("| #0 | ") and "60.0 → 50.0" in line and "330 → 580（+250）" in line for line in lines)
    # メモリが増えても、FPS が落ちていなければ出さない
    _, issues = run(grow)
    assert not issues
    # FPS が落ちても、メモリが増えていなければ出さない（R-05 などで見る）
    _, issues = run(title + [330] * 4200, fps_end=50.0)
    assert not issues
    # 途中で増えても、終わりの 15 分のうちに解放されて下がれば増えたとしない
    _, issues = run(title + play + [600] * 2700 + [340] * 300 + [600] * 300, fps_end=50.0)
    assert not issues
    # 短い起動（30 分未満）は見ない
    _, issues = run(title + [330] * 300 + [600] * 1100, fps_end=50.0)
    assert not issues


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


def test_r10_commands_need_repeats_and_a_bad_change_itself(repo_rules, repo_perf):
    # コマンドごとの前回比は、今回も前回も min_cmd_runs 回以上実行したものだけ。今回の上がり方そのものも cpu_add 以上
    # のときだけ（run 2026-11-07_1809: 1 回だけ実行した演出で、CPU の上がり方 -16 → +3 ポイントを Major にした）
    s = warm(repo_rules, repo_perf)
    perf = s.engine.perf
    perf.history = [{"run": "prev", "scenes": {}, "commands": {
        "@back once": {"n": 5, "cpu": -16.0},
        "@back few_past": {"n": 1, "cpu": 2.0},
        "@back small": {"n": 5, "cpu": -16.0},
        "@back heavy": {"n": 5, "cpu": 2.0},
    }}]
    perf.cmd_stats = {
        "@back once": {"cpu": [30.0]},  # 今回 1 回だけ
        "@back few_past": {"cpu": [30.0, 30.0, 30.0]},  # 前回 1 回だけ
        "@back small": {"cpu": [3.0, 3.0, 3.0]},  # 下がっていたものが少し上がっただけ
        "@back heavy": {"cpu": [30.0, 30.0, 30.0]},
    }
    issues = [i for i in s.engine.finish(s.t) if i.rule_id == "R-10"]
    assert len(issues) == 1
    assert "`@back heavy`" in issues[0].detail and "+2 → +30 ポイント" in issues[0].detail


def test_r08_ignores_commands_around_scene_and_load_boundaries(repo_rules, repo_perf):
    # scene の切り替え・ロードの負荷を、そのとき実行したコマンドの負荷にしない
    s = warm(repo_rules, repo_perf)
    for n in range(3):
        s.feed("game", "scene", name=f"Main{n}")
        s.feed("game", "command", name="hidePrinter", line_id=f"Main{n}:1.0")
        s.step(4, cpu=120)
        s.step(10)
    for n in range(3):
        s.feed("game", "command", name="EndMemory", line_id=f"Memory{n}:9.0")
        s.feed("game", "load", phase="start", slot="s")
        s.step(2, cpu=120)
        s.feed("game", "load", phase="end", slot="s")
        s.step(2, cpu=120)
        s.step(10)
    for n in range(3):  # scene の切り替えの直前に実行したコマンド
        s.feed("game", "command", name="wait", target="1.0", line_id=f"Main:{n}.0")
        s.step(1)
        s.feed("game", "scene", name="Next")
        s.step(3, cpu=120)
        s.step(10)
    assert "R-08" not in s.ids()


def test_r08_still_fires_away_from_boundaries(repo_rules, repo_perf):
    s = warm(repo_rules, repo_perf)
    s.feed("game", "scene", name="Next")
    s.step(10)  # 切り替えから十分に離れている
    for n in range(2):
        s.feed("game", "command", name="spawn", target="Rain", line_id=f"Next:{n}.0")
        s.step(4, cpu=120)
        s.step(10)
    assert "R-08" in s.ids()
