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
from datetime import datetime
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
from .fuzz.experience import SECTIONS, experience_path, forget, section_sizes
from .fuzz.roles import RoleBook, load_role_hints
from .platform import Platform, get_platform
from .profile import apply_rules, load_spec
from .purpose import MODES, PurposeError
from .purpose import resolve as resolve_purpose
from .savestate import SaveState, resolve_target
from .sensors.window import ClientArea
from .state import GameState
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
experience_app = typer.Typer(no_args_is_help=True, help="ランをまたいで覚えたこと（経験の蓄積）の管理")
app.add_typer(builds_app, name="builds")
app.add_typer(schedule_app, name="schedule")
app.add_typer(experience_app, name="experience")

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


def _game_name(profile_path: Path | None, b: BuildConfig) -> str:
    """経験の集計ファイルの <ゲーム>: プロファイルがあればその親ディレクトリ名、なければビルドの名前（DESIGN.md §9）。"""
    return profile_path.parent.name if profile_path else b.name


def _status_text(s: dict[str, Any]) -> Text:
    e = int(s["elapsed"])
    c = s["counts"]
    line = (
        f"[{e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d}] {s['build']}  "
        f"launches {s['launches']}  restarts {s['restarts']}  "
        f"issues: C{c['critical']} M{c['major']} m{c['minor']}"
    )
    if s.get("scene"):
        line += f"  scene: {s['scene']}"
    if s.get("ops") is not None:
        line += f"  ops: {s['ops']}"
    if s.get("blocked"):
        line += f"\n           操作できません: {s['blocked']}（画面のロックを解除し、ゲームを前面に戻すと続けます）"
    if s["last"]:
        line += f"\n           last: {s['last'][:100]}"
    return Text(line)


