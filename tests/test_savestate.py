from pathlib import Path

import pytest

from yomawari.savestate import SaveState, SaveTarget


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_backup_and_restore(tmp_path, fake_platform):
    save = tmp_path / "save"
    write(save / "slot1.dat", "original")
    fake_platform.prefs["unity.C.P"] = b"orig-prefs"
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    st = SaveState(runs, fake_platform)
    st.backup(run_dir, SaveTarget(save, "unity.C.P"), None)
    assert st.pending() is not None

    # ラン中にゲームが書き換えた
    write(save / "slot1.dat", "changed")
    write(save / "slot2.dat", "new")
    fake_platform.prefs["unity.C.P"] = b"changed-prefs"

    st.restore()
    assert (save / "slot1.dat").read_text() == "original"
    assert not (save / "slot2.dat").exists()
    assert fake_platform.prefs["unity.C.P"] == b"orig-prefs"
    assert st.pending() is None
    # ラン後の状態は消さずに残っている
    after = next((run_dir / "backup").glob("after_run*"))
    assert (after / "save" / "slot2.dat").read_text() == "new"
    assert (after / "prefs.plist").read_bytes() == b"changed-prefs"


def test_absent_save_and_prefs_are_removed_on_restore(tmp_path, fake_platform):
    save = tmp_path / "save"
    st = SaveState(tmp_path / "runs", fake_platform)
    st.backup(tmp_path / "runs" / "r1", SaveTarget(save, "unity.C.P"), None)
    write(save / "slot1.dat", "made by game")
    fake_platform.prefs["unity.C.P"] = b"made by game"
    st.restore()
    assert not save.exists()
    assert "unity.C.P" not in fake_platform.prefs


def test_initial_save_is_placed(tmp_path, fake_platform):
    save = tmp_path / "save"
    write(save / "slot1.dat", "original")
    init = tmp_path / "init"
    write(init / "slot9.dat", "fixture")
    st = SaveState(tmp_path / "runs", fake_platform)
    st.backup(tmp_path / "runs" / "r1", SaveTarget(save, None), init)
    assert sorted(p.name for p in save.iterdir()) == ["slot9.dat"]
    st.restore()
    assert sorted(p.name for p in save.iterdir()) == ["slot1.dat"]


def test_refuses_second_backup_while_pending(tmp_path, fake_platform):
    st = SaveState(tmp_path / "runs", fake_platform)
    st.backup(tmp_path / "runs" / "r1", SaveTarget(tmp_path / "save", None), None)
    with pytest.raises(RuntimeError):
        st.backup(tmp_path / "runs" / "r2", SaveTarget(tmp_path / "save", None), None)


def test_restore_without_pending_is_noop(tmp_path, fake_platform):
    assert SaveState(tmp_path / "runs", fake_platform).restore() == []
