"""rules.yaml の値そのままで、時刻を与えて検出器を確かめる。"""

from yomawari.detect.engine import Engine, RuleError, load_rules
from yomawari.events import Event

import pytest


def ev(source, kind, t, **data):
    return Event(source, kind, data, launch=0, t=t, wall=f"w{t}")


def run(engine, events, ticks=()):
    issues = []
    timeline = sorted([(e.t, 0, e) for e in events] + [(t, 1, t) for t in ticks], key=lambda x: (x[0], x[1]))
    for _, kind, item in timeline:
        issues += engine.feed(item) if kind == 0 else engine.tick(item, 0)
    return issues


def test_rules_have_expected_params(repo_rules):
    assert repo_rules["C-03"].param("timeout_sec") == 30
    assert repo_rules["H-01"].param("timeout_sec") == 10
    assert {r.severity for r in repo_rules.values()} <= {"critical", "major"}


def test_c01_unexpected_exit(repo_rules):
    e = Engine(repo_rules)
    issues = run(e, [ev("harness", "launch", 0), ev("proc", "proc_exit", 5, code=0, by_harness=False)])
    assert [(i.rule_id, i.severity, i.action) for i in issues] == [("C-01", "critical", "restart")]


def test_c01_harness_stop_is_ok_but_nonzero_is_not(repo_rules):
    ok = run(Engine(repo_rules), [ev("proc", "proc_exit", 1, code=-15, by_harness=True)])
    assert ok == []
    bad = run(Engine(repo_rules), [ev("proc", "proc_exit", 1, code=134, by_harness=True)])
    assert [i.rule_id for i in bad] == ["C-01"]


def test_c02_crash_dump(repo_rules):
    issues = run(Engine(repo_rules), [ev("crash", "crash_dump", 1, path="/x/Rachel-2026.ips")])
    assert [(i.rule_id, i.severity) for i in issues] == [("C-02", "critical")]


def test_c03_boot_timeout_boundary(repo_rules):
    launch = ev("harness", "launch", 100.0)
    assert [i for i in run(Engine(repo_rules), [launch], ticks=[129.9]) if i.rule_id == "C-03"] == []
    issues = run(Engine(repo_rules), [launch], ticks=[129.9, 130.0, 135.0])
    assert [i.rule_id for i in issues] == ["C-03"]  # 1回だけ
    booted = run(Engine(repo_rules), [launch, ev("game", "boot", 105.0)], ticks=[140.0])
    assert [i for i in booted if i.rule_id == "C-03"] == []


def test_h01_heartbeat_gap(repo_rules):
    base = [ev("harness", "launch", 0), ev("game", "boot", 1), ev("proc", "proc_stats", 2, cpu=97.0)]
    hbs = [ev("game", "heartbeat", t) for t in (2, 3, 4)]
    assert run(Engine(repo_rules), base + hbs, ticks=[13.9]) == []
    issues = run(Engine(repo_rules), base + hbs, ticks=[13.9, 14.0, 20.0])
    assert [i.rule_id for i in issues] == ["H-01"]
    assert "97%" in issues[0].detail


def test_h01_not_before_boot_nor_after_exit(repo_rules):
    # boot 前の沈黙は C-03 の担当
    issues = run(Engine(repo_rules), [ev("harness", "launch", 0)], ticks=[15])
    assert "H-01" not in [i.rule_id for i in issues]
    exited = [ev("harness", "launch", 0), ev("game", "boot", 1), ev("proc", "proc_exit", 2, code=-15, by_harness=True)]
    assert run(Engine(repo_rules), exited, ticks=[20]) == []


def test_l01_log_error_event(repo_rules):
    e = Engine(repo_rules)
    exc = ev("game", "log_error", 1, type="Exception", message="NullReferenceException: Object reference not set",
             stack="A.B:C()\nD.E:F()\n")
    err = ev("game", "log_error", 2, type="Error", message="Save slot 1 not found", stack="Game.Save:Load(Int32)\n")
    asrt = ev("game", "log_error", 3, type="Assert", message="Assertion failed", stack="")
    issues = run(e, [exc, err, asrt])
    assert [(i.rule_id, i.severity) for i in issues] == [("L-01", "major")] * 3
    assert len({i.key for i in issues}) == 3
    assert issues[0].detail.startswith("Exception: NullReferenceException")
    assert "A.B:C()" in issues[0].evidence["log"]


def test_l01_ignores_player_log_text(repo_rules):
    """Player.log の本文はスタックが付いていても例外の文面でも L-01 にしない（判定は log_error だけ）。"""
    e = Engine(repo_rules)
    with_stack = ev("log", "unity_log", 1, message="[Panel] SetVisibility(False)",
                    stack=["UnityEngine.DebugLogHandler:Internal_Log(LogType, LogOption, String, Object)"], has_stack=True)
    exc_text = ev("log", "unity_log", 2, message="NullReferenceException: x", stack=["A.B:C()"], has_stack=True)
    assert run(e, [with_stack, exc_text]) == []


def test_l01_type_filter_and_ignore(repo_rules):
    rules = repo_rules
    rules["L-01"].params["ignore_patterns"] = ["^Known noisy"]
    e = Engine(rules)
    assert run(e, [ev("game", "log_error", 1, type="Warning", message="w", stack="")]) == []
    assert run(e, [ev("game", "log_error", 1, type="Error", message="Known noisy thing", stack="")]) == []


def test_load_rules_validation(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("rules:\n  - id: X\n    severity: fatal\n    title: t\n")
    with pytest.raises(RuleError):
        load_rules(p)
    p.write_text("rules:\n  - id: C-03\n    severity: critical\n    title: t\n")
    with pytest.raises(RuleError):
        Engine(load_rules(p))  # timeout_sec がない
