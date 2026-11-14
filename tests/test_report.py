"""レポートの「まとめ」「やること」と項目ごとの節（post/report.py、post/digest.py）。"""

import json
from dataclasses import replace

from yomawari.events import Issue
from yomawari.post.digest import WHO_GAME, WHO_HARNESS, WHO_UNKNOWN, family
from yomawari.post.report import RunSummary, render


def _summary(**kw):
    base = dict(run_id="r", build_name="b", build_path="/b.app", started="s", ended="e", duration_sec=3600.0,
                mode="random", seed="1", launches=1, restarts=0, interrupted=False, rules_path="rules.yaml")
    return RunSummary(**{**base, **kw})


def _w(sec: float) -> str:
    return f"2026-01-01T00:{int(sec) // 60:02d}:{sec % 60:06.3f}+09:00"


def _issue(rule, severity, detail, sec, key=None, launch=0, **evidence):
    return Issue(rule, severity, f"{rule} の題", detail, key or f"{rule}:{detail}", launch, sec, _w(sec), evidence=evidence)


def _ev(sec, ev_kind, source="harness", launch=0, **data):
    return {"source": source, "kind": ev_kind, "data": data, "launch": launch, "t": sec, "wall": _w(sec)}


def _write(run_dir, events):
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                                          encoding="utf-8")


def _table(text):
    return text.split("## やること", 1)[1].split("\n### ", 1)[0]


def _rows(text):
    return [r for r in _table(text).splitlines() if r.startswith("| ") and r[2:3].isdigit()]


def _section(text, n):
    return text.split(f"\n### {n}. ", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]


def test_top_sections_come_first_and_items_are_ordered_by_severity(tmp_path):
    issues = [
        _issue("R-07", "minor", "重いフレーム 1", 0),
        _issue("R-07", "minor", "重いフレーム 2", 1),
        _issue("S-02", "critical", "本文が進まない", 3, state={"scene": "main/s1", "line_id": "main/s1:10"}),
        _issue("L-01", "major", "スクリプトを読み込めない\n  at Stack.Trace", 2),
    ]
    text = render(_summary(), issues, tmp_path)
    assert text.index("## まとめ") < text.index("## やること") < text.index("### 1. ") < text.index("## ランの情報")
    rows = _rows(text)
    assert len(rows) == 3  # 同じルールの R-07 は 1 項目
    assert "[S-02]" in rows[0] and "| Critical |" in rows[0] and f"| {WHO_UNKNOWN} |" in rows[0]
    assert "`main/s1:10`" in rows[0]
    assert "[L-01]" in rows[1] and f"| {WHO_GAME} |" in rows[1]
    assert "[R-07]" in rows[2] and "| 2 |" in rows[2]
    assert "- やること 3 件（ゲーム 1、未確定 2）" in text
    s1 = _section(text, 1)
    assert "- 誰が直す: 未確定\n" in s1 and "- 判定: [ ] 正しい検出　[ ] 誤検知" in s1
    assert "最初の検出のときの画面: scene `main/s1`、行 `main/s1:10`" in s1
    assert "根拠: ゲームが出したログ" in _section(text, 2)
    assert "Stack.Trace" not in _table(text)
    assert "| 深刻度別件数 | Critical 1 Major 1 Minor 2 Info 0 |" in text


def test_buttons_of_the_same_row_and_warnings_of_the_same_kind_become_one_item(tmp_path):
    issues = [_issue("S-05", "major", "反応しない", n, key=f"S-05:ListUI/Page0{n}") for n in range(1, 4)]
    issues.append(_issue("S-01", "critical", "反応がありません", 5, key="S-01:入力|click ListUI/Page05|ListUI"))
    for n, where in enumerate(("s:1", "s:2", "s:1")):
        issues.append(_issue("L-06", "minor", "Warning: Failed A", 6 + n, key=f"L-06:{where}|Failed A", kind="Failed A",
                             where=where))
    issues.append(_issue("L-06", "minor", "Warning: Failed B", 9, key="L-06:t:9|Failed B", kind="Failed B", where="t:9"))
    text = render(_summary(), issues, tmp_path)
    rows = _rows(text)
    assert len(rows) == 3
    assert "[S-01・S-05] `ListUI/Page` を押しても反応しない" in rows[0] and "| Critical |" in rows[0] and "| 4 |" in rows[0]
    assert "Failed A" in rows[1] and "`s:1` ほか 1 か所" in rows[1] and "| 3 |" in rows[1]
    assert "Failed B" in rows[2]
    assert family("click SaveUI/Slot (Clone)#3") == "SaveUI/Slot (Clone)" and family("advance") is None