@app.command()
def run(
    ctx: typer.Context,
    build: str | None = typer.Option(None, "--build", "-b", help="登録済みビルドの名前"),
    build_path: Path | None = typer.Option(None, "--build-path", help="その場限りで使うビルドのパス（登録しない）"),
    hours: float | None = typer.Option(None, "--hours", help="実行時間（時間）。既定は yomawari.toml の run.hours"),
    purpose: str | None = typer.Option(
        None, "--purpose", "-p",
        help="ランの目的: play（通常のプレイ）| stuck（進行不能を探す）| buttons（ボタンの不具合）| gacha（ガチャプレイ）"
             "| rta（最短で進める）| aging（放置）、または yomawari.toml で足した目的。既定は yomawari.toml の run.purpose（play）",
    ),
    mode: str | None = typer.Option(
        None, "--mode",
        help="目的の mode を上書きする: fuzz（通常の操作 + イレギュラー操作）| random（通常の操作のみ）| aging（操作せず観測のみ）",
    ),
    seed: str | None = typer.Option(None, "--seed", help="乱数シード。既定は yomawari.toml の run.seed"),
    no_open: bool = typer.Option(False, "--no-open", help="終了後にレポートを開かない"),
    cont: bool | None = typer.Option(
        None, "--continue/--from-start",
        help="ランの最初の起動から、前のランのセーブの続きを遊ぶ（タイトルの「続きから」といちばん新しいセーブを選ぶ）。"
             "既定は yomawari.toml の run.continue（false: はじめから）",
    ),
    display_off: bool | None = typer.Option(
        None, "--display-off/--no-display-off",
        help="ゲームが起動したあと、画面（ディスプレイ）を消す（macOS: pmset displaysleepnow）。既定は yomawari.toml の run.display_off",
    ),
    forget_all: bool = typer.Option(
        False, "--forget",
        help="ランを始める前に、そのゲームで覚えたこと（runs/_experience/<ゲーム>.json）をすべて消す（.bak-<日時> に写してから）。"
             "一部だけ消すときは yomawari experience forget",
    ),
) -> None:
    """ビルドを起動して監視し、report.md を出力する。"""
    if mode is not None and mode not in MODES:
        err.print(f"[red]--mode が不正です: {mode}[/]")
        raise typer.Exit(EXIT_HARNESS_ERROR)
    cfg = _config(ctx)
    try:
        goal = resolve_purpose(str(purpose or cfg.run["purpose"]), cfg.purposes)
    except PurposeError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(EXIT_HARNESS_ERROR) from None
    mode = mode or goal.mode
    console.print(f"目的: {goal.name}（{goal.description}）／モード: {mode}")
    if goal.burst is not None:
        b = goal.burst
        console.print(f"  初めて来た画面・scene で、確率 {b.prob} で {b.sec:.0f} 秒だけ区間（{b.mode}）に切り替える"
                      f"（ランの {b.share:.0%} まで）")
    plat = get_platform()
    rules = load_rules(cfg.rules_path)
    b = cfg.resolve_build(build, build_path)
    if b is None:
        b = register_interactive(cfg, plat, rules)
    exe, msgs = launcher.validate_build(b, plat)
    if exe is None:
        _print_msgs(msgs)
        raise typer.Exit(EXIT_HARNESS_ERROR)
    spec = load_spec(cfg, b)
    rules = apply_rules(rules, spec)
    if spec.profile_path:
        console.print(f"プロファイル: {spec.profile_path}")
    # ボタンの役割の推定と確定（DESIGN.md §9「役割の推定と判定」）。ランをまたいで runs/_experience/<ゲーム>.json に覚える
    game = _game_name(spec.profile_path, b)
    forget_note = None
    if forget_all:
        done = forget(experience_path(cfg.runs_dir, game), None, datetime.now().strftime("%Y%m%d-%H%M%S"))
        if done.backup is None:
            forget_note = f"--forget: 覚えたことはなかった（{game}）"
        else:
            forget_note = f"--forget: ランの前に、覚えたこと（{game}）をすべて消した。消す前の写し: {done.backup}"
        console.print(forget_note)
    roles = RoleBook(load_role_hints(cfg.rules_path), experience_path(cfg.runs_dir, game))

    rc = cfg.run
    dark = bool(rc["display_off"] if display_off is None else display_off)
    if dark:
        lock = getattr(plat, "screen_lock_delay", lambda: None)()
        console.print(f"ゲームが起動して {float(rc['display_off_delay_sec']):.0f} 秒後に、画面を消します")
        if lock is not None and lock != "off":
            when = "すぐ" if lock == "immediate" else lock.replace(" seconds", " 秒後に")
            err.print(f"[yellow]注意: この Mac は画面が消えると{when}ロックされます。ロックされると操作できません"
                      "（システム設定 > ロック画面 の「スクリーンセーバの開始後またはディスプレイがオフのときにパスワードを要求」を"
                      "「しない」か長い時間にする）[/]")
    opts = RunOptions(
        hours=float(hours if hours is not None else rc["hours"]),
        mode=mode,
        seed=str(seed if seed is not None else rc["seed"]),
        purpose=goal,
        display_off=dark,
        display_off_delay_sec=float(rc["display_off_delay_sec"]),
        continue_previous=bool(rc["continue"] if cont is None else cont),
    )
    if opts.continue_previous:
        console.print("前のランのセーブの続きから遊びます（セーブが見つからなければ、はじめから）")
    live = Live(Text("起動中…"), console=console, refresh_per_second=2) if console.is_terminal else None
    harness = Harness(
        b, exe, plat, rules, cfg.rules_path, cfg.runs_dir, rc, cfg.sensors,
        on_status=(lambda s: live.update(_status_text(s))) if live else None,
        fuzz_cfg=goal.apply(cfg.fuzz),
        spec=spec,
        roles=roles,
        judge_cfg=cfg.judge,
    )
    if forget_note:
        harness.notes.append(forget_note)  # レポートの「ハーネスの記録」に、学習を消してから回したことを残す
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
def inspect(
    ctx: typer.Context,
    build: str | None = typer.Option(None, "--build", "-b", help="登録済みビルドの名前"),
    seconds: float = typer.Option(60, "--seconds", help="起動しておく時間（秒）"),
    click: list[str] = typer.Option([], "--click", help='押すボタン "<menu>/<ボタン>"（複数指定で順に押す）'),
) -> None:
    """ビルドを起動し、画面に出ている menu とボタン、描画領域を表示する。--click で押すこともできる。"""
    from .inspector import inspect_build
    from .input.mouse_kb import PyAutoGuiDriver

    cfg = _config(ctx)
    plat = get_platform()
    b = cfg.resolve_build(build, None)
    if b is None:
        err.print("[red]ビルドを --build で指定するか、yomawari.toml の default_build を設定してください[/]")
        raise typer.Exit(EXIT_HARNESS_ERROR)
    exe, msgs = launcher.validate_build(b, plat)
    if exe is None:
        _print_msgs(msgs)
        raise typer.Exit(EXIT_HARNESS_ERROR)

    def on_change(state: GameState, area: ClientArea | None) -> None:
        console.rule(f"scene: {state.scene or '-'}  busy: {state.busy}")
        if state.resolution:
            r = state.resolution
            where = (
                f"描画領域 ({area.rect.x:.0f}, {area.rect.y:.0f}) {area.rect.w:.0f}x{area.rect.h:.0f}pt  倍率 {area.scale:g}"
                if area else "描画領域: 取得できません"
            )
            console.print(f"解像度 {r['w']}x{r['h']} {r['mode']}  {where}")
        top = state.top_modal()
        table = Table("menu", "modal", "ボタン（--click に渡す名前）", show_lines=False)
        for m in sorted(state.menus.values(), key=lambda m: -m.order):
            mark = "最前面" if top is m else ("yes" if m.modal else "")
            table.add_row(m.name, mark, "\n".join(t.key for t in m.buttons) or "-")
        if state.choices:
            table.add_row("choices", "", "\n".join(f"{c.key}  {c.text or ''}" for c in state.choices))
        console.print(table)

    def on_click(key: str, pos: tuple[float, float] | None, error: str | None) -> None:
        if pos is not None:
            console.print(f"[green]押しました[/] {key} ({pos[0]:.0f}, {pos[1]:.0f})")
        else:
            console.print(f"[red]押せませんでした[/] {key}: {error}")

    result = asyncio.run(
        inspect_build(
            exe, b, plat, cfg.runs_dir, seconds, click, cfg.sensors,
            float(cfg.run["quit_timeout_sec"]), PyAutoGuiDriver, on_change, on_click,
        )
    )
    for key, reason in result.not_clicked:
        console.print(f"[yellow]押していません[/] {key}: {reason}")
    if result.exit_code is not None:
        console.print(f"[yellow]ゲームが終了しました（終了コード {result.exit_code}）[/]")
    for n in result.notes:
        log.info(n)
    raise typer.Exit(EXIT_OK if not result.not_clicked else EXIT_CRITICAL)


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
def doctor(
    ctx: typer.Context,
    fix: bool = typer.Option(False, "--fix", help="足りない権限の確認ダイアログと、システム設定の該当画面を開く"),
) -> None:
    """実行環境を確認する。"""
    cfg = _config(ctx)
    plat = get_platform()
    msgs: list[tuple[str, str]] = []

    machine = pyplatform.machine()
    msgs.append(("ok" if machine == "arm64" else "error", f"Python {sys.version.split()[0]} ({machine})"))

    missing: list[str] = []
    for perm, granted in plat.check_permissions().items():
        if granted is None:
            msgs.append(("warn", f"{perm}権限: 確認できませんでした"))
        else:
            msgs.append(("ok" if granted else "error", f"{perm}権限: {'あり' if granted else 'なし'}"))
            if not granted:
                missing.append(perm)

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
    if missing:
        console.print("\n権限の付け方:")
        for line in plat.permission_help(missing):
            console.print(f"  - {line}")
        if fix:
            plat.request_permissions(missing)
            console.print("  確認ダイアログとシステム設定を開きました。オンにしたら、上のアプリを再起動して doctor をもう一度実行してください")
        else:
            console.print("  yomawari doctor --fix で、確認ダイアログとシステム設定の該当画面を開けます")
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


