"""レポートの「不具合の疑い」（post/report.py）。"""

from dataclasses import replace

from yomawari.events import Issue
from yomawari.post.report import (
    DOUBT_BUTTON,
    DOUBT_ERROR,
    DOUBT_LOG,
    DOUBT_NOTE,
    DOUBT_PERF,
    DOUBT_SILENT,
    DOUBT_STALL,
    DOUBT_UNKNOWN,
    DOUBT_WARNING,
    RunSummary,
    render,
)


def _summary(**kw):
    return RunSummary(run_id="r", build_name="b", build_path="/b.app", started="s", ended="e", duration_sec=1.0,
                      mode="random", seed="1", launches=1, restarts=0, interrupted=False, rules_path="rules.yaml", **kw)


def _issue(rule, severity, detail, wall, key=None, launch=0, state=None):
    return Issue(rule, severity, f"{rule} の題", detail, key or f"{rule}:{detail}", launch, 0.0, wall,
                 evidence={"state": state} if state else {})


def _section(text):
    return text.split("## 不具合の疑い", 1)[1].split("\n## ", 1)[0]


def test_suspects_come_right_after_the_summary_in_order_of_severity(tmp_path):
    issues = [
        _issue("R-07", "minor", "重いフレーム 1", "2026-01-01T00:00:00+09:00"),
        _issue("R-07", "minor", "重いフレーム 2", "2026-01-01T00:00:01+09:00"),
        _issue("S-05", "major", "ボタンが反応しない", "2026-01-01T00:00:02+09:00",
               state={"scene": "main/s1", "line_id": "main/s1:10"}),
        _issue("S-02", "critical", "本文が進まない", "2026-01-01T00:00:03+09:00"),
    ]
    text = render(_summary(), issues, tmp_path)
    assert text.index("## サマリ") < text.index("## 不具合の疑い") < text.index("## Critical")
    sec = _section(text)
    assert sec.index("[S-02]") < sec.index("[S-05]") < sec.index("[R-07]")
    # 同じ重さなら、性能の検出はあと
    order = _section(render(_summary(), [_issue("R-08", "major", "重い", "w"), _issue("L-01", "major", "エラー", "w")], tmp_path))
    assert order.index("[L-01]") < order.index("[R-08]")
    assert f"| Major | [S-05] S-05 の題 | `main/s1:10` | 1 | {DOUBT_BUTTON} | ボタンが反応しない |" in sec
    assert f"| 1 | {DOUBT_UNKNOWN} | 本文が進まない |" in sec
    assert f"| 2 | {DOUBT_PERF} |" in sec
    # Minor はルールごとに1行
    assert sec.count("[R-07]") == 1 and "| 2 か所 | 2 |" in sec


def test_error_log_just_before_is_shown_as_the_clue(tmp_path):
    err = "スクリプトを読み込めない\n  at Stack.Trace"
    log = _issue("L-01", "major", err, "2026-01-01T00:00:00+09:00")
    hit = _issue("S-02", "critical", "本文が進まない", "2026-01-01T00:01:30+09:00")
    sec = _section(render(_summary(), [log, hit], tmp_path))
    assert f"| Critical | [S-02] S-02 の題 | - | 1 | {DOUBT_ERROR} | 直前のエラーログ: スクリプトを読み込めない |" in sec
    assert f"| {DOUBT_LOG} |" in sec
    assert "Stack.Trace" not in sec
    # 窓の外、別の起動のエラーログは手がかりにしない
    far = _issue("S-02", "critical", "本文が進まない", "2026-01-01T00:02:01+09:00")
    assert "直前のエラーログ" not in _section(render(_summary(), [log, far], tmp_path))
    other = _issue("S-02", "critical", "本文が進まない", "2026-01-01T00:01:30+09:00", launch=1)
    assert "直前のエラーログ" not in _section(render(_summary(), [log, other], tmp_path))
    # 窓は設定で変えられる
    assert "直前のエラーログ" in _section(render(_summary(suspect_error_window_sec=180), [log, far], tmp_path))


