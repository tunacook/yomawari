import logging

import pytest
from typer.testing import CliRunner

from yomawari.cli import app

# 実装済みのコマンド（run / builds / doctor / restore）は test_e2e.py などで確かめる
COMMANDS = [
    (["replay", "C-01-0001"], "replay"),
    (["minimize", "C-01-0001"], "minimize"),
    (["report", "2026-10-18_0100_mac_dev"], "report"),
    (["schedule", "install", "--at", "02:30"], "schedule install"),
    (["schedule", "uninstall"], "schedule uninstall"),
]


@pytest.mark.parametrize("args,command", COMMANDS)
def test_every_command_logs_and_exits_0(args, command, caplog):
    caplog.set_level(logging.INFO, logger="yomawari")
    res = CliRunner().invoke(app, ["--config", "/tmp/y.toml", *args])
    assert res.exit_code == 0, res.output
    msgs = [r.getMessage() for r in caplog.records if r.name == "yomawari"]
    assert any(m.startswith(f"{command}: 未実装です") for m in msgs), msgs


def test_args_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="yomawari")
    CliRunner().invoke(app, ["schedule", "install", "--at", "02:30"])
    assert "'at': '02:30'" in caplog.records[-1].getMessage()


def test_verbose_enables_debug(caplog):
    caplog.set_level(logging.DEBUG, logger="yomawari")
    res = CliRunner().invoke(app, ["-v", "--config", "/tmp/y.toml", "report", "r1"])
    assert res.exit_code == 0
    assert any(r.levelno == logging.DEBUG and "config=" in r.getMessage() for r in caplog.records)


def test_no_args_shows_help():
    res = CliRunner().invoke(app, [])
    assert "run" in res.output and "builds" in res.output


def test_missing_argument_is_usage_error():
    res = CliRunner().invoke(app, ["replay"])
    assert res.exit_code == 2