def test_button_whose_screen_text_changed_is_marked_as_a_harness_false_positive(tmp_path):
    # 押すとページが切り替わる（ボタンの文字だけが変わる）のに、反応しないとした
    events = []
    for n, (before, after) in enumerate((("No.001", "No.011"), ("No.011", "No.021"), ("No.021", "No.021"))):
        t = 10.0 * n
        events += [_ev(t, "read_text", texts={"ListUI/Slot#1": before, "ListUI/Slot#2": before + "b", "ListUI/Clock": "1"}),
                   _ev(t + 1, "op", kind="click", label=f"click ListUI/Page0{n + 2}"),
                   _ev(t + 2, "op_result", responded=False),
                   _ev(t + 3, "read_text", texts={"ListUI/Slot#1": after, "ListUI/Slot#2": after + "b", "ListUI/Clock": "2"})]
    _write(tmp_path, events)
    issues = [_issue("S-05", "major", "反応しない", 2, key="S-05:ListUI/Page02")]
    text = render(_summary(), issues, tmp_path)
    assert f"| {WHO_HARNESS} |" in _rows(text)[0]
    sec = _section(text, 1)
    assert "同じ画面のボタンの文字が変わっていた: 2/3 回" in sec and "根拠: 押したあと" in sec
    # 文字が変わっていなければ未確定
    _write(tmp_path, [e for e in events if e["kind"] != "read_text"])
    assert f"| {WHO_UNKNOWN} |" in _rows(render(_summary(), issues, tmp_path))[0]


def test_timeline_around_the_first_detection(tmp_path):
    events = [_ev(0, "launch"), _ev(1, "line", "game", line_id="a:1", text="<b>はじめ</b>"),
              *[_ev(2 + n * 0.1, "op", kind="advance", label="advance") for n in range(3)],
              _ev(3, "op", kind="click", label="click ListUI/Close"), _ev(3.5, "op_result", responded=False),
              _ev(4, "menu", "game", name="ListUI"), _ev(4.1, "menu", "game", name="ListUI"),
              _ev(5, "command", "game", name="goto", target="b", line_id="a:2"),
              _ev(5.1, "command", "game", name="print", line_id="a:3"),
              _ev(6, "load", "game", slot="Save1", phase="start"),
              _ev(7, "line", "game", launch=1, line_id="z:1", text="別の起動"),
              _ev(100, "line", "game", line_id="a:9", text="ずっとあと")]
    _write(tmp_path, events)
    text = render(_summary(), [_issue("S-02", "critical", "進まない", 8)], tmp_path)
    sec = _section(text, 1)
    assert "- 経過（最初の検出（00:00:08、起動 #0）の前後）:" in sec
    # 経過の窓の外になりやすい、起動とロードは数字に書く
    assert "この起動を始めた時刻: 00:00:00、8 秒後に検出" in sec and "この起動で最後に始めたロード: Save1（00:00:06）" in sec
    timeline = sec.split("```text\n", 1)[1].split("\n```", 1)[0].splitlines()
    assert timeline == [
        "00:00:00 ゲームを起動した",
        "00:00:01 本文 `a:1`「はじめ」",
        "00:00:02 押した `advance` ×3",
        "00:00:03 押した `click ListUI/Close` → 反応なし",
        "00:00:04 画面が開いた `ListUI`",
        "00:00:05 コマンド @goto b（`a:2`）",
        "00:00:06 ロード Save1",
    ]
    # 長い経過は始めと終わりを残して省く
    _write(tmp_path, [_ev(n, "line", "game", line_id=f"a:{n}", text="x") for n in range(30)])
    sec = _section(render(_summary(report_cfg={"report_timeline_lines": 6}), [_issue("S-02", "critical", "x", 29)],
                          tmp_path), 1)
    assert "…（24 行省略）…" in sec and "`a:28`" in sec and "`a:29`" in sec