def test_stalls_notes_and_silent_buttons_are_gathered(tmp_path):
    notes = ["操作: 10 回", "巻き戻せなかった。戻るはずの場面なら不具合の可能性がある", "一覧で押せるものが見つからなかった（進めない可能性がある）",
             "流れの `main/s1:5` で、外れのはずの答え（はい）のあと、同じ質問に戻らずに先へ進んだ（プロファイルの正解が古いか、ゲームの判定の不具合の可能性がある）"]
    sec = _section(render(_summary(stall_rows=[("`main/s1:1`", "9 分。ランが終わるまで進まなかった")], harness_notes=notes,
                                   silent_buttons={"ListUI/Slot": 2}), [], tmp_path))
    assert f"| 要確認 | 進めなかった区間 | `main/s1:1` | 1 | {DOUBT_STALL} | 9 分。ランが終わるまで進まなかった |" in sec
    assert sec.count(DOUBT_NOTE) == 3 and DOUBT_SILENT in sec
    assert "巻き戻せなかった" in sec and "押せるものが見つからなかった" in sec
    assert "外れのはずの答え（はい）のあと" in sec and "ゲームの判定の不具合の可能性がある" in sec
    assert "操作: 10 回" not in sec
    assert "`ListUI/Slot`" in sec


def test_no_suspects_and_table_safe_text(tmp_path):
    assert "なし" in _section(render(_summary(), [], tmp_path))
    long = "x" * 300 + "|y"
    sec = _section(render(_summary(), [_issue("L-01", "major", "a|b", "w"), _issue("L-01", "major", long, "w")], tmp_path))
    assert "a\\|b" in sec and "…" in sec and "x" * 121 not in sec


def test_summary_says_how_the_run_was_stopped(tmp_path):
    summary = replace(_summary(), interrupted=True, stop_reason="手動で中断した（Ctrl+C）", planned_hours=8.0)
    text = render(summary, [], tmp_path)
    assert "| 終わり方 | 手動で中断した（Ctrl+C）。予定の 8 時間より前に止めた" in text
    assert "（中断）" in text


def test_summary_has_no_stop_row_when_the_run_finished(tmp_path):
    assert "終わり方" not in render(_summary(), [], tmp_path)


def _warning(kind, where, wall="w"):
    i = _issue("L-06", "minor", f"Warning: {kind}", wall, key=f"L-06:{where}|{kind}")
    i.evidence.update(kind=kind, where=where)
    return i


def test_warnings_get_one_row_per_kind_with_count_and_place(tmp_path):
    many = [_warning("Failed A", f"s:{n}") for n in range(3)] + [_warning("Failed A", "s:0")]
    sec = _section(render(_summary(), many + [_warning("Failed B", "t:9")], tmp_path))
    rows = [r for r in sec.splitlines() if "[L-06]" in r]
    assert len(rows) == 2
    assert "`s:0` ほか 2 か所 | 4 |" in rows[0] and "Failed A" in rows[0]  # 回数の多い順
    assert "`t:9` | 1 |" in rows[1] and "Failed B" in rows[1]
    assert DOUBT_WARNING in rows[0]


def test_other_minor_rules_still_get_one_row_per_rule(tmp_path):
    sec = _section(render(_summary(), [_issue("V-07", "minor", "a", "w"), _issue("V-07", "minor", "b", "w")], tmp_path))
    assert len([r for r in sec.splitlines() if "[V-07]" in r]) == 1


def test_place_uses_command_line_after_last_text(tmp_path):
    # 本文のない演出の途中で検出したら、場所はスクリプトが実際にいるコマンドの行。最後の本文の行も添える
    state = {"scene": "main/b", "line_id": "main/a:10.0", "command_line_id": "main/b:19.0", "menus": [],
             "top_modal": None}
    text = render(_summary(), [_issue("S-01", "critical", "反応がありません", "w", state=state)], tmp_path)
    row = next(r for r in _section(text).splitlines() if "[S-01]" in r)
    assert "`main/b:19.0`" in row and "main/a:10.0" not in row
    assert "行 `main/b:19.0`（最後の本文は `main/a:10.0`。" in text
    # コマンドが進んでいなければ、今までどおり最後の本文の行
    state = {**state, "command_line_id": None}
    text = render(_summary(), [_issue("S-01", "critical", "反応がありません", "w", state=state)], tmp_path)
    assert "`main/a:10.0`" in next(r for r in _section(text).splitlines() if "[S-01]" in r)
    assert "行 `main/a:10.0`、" in text
