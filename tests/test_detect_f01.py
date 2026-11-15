"""F-01（セーブしたのにファイルが更新されない）: セーブの場所を見る sensors/savefile.py と、検出器。"""

import asyncio
import os

from yomawari.detect.engine import Engine
from yomawari.events import Event
from yomawari.sensors.savefile import SaveFileWatch, newest_file


def save(phase, slot="Slot01", t=0.0):
    return Event("game", "save", {"slot": slot, "type": "Normal", "phase": phase}, launch=0, t=t, wall=f"w{t}")


def _touch(p, mtime):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    os.utime(p, (mtime, mtime))


def test_newest_file(tmp_path):
    assert newest_file(tmp_path / "missing") is None
    _touch(tmp_path / "a.sav", 100)
    _touch(tmp_path / "sub" / "b.sav", 200)
    assert newest_file(tmp_path) == (tmp_path / "sub" / "b.sav", 200)


def test_save_updated_and_not_updated(tmp_path):
    clock = [1000.0]
    watch = SaveFileWatch(tmp_path, lambda e: None, check_after=0, slack=3, clock=lambda: clock[0])
    _touch(tmp_path / "Saves" / "old.sav", 900)
    _touch(tmp_path / "Saves" / "slot01.sav", 1001)  # セーブを始めたあとに書かれた
    ev = watch.check("Slot01", 1000.0, 0)
    assert ev.kind == "save_file" and ev.data["updated"] and ev.data["newest"] == os.path.join("Saves", "slot01.sav")
    ev = watch.check("Slot02", 1010.0, 0)  # 始めてから何も書かれていない
    assert not ev.data["updated"] and ev.data["newest_before_sec"] == 9.0
    # ログを受け取る遅れ（slack）のぶんは、始める少し前のファイルも数える
    assert watch.check("Slot01", 1003.5, 0).data["updated"]


def test_watch_checks_after_save_end(tmp_path):
    published = []

    async def main():
        watch = SaveFileWatch(tmp_path, published.append, check_after=0.01, slack=0)
        watch.on_event(save("start"))
        await asyncio.sleep(0.01)
        _touch(tmp_path / "slot01.sav", os.path.getmtime(tmp_path) + 100)
        watch.on_event(save("end"))
        await asyncio.sleep(0.05)
        watch.close()

    asyncio.run(main())
    assert [(e.data["slot"], e.data["updated"]) for e in published] == [("Slot01", True)]


def test_f01_issue(repo_rules):
    e = Engine({"F-01": repo_rules["F-01"]})
    ok = Event("harness", "save_file", {"slot": "Slot01", "updated": True, "save_dir": "/s", "newest": "a"}, 0, 1, "w1")
    bad = Event("harness", "save_file", {"slot": "Slot02", "updated": False, "save_dir": "/s", "newest": "a",
                                         "newest_before_sec": 9.0}, 0, 2, "w2")
    issues = e.feed(ok) + e.feed(bad)
    assert [(i.rule_id, i.severity, i.evidence["slot"]) for i in issues] == [("F-01", "critical", "Slot02")]
    assert "セーブを始める 9.0 秒前に更新" in issues[0].detail