def test_long_stall_is_an_item_with_answers_and_short_ones_are_merged(tmp_path):
    q = {"flow": "質問", "question": "s:5"}
    events = [_ev(0, "launch"), _ev(1, "line", "game", line_id="s:5", text="どれ？")]
    for n in range(3):
        t = 100.0 + n * 400
        events += [_ev(t, "flow", flow="質問", step=1, note="一覧を開く"),
                   _ev(t + 1, "answer", **q, picked=["カード #2"], known=["カード #1"], follows=False,
                       deliberate=n == 0, locale_off=False),
                   _ev(t + 2, "judged", **q, picked=["カード #2"], right=False),
                   _ev(t + 3, "flow", flow=None, **{"from": {"flow": "質問", "step": 3}})]
    events += [_ev(2000, "line", "game", line_id="s:6", text="次"), _ev(2400, "line", "game", line_id="s:7", text="次の次"),
               _ev(2800, "heartbeat")]
    _write(tmp_path, events)
    text = render(_summary(report_cfg={"stall_report_sec": 300, "report_stall_item_sec": 1000}), [], tmp_path)
    rows = _rows(text)
    assert "`s:5` から 33 分進めなかった" in rows[0] and "| 要確認 |" in rows[0]
    assert "短く進めなかった区間 2 件" in rows[1]
    sec = _section(text, 1)
    assert "答えた: 3 回（カード #2 ×3）" in sec
    assert "プロファイルの正解: 分かっていた（カード #1）。正解に沿って答えた 0 回、わざと外れを答えるつもりだった 1 回" in sec
    assert "答えの判定: 正解 0 回、外れ（同じ質問に戻った） 3 回" in sec
    assert "流れの手順に入った回数: 「質問」手順 1（一覧を開く） 3 回" in sec
    assert "答えた: カード #2" in sec  # 経過にも出る
    assert "- いちばん長く進めなかった区間: 33 分（ランの 56%）、`s:5` から" in text
    # 操作しないモードでは、進まないのが正常
    assert "進めなかった" not in _table(render(_summary(mode="aging", report_cfg={"stall_report_sec": 300}), [], tmp_path))


def test_error_log_just_before_is_shown_as_the_clue(tmp_path):
    log = _issue("L-01", "major", "スクリプトを読み込めない\n  at Stack.Trace", 0)
    hit = _issue("S-02", "critical", "本文が進まない", 90)

    def clue(issues, **kw):
        return "直前のエラーログ: スクリプトを読み込めない" in _section(render(_summary(**kw), issues, tmp_path), 1)

    assert clue([log, hit])
    far = _issue("S-02", "critical", "本文が進まない", 121)
    assert not clue([log, far])
    assert not clue([log, _issue("S-02", "critical", "本文が進まない", 90, launch=1)])
    assert clue([log, far], suspect_error_window_sec=180)  # 窓は設定で変えられる


def test_notes_and_silent_buttons_are_gathered(tmp_path):
    notes = ["操作: 10 回", "巻き戻せなかった。戻るはずの場面なら不具合の可能性がある", "一覧で押せるものが見つからなかった（進めない可能性がある）"]
    text = render(_summary(harness_notes=notes, silent_buttons={"ListUI/Slot#1": 2, "ListUI/Slot#2": 1}), [], tmp_path)
    rows = _rows(text)
    assert "ハーネスが不具合の可能性を書いた記録 2 件" in rows[0] and "一度も反応しなかったボタン 2 個" in rows[1]
    assert "操作: 10 回" not in _section(text, 1) and "巻き戻せなかった" in _section(text, 1)
    assert "`ListUI/Slot`: 押した 3 回" in _section(text, 2)
    assert "| `ListUI/Slot#1` | 2 |" in text  # 下の節にはボタンごとに残す


def test_no_items_and_table_safe_text(tmp_path):
    text = render(_summary(), [], tmp_path)
    assert "- やること: なし" in text
    issues = [_issue("V-07", "minor", "a|b", 0), _issue("V-08", "minor", "x" * 300, 0)]
    issues[0].title = "題|縦線"
    text = render(_summary(), issues, tmp_path)
    assert "題\\|縦線" in _table(text)
    assert "x" * 201 not in text.split("## ランの情報")[0]


def test_summary_says_how_the_run_was_stopped(tmp_path):
    summary = replace(_summary(), interrupted=True, stop_reason="手動で中断した（Ctrl+C）", planned_hours=8.0)
    text = render(summary, [], tmp_path)
    assert "| 終わり方 | 手動で中断した（Ctrl+C）。予定の 8 時間より前に止めた" in text
    assert "- 1.0 時間、起動 1 回（再起動 0 回）。手動で中断した（Ctrl+C）" in text
    assert "（中断）" in text
    assert "終わり方" not in render(_summary(), [], tmp_path)


def test_place_uses_command_line_after_last_text(tmp_path):
    # 本文のない演出の途中で検出したら、場所はスクリプトが実際にいるコマンドの行。最後の本文の行も添える
    state = {"scene": "main/b", "line_id": "main/a:10.0", "command_line_id": "main/b:19.0", "menus": [], "top_modal": None}
    text = render(_summary(), [_issue("S-02", "critical", "進まない", 0, state=state)], tmp_path)
    assert "`main/b:19.0`" in _rows(text)[0] and "main/a:10.0" not in _rows(text)[0]
    assert "行 `main/b:19.0`（最後の本文は `main/a:10.0`。" in text
    state = {**state, "command_line_id": None}
    text = render(_summary(), [_issue("S-02", "critical", "進まない", 0, state=state)], tmp_path)
    assert "`main/a:10.0`" in _rows(text)[0] and "行 `main/a:10.0`、" in text
