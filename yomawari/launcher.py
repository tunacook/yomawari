"""ビルドの起動。実行ファイルの解決と検証、-yomawari / -logFile の付与、停止。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from .config import BuildConfig
from .platform import Platform
from .sensors.logtail import PREFIX

Message = tuple[str, str]  # (ok / warn / error, 文言)


def validate_build(build: BuildConfig, platform: Platform) -> tuple[Path | None, list[Message]]:
    """パスの存在、.app の CFBundleExecutable の解決、実行形式を確認する。"""
    msgs: list[Message] = []
    if not build.path.exists():
        return None, [("error", f"パスが存在しません: {build.path}")]
    try:
        exe = platform.resolve_executable(build.path)
    except (OSError, ValueError) as e:
        return None, [("error", f"実行ファイルを解決できません: {e}")]
    if exe != build.path:
        msgs.append(("ok", f"実行ファイル: {exe}"))
    check = platform.check_executable(exe)
    msgs.extend(check.messages)
    return (exe if check.ok else None), msgs


def build_command(exe: Path, build: BuildConfig, log_path: Path) -> list[str]:
    return [str(exe), *build.args, "-yomawari", "-logFile", str(log_path)]


def launch(exe: Path, build: BuildConfig, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # start_new_session: Ctrl+C の SIGINT をゲームに直接届けず、ハーネスが終了処理を行う
    return subprocess.Popen(
        build_command(exe, build, log_path),
        cwd=exe.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop(proc: subprocess.Popen[bytes], timeout: float) -> int:
    """terminate → timeout 秒待つ → kill。終了コードを返す。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return proc.returncode


def find_boot(log_path: Path) -> dict | None:
    if not log_path.exists():
        return None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(PREFIX):
            try:
                data = json.loads(line[len(PREFIX):])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("ev") == "boot":
                return data
    return None


async def boot_check(
    exe: Path, build: BuildConfig, log_path: Path, timeout: float, quit_timeout: float
) -> dict | None:
    """一度起動して boot 行が出るか確かめ、終了させる。出れば boot の内容を返す。"""
    proc = launch(exe, build, log_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    boot = None
    try:
        while loop.time() < deadline and proc.poll() is None:
            boot = find_boot(log_path)
            if boot:
                break
            await asyncio.sleep(0.2)
        boot = boot or find_boot(log_path)
    finally:
        await asyncio.to_thread(stop, proc, quit_timeout)
    return boot
