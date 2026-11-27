"""再起動の前に、画面の写しと状態をランのフォルダに残す（snapshot.py、watchdog.py、レポートのリンク）。"""

import json
import struct
import zlib
from pathlib import Path

from yomawari import snapshot
from yomawari.events import Event, Issue
from yomawari.post.report import RunSummary, render
from yomawari.sensors.screen import Shot, to_png
from yomawari.state import GameState
from yomawari.watchdog import Harness


def _png_pixels(png: bytes) -> tuple[int, int, bytes]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        n, kind = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    return w, h, zlib.decompress(idat)


def test_png_from_bgra_capture():
    # 2x1、行の末尾に詰め物のあるキャプチャ（bytes_per_row が幅より大きい）
    shot = Shot(2, 1, 12, bytes([1, 2, 3, 255, 4, 5, 6, 128, 9, 9, 9, 9]))
    w, h, raw = _png_pixels(to_png(shot))
    assert (w, h) == (2, 1)
    assert raw == bytes([0, 3, 2, 1, 255, 6, 5, 4, 128])  # フィルタなし、RGBA


def _state() -> GameState:
    st = GameState()
    st.feed(Event("game", "menu", {"name": "ScreenUI", "modal": False,
                                   "buttons": [{"name": "Next", "rect": [0.8, 0.8, 0.9, 0.9], "type": "button"}]}, 0))
    return st


def test_snapshot_writes_state_and_screen(tmp_path: Path):
    shot = Shot(1, 1, 4, bytes([0, 0, 0, 255]))
    paths = snapshot.take(tmp_path, "000101_S-06_0", {"rule_id": "S-06"}, _state(), shot)
    assert paths == {"png": "snapshots/000101_S-06_0.png", "json": "snapshots/000101_S-06_0.json"}
    body = json.loads((tmp_path / paths["json"]).read_text())
    assert body["rule_id"] == "S-06"
    assert body["state"]["menus"][0]["name"] == "ScreenUI"
    assert body["state"]["menus"][0]["buttons"][0]["name"] == "Next"
    assert (tmp_path / paths["png"]).read_bytes()[:4] == b"\x89PNG"


def test_restart_issue_leaves_snapshot_before_restart(tmp_path: Path, fake_platform):
    # 再起動すると画面は消えるので、検出した時点で状況を残し、検出の証拠とレポートから辿れるようにする
    h = Harness(None, Path("x"), fake_platform, {}, Path("rules.yaml"), tmp_path, {}, {})
    h._run_dir = tmp_path
    h._bus = type("Bus", (), {"publish": lambda self, ev: None})()
    h.state = _state()
    h._on_event(Event("harness", "op", {"label": "click ScreenUI/Next"}, 0, t=1.0, wall="2026-01-01T00:01:01+09:00"))
    i = Issue("S-06", "critical", "画面のどの操作にも反応しない", "反応しない", "S-06:x", 0, 2.0,
              "2026-01-01T00:01:02+09:00", action="restart", evidence={"recent": ["click ScreenUI/Next"]})
    h._handle_issues([i])
    snap = i.evidence["snapshot"]
    assert snap == {"json": "snapshots/000102_S-06_0.json"}  # ゲームを操作していないので画面は撮らない
    body = json.loads((tmp_path / snap["json"]).read_text())
    assert [e["kind"] for e in body["recent_events"]] == ["op"]
    assert body["recent_ops"] == ["click ScreenUI/Next"]
    summary = RunSummary(run_id="r", build_name="b", build_path="/b.app", started="s", ended="e", duration_sec=60.0,
                         mode="random", seed="1", launches=1, restarts=1, interrupted=False, rules_path="rules.yaml")
    i.evidence["snapshot"] = {"png": "snapshots/a.png", "json": "snapshots/a.json"}
    text = render(summary, [i], tmp_path)
    assert "- 最初の検出のときの状況（再起動の前に残した）: [画面](snapshots/a.png)・[状態と直前のイベント](snapshots/a.json)" in text


def test_v07_leaves_the_screen_measured_before_the_press(tmp_path: Path, fake_platform):
    # V-07 は押したあとに出る。押すと別の画面に変わっていて、押せないように見えたボタンが写らないので、
    # 押す前に見た目を測ったときの画面を残す。再起動はしない
    from yomawari.detect.engine import load_rules

    h = Harness(None, Path("x"), fake_platform, load_rules(Path("rules.yaml")), Path("rules.yaml"), tmp_path, {}, {})
    h._run_dir = tmp_path
    h._bus = type("Bus", (), {"publish": lambda self, ev: None})()
    h.state = _state()
    before = Shot(1, 1, 4, bytes([10, 20, 30, 255]))
    h._session_fuzz = type("F", (), {"launch": 0, "look_shot": before, "burst": None})()
    i = Issue("V-07", "major", "暗いボタンで別の画面に進める", "押せた", "V-07:x", 0, 2.0, "2026-01-01T00:01:02+09:00")
    h._handle_issues([i])
    snap = i.evidence["snapshot"]
    assert snap["png"] == "snapshots/000102_V-07_0.png" and i.evidence["snapshot_before_press"]
    assert (tmp_path / snap["png"]).read_bytes() == to_png(before)
    summary = RunSummary(run_id="r", build_name="b", build_path="/b.app", started="s", ended="e", duration_sec=60.0,
                         mode="random", seed="1", launches=1, restarts=0, interrupted=False, rules_path="rules.yaml")
    text = render(summary, [i], tmp_path)
    assert "- 最初の検出のときの状況（押す前に見た目を測ったときの画面）: [画面](snapshots/000102_V-07_0.png)" in text
