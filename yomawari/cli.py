"""yomawari / ymw コマンドのエントリポイント。

フェーズ1で run / builds / doctor / restore を実装した。それ以外はスケルトンのまま、引数をログに出して終わる。
"""

from __future__ import annotations

import asyncio
import logging
import platform as pyplatform
import shlex
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import launcher
from .config import BuildConfig, Config, ConfigError, find_config_path, normalize_path_input, stdin_is_tty
from .detect.engine import Rule, RuleError, load_rules
from .platform import Platform, get_platform
from .savestate import SaveState, resolve_target
from .watchdog import Harness, RunOptions

EXIT_OK, EXIT_CRITICAL, EXIT_HARNESS_ERROR = 0, 1, 2

log = logging.getLogger("yomawari")
console = Console()
err = Console(stderr=True)

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


_LEVEL_STYLE = {"ok": "[green]OK[/]", "warn": "[yellow]注意[/]", "error": "[red]NG[/]"}


@app.callback()
def _root(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="yomawari.toml のパス（既定: $YOMAWARI_CONFIG → ./yomawari.toml）"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="デバッグログを出す"),
) -> None:
    setup_logging(verbose)
    ctx.obj = find_config_path(config)
    log.debug("config=%s", ctx.obj)


def _config(ctx: typer.Context) -> Config:
    return Config(ctx.obj)


def _print_msgs(msgs: list[tuple[str, str]]) -> None:
    for level, text in msgs:
        console.print(f"  {_LEVEL_STYLE[level]} {text}")


def _boot_check(cfg: Config, build: BuildConfig, exe: Path, rules: dict[str, Rule]) -> bool:
    timeout = float(rules["C-03"].param("timeout_sec"))
    work = cfg.runs_dir / "_check"
    work.mkdir(parents=True, exist_ok=True)
    log = work / f"{build.name}.player.log"
    log.unlink(missing_ok=True)
    console.print(f"  起動して boot 行を待ちます（最大 {timeout:g} 秒）…")
    boot = asyncio.run(
        launcher.boot_check(exe, build, log, timeout, float(cfg.run["quit_timeout_sec"]))
    )
    if boot:
        info = ", ".join(f"{k}={boot[k]}" for k in ("version", "build_type", "build_time") if k in boot)
        console.print(f"  {_LEVEL_STYLE['ok']} boot を受信しました（{info}）")
        return True
    console.print(
        f"  {_LEVEL_STYLE['warn']} boot 行が出ませんでした。YomawariLog の組み込みと -yomawari の扱いを確認してください"
        f"（ログ: {log}）"
    )
    return False


def _require_tty(what: str) -> None:
    if not stdin_is_tty():
        err.print(f"[red]{what}には対話入力が必要ですが、標準入力が端末ではありません[/]")
        raise typer.Exit(EXIT_HARNESS_ERROR)


def register_interactive(cfg: Config, plat: Platform, rules: dict[str, Rule]) -> BuildConfig:
    """ビルド未登録時の対話登録。パスを尋ね、検証し、名前を付けて yomawari.toml に保存する。"""
    _require_tty("ビルドの登録")
    console.print("[bold]ビルドが登録されていません。登録します。[/]")
    while True:
        text = Prompt.ask("ビルドのパス（Finder から .app をドラッグ&ドロップできます）")
        path = normalize_path_input(text)
        exe, msgs = launcher.validate_build(BuildConfig("_", path), plat)
        _print_msgs(msgs)
        if exe is not None:
            break
    default = path.stem.lower().replace(" ", "_")
    name = Prompt.ask("このビルドの名前", default=default)
    build = BuildConfig(name, path)
    _boot_check(cfg, build, exe, rules)
    cfg.set_build(name, path)
    cfg.save()
    console.print(f"[green]{cfg.path} に '{name}' を保存しました[/]")
    return cfg.get_build(name)


# --- run ---


def _status_text(s: dict[str, Any]) -> Text:
    e = int(s["elapsed"])
    c = s["counts"]
    line = (
        f"[{e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d}] {s['build']}  "
        f"launches {s['launches']}  restarts {s['restarts']}  "
        f"issues: C{c['critical']} M{c['major']} m{c['minor']}"
    )
    if s["last"]:
        line += f"\n           last: {s['last'][:100]}"
    return Text(line)


