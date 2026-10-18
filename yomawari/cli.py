"""yomawari / ymw コマンドのエントリポイント（スケルトン）。

DESIGN.md §5 のコマンドと引数を定義している。中身は未実装で、受け取った引数をログに出して終わる。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.logging import RichHandler

log = logging.getLogger("yomawari")

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Unity製ノベルゲームのビルドを自動プレイし、問題をレポートにまとめる",
)
builds_app = typer.Typer(no_args_is_help=True, help="ビルドの登録と確認")
schedule_app = typer.Typer(no_args_is_help=True, help="launchd による定時実行の設定")
app.add_typer(builds_app, name="builds")
app.add_typer(schedule_app, name="schedule")


def setup_logging(verbose: bool) -> None:
    # ルートロガーには触らず yomawari 配下だけ設定する（依存ライブラリのログや pytest の caplog を壊さない）
    handler = RichHandler(console=Console(stderr=True), show_path=False, log_time_format="[%X]")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def _stub(command: str, **params: Any) -> None:
    log.info("%s: 未実装です（引数: %s）", command, params)


@app.callback()
def _root(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="yomawari.toml のパス（既定: $YOMAWARI_CONFIG → ./yomawari.toml）"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="デバッグログを出す"),
) -> None:
    setup_logging(verbose)
    ctx.obj = config
    log.debug("config=%s", config)


@app.command()
def run(
    build: str | None = typer.Option(None, "--build", "-b", help="登録済みビルドの名前"),
    build_path: Path | None = typer.Option(None, "--build-path", help="その場限りで使うビルドのパス（登録しない）"),
    hours: float | None = typer.Option(None, "--hours", help="実行時間（時間）。既定は yomawari.toml の run.hours"),
    mode: str = typer.Option("fuzz", "--mode", help="fuzz | aging | random"),
    seed: str | None = typer.Option(None, "--seed", help="乱数シード。既定は yomawari.toml の run.seed"),
    no_open: bool = typer.Option(False, "--no-open", help="終了後にレポートを開かない"),
) -> None:
    """ビルドを起動して自動プレイし、report.md を出力する。"""
    _stub("run", build=build, build_path=build_path, hours=hours, mode=mode, seed=seed, no_open=no_open)


@app.command()
def replay(issue_id: str = typer.Argument(..., help="ISSUE の ID")) -> None:
    """events.jsonl の操作列を再生して ISSUE を再現する。"""
    _stub("replay", issue_id=issue_id)


@app.command()
def minimize(issue_id: str = typer.Argument(..., help="ISSUE の ID")) -> None:
    """ISSUE を再現する操作列を最小化する。"""
    _stub("minimize", issue_id=issue_id)


@app.command()
def report(run_id: str = typer.Argument(..., help="ラン ID（runs/ 以下のディレクトリ名）")) -> None:
    """ランの report.md を作り直す。"""
    _stub("report", run_id=run_id)


@builds_app.command("list")
def builds_list() -> None:
    """登録済みビルドの一覧。"""
    _stub("builds list")


@builds_app.command("add")
def builds_add(
    name: str = typer.Argument(..., help="ビルドの名前"),
    path: str = typer.Argument(..., help="ビルドのパス（.app または実行ファイル）"),
) -> None:
    """ビルドを登録する。"""
    _stub("builds add", name=name, path=path)


@builds_app.command("edit")
def builds_edit(name: str = typer.Argument(..., help="ビルドの名前")) -> None:
    """登録済みビルドを対話で変更する。"""
    _stub("builds edit", name=name)


@builds_app.command("check")
def builds_check(name: str = typer.Argument(..., help="ビルドの名前")) -> None:
    """ビルドを検証し、起動して boot 行が出るか確かめる。"""
    _stub("builds check", name=name)


@app.command()
def doctor() -> None:
    """実行環境（arm64、権限、Ollama、登録ビルド）を確認する。"""
    _stub("doctor")


@schedule_app.command("install")
def schedule_install(at: str = typer.Option("01:00", "--at", help="起動時刻（HH:MM）")) -> None:
    """launchd に定時実行を登録する。"""
    _stub("schedule install", at=at)


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """launchd の定時実行を解除する。"""
    _stub("schedule uninstall")


@app.command()
def restore() -> None:
    """異常終了などで戻っていないセーブと PlayerPrefs を復元する。"""
    _stub("restore")


def main() -> None:
    app(prog_name="yomawari")