@experience_app.command("forget")
def experience_forget(
    ctx: typer.Context,
    build: str | None = typer.Option(None, "--build", "-b", help="登録済みビルドの名前（そのゲームの経験を消す）"),
    game: str | None = typer.Option(None, "--game", help="runs/_experience/<ゲーム>.json の <ゲーム>（--build の代わり）"),
    roles: bool = typer.Option(False, "--roles", help="ボタンの役割だけ消す"),
    progress: bool = typer.Option(False, "--progress", help="進行につながった操作・巻き戻した操作だけ消す"),
    flow_wrong: bool = typer.Option(False, "--flow-wrong", help="ゲームの流れで外れと分かった答えだけ消す"),
    flow_right: bool = typer.Option(False, "--flow-right", help="ゲームの流れで正解と分かった答えだけ消す"),
    yes: bool = typer.Option(False, "--yes", "-y", help="確認せずに消す"),
) -> None:
    """ランをまたいで覚えたこと（runs/_experience/<ゲーム>.json）を消す。消す前に .bak-<日時> に写す。

    節を指定しなければ、すべて消す。ハーネスの仕様が変わったときや、ボタンの並びが変わるビルドで、覚えたことが
    合わなくなったときに使う。"""
    cfg = _config(ctx)
    if game is None:
        b = cfg.resolve_build(build, None)
        if b is None:
            err.print("[red]ビルドを --build で指定するか、--game でゲームの名前を指定してください"
                      "（yomawari.toml の default_build でもよい）[/]")
            raise typer.Exit(EXIT_HARNESS_ERROR)
        game = _game_name(load_spec(cfg, b).profile_path, b)
    path = experience_path(cfg.runs_dir, game)
    if not path.exists():
        names = sorted(p.stem for p in path.parent.glob("*.json")) if path.parent.exists() else []
        console.print(f"覚えたことはありません: {path}")
        if names:
            console.print(f"  ある経験: {', '.join(names)}（--game で指定する）")
        return
    chosen = [k for k, on in (("roles", roles), ("progress", progress), ("flow_wrong", flow_wrong),
                              ("flow_right", flow_right)) if on]
    sizes = section_sizes(path)
    targets = chosen or list(sizes)
    console.print(f"消すもの（{path}）:")
    for key in targets:
        console.print(f"  {SECTIONS.get(key, key)}（{key}）: {sizes.get(key, 0)} 件")
    if not chosen:
        console.print("  （ファイルごと消します）")
    if not yes:
        if not stdin_is_tty():
            err.print("[red]確認には対話入力が必要です。確認せずに消すなら --yes を付けてください[/]")
            raise typer.Exit(EXIT_HARNESS_ERROR)
        if Prompt.ask("消しますか", choices=["y", "n"], default="n") != "y":
            console.print("消しませんでした")
            return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        done = forget(path, chosen or None, stamp)
    except (OSError, ValueError) as e:
        err.print(f"[red]消せませんでした: {e}[/]")
        raise typer.Exit(EXIT_HARNESS_ERROR) from None
    what = "ファイルごと" if done.deleted else "、".join(f"{SECTIONS.get(k, k)} {n} 件" for k, n in done.removed.items())
    console.print(f"[green]消しました[/]: {what or '（該当する節はありませんでした）'}")
    console.print(f"  消す前の写し: {done.backup}")


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