@app.command()
def run(
    ctx: typer.Context,
    build: str | None = typer.Option(None, "--build", "-b", help="登録済みビルドの名前"),
    build_path: Path | None = typer.Option(None, "--build-path", help="その場限りで使うビルドのパス（登録しない）"),
    hours: float | None = typer.Option(None, "--hours", help="実行時間（時間）。既定は yomawari.toml の run.hours"),
    mode: str = typer.Option("fuzz", "--mode", help="fuzz | aging | random（フェーズ1は観測のみ）"),
    seed: str | None = typer.Option(None, "--seed", help="乱数シード。既定は yomawari.toml の run.seed"),
    no_open: bool = typer.Option(False, "--no-open", help="終了後にレポートを開かない"),
) -> None:
    """ビルドを起動して監視し、report.md を出力する。"""
    if mode not in ("fuzz", "aging", "random"):
        err.print(f"[red]--mode が不正です: {mode}[/]")
        raise typer.Exit(EXIT_HARNESS_ERROR)
    cfg = _config(ctx)
    plat = get_platform()
    rules = load_rules(cfg.rules_path)
    b = cfg.resolve_build(build, build_path)
    if b is None:
        b = register_interactive(cfg, plat, rules)
    exe, msgs = launcher.validate_build(b, plat)
    if exe is None:
        _print_msgs(msgs)
        raise typer.Exit(EXIT_HARNESS_ERROR)

    rc = cfg.run
    opts = RunOptions(
        hours=float(hours if hours is not None else rc["hours"]),
        mode=mode,
        seed=str(seed if seed is not None else rc["seed"]),
    )
    live = Live(Text("起動中…"), console=console, refresh_per_second=2) if console.is_terminal else None
    harness = Harness(
        b, exe, plat, rules, cfg.rules_path, cfg.runs_dir, rc, cfg.sensors,
        on_status=(lambda s: live.update(_status_text(s))) if live else None,
    )
    if live:
        with live:
            result = asyncio.run(harness.run(opts))
    else:
        result = asyncio.run(harness.run(opts))

    console.print(f"レポート: {result.report_path}")
    if rc["open_report"] and not no_open:
        plat.open_file(result.report_path)
    raise typer.Exit(result.exit_code)


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


# --- builds ---


@builds_app.command("list")
def builds_list(ctx: typer.Context) -> None:
    """登録済みビルドの一覧。"""
    cfg = _config(ctx)
    names = cfg.build_names()
    if not names:
        console.print(f"登録済みのビルドはありません（{cfg.path}）")
        return
    t = Table("名前", "パス", "既定", "存在")
    for n in names:
        b = cfg.get_build(n)
        t.add_row(n, str(b.path), "*" if n == cfg.default_build else "", "✓" if b.path.exists() else "✗")
    console.print(t)


@builds_app.command("add")
def builds_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="ビルドの名前"),
    path: str = typer.Argument(..., help="ビルドのパス（.app または実行ファイル）"),
    no_launch: bool = typer.Option(False, "--no-launch", help="起動しての boot 確認を省く"),
) -> None:
    """ビルドを登録する。"""
    cfg = _config(ctx)
    plat = get_platform()
    p = normalize_path_input(path)
    build = BuildConfig(name, p)
    exe, msgs = launcher.validate_build(build, plat)
    _print_msgs(msgs)
    if exe is None:
        raise typer.Exit(EXIT_HARNESS_ERROR)
    if not no_launch:
        _boot_check(cfg, build, exe, load_rules(cfg.rules_path))
    cfg.set_build(name, p)
    cfg.save()
    console.print(f"[green]{cfg.path} に '{name}' を保存しました[/]")


@builds_app.command("edit")
def builds_edit(ctx: typer.Context, name: str = typer.Argument(..., help="ビルドの名前")) -> None:
    """登録済みビルドのパスと起動引数を対話で変更する。"""
    _require_tty("builds edit")
    cfg = _config(ctx)
    plat = get_platform()
    cur = cfg.get_build(name)
    while True:
        p = normalize_path_input(Prompt.ask("ビルドのパス", default=str(cur.path)))
        exe, msgs = launcher.validate_build(BuildConfig(name, p), plat)
        _print_msgs(msgs)
        if exe is not None:
            break
    args = shlex.split(Prompt.ask("起動引数", default=shlex.join(cur.args)))
    cfg.set_build(name, p, args)
    cfg.save()
    console.print(f"[green]'{name}' を更新しました[/]")


@builds_app.command("check")
def builds_check(ctx: typer.Context, name: str = typer.Argument(..., help="ビルドの名前")) -> None:
    """ビルドを検証し、起動して boot 行が出るか確かめる。"""
    cfg = _config(ctx)
    plat = get_platform()
    b = cfg.get_build(name)
    console.print(f"[bold]{name}[/] {b.path}")
    exe, msgs = launcher.validate_build(b, plat)
    _print_msgs(msgs)
    if exe is None:
        raise typer.Exit(EXIT_HARNESS_ERROR)
    target = resolve_target(b, plat)
    console.print(f"  セーブの場所: {target.save_dir or '不明'}")
    console.print(f"  PlayerPrefs: {target.prefs_domain or '不明'}")
    ok = _boot_check(cfg, b, exe, load_rules(cfg.rules_path))
    raise typer.Exit(EXIT_OK if ok else EXIT_CRITICAL)


# --- schedule ---


@schedule_app.command("install")
def schedule_install(at: str = typer.Option("01:00", "--at", help="起動時刻（HH:MM）")) -> None:
    """launchd に定時実行を登録する。"""
    _stub("schedule install", at=at)


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """launchd の定時実行を解除する。"""
    _stub("schedule uninstall")


# --- doctor / restore ---


@app.command()
def doctor(ctx: typer.Context) -> None:
    """実行環境を確認する。"""
    cfg = _config(ctx)
    plat = get_platform()
    msgs: list[tuple[str, str]] = []

    machine = pyplatform.machine()
    msgs.append(("ok" if machine == "arm64" else "error", f"Python {sys.version.split()[0]} ({machine})"))

    for perm, granted in plat.check_permissions().items():
        if granted is None:
            msgs.append(("warn", f"{perm}権限: 確認できませんでした"))
        else:
            msgs.append(("ok" if granted else "error", f"{perm}権限: {'あり' if granted else 'なし（システム設定 → プライバシーとセキュリティ で実行元のターミナルに付与）'}"))

    try:
        rules = load_rules(cfg.rules_path)
        msgs.append(("ok", f"rules.yaml: {len(rules)} ルール（{cfg.rules_path}）"))
    except RuleError as e:
        msgs.append(("error", str(e)))

    if not cfg.exists:
        msgs.append(("warn", f"{cfg.path} がありません（yomawari.example.toml を参考に作るか、run で対話登録）"))
    for n in cfg.build_names():
        try:
            exe, bmsgs = launcher.validate_build(cfg.get_build(n), plat)
        except ConfigError as e:
            msgs.append(("error", str(e)))
            continue
        level = "ok" if exe else "error"
        msgs.append((level, f"ビルド {n}: " + "; ".join(m for _, m in bmsgs)))

    judge = cfg.judge
    if judge.get("kind", "none") != "none":
        endpoint = judge.get("endpoint", "http://localhost:11434")
        model = judge.get("model", "")
        try:
            tags = httpx.get(f"{endpoint}/api/tags", timeout=3).json()
            names = {m.get("name") for m in tags.get("models", [])}
            if model and model in names:
                msgs.append(("ok", f"Ollama: {model} は pull 済み"))
            else:
                msgs.append(("warn", f"Ollama は起動していますが、モデル '{model}' が見つかりません"))
        except (httpx.HTTPError, ValueError):
            msgs.append(("warn", f"Ollama に接続できません（{endpoint}）。画像判定はフェーズ5から使います"))

    if SaveState(cfg.runs_dir, plat).pending():
        msgs.append(("warn", "未復元のセーブ退避があります。yomawari restore を実行してください"))
    if shutil.which("caffeinate") is None:
        msgs.append(("warn", "caffeinate が見つかりません"))

    _print_msgs(msgs)
    raise typer.Exit(EXIT_CRITICAL if any(lv == "error" for lv, _ in msgs) else EXIT_OK)


@app.command()
def restore(ctx: typer.Context) -> None:
    """異常終了などで戻っていないセーブと PlayerPrefs を復元する。"""
    cfg = _config(ctx)
    notes = SaveState(cfg.runs_dir, get_platform()).restore()
    if not notes:
        console.print("未復元の退避はありません")
    for n in notes:
        console.print(f"  {n}")


def main() -> None:
    try:
        app(prog_name="yomawari")
    except (ConfigError, RuleError) as e:
        err.print(f"[red]{e}[/]")
        sys.exit(EXIT_HARNESS_ERROR)
    except Exception:
        err.print("[red]ハーネス自体のエラーで終了しました[/]")
        err.print(traceback.format_exc(), markup=False)
        sys.exit(EXIT_HARNESS_ERROR)
