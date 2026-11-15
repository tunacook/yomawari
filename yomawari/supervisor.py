"""見張り役（ウォッチドッグ）。DESIGN.md §7「ラン・ライフサイクルとウォッチドッグ」。

`yomawari run` は、同じコマンドをハーネス本体として子プロセスで起動し、終わるまで見張る。ハーネス本体が終了処理まで
進まずに終わった（例外・kill -9・Ctrl+\\）か、`events.jsonl` が `watchdog_hang_sec` 秒増えなくなった（固まった）ら、
見張り役が後始末をする: 残ったゲームを終了し、退避したセーブと設定を戻し、記録からレポートを作り直す。

- ハーネス本体は別のセッション（プロセスグループ）で動かす。端末の Ctrl+C は見張り役だけに届き、見張り役が 1 回ずつ渡す
  （同じグループだと、子に端末からと見張り役からの 2 回届く）。SIGTERM・SIGHUP は SIGTERM にして渡す
- Ctrl+\\（SIGQUIT）は「待たずに止める」: ハーネス本体をすぐ止め（SIGKILL）、見張り役が後始末をする
- ハーネス本体は、環境変数 `YOMAWARI_WATCHDOG_STATE` が指すファイルに、ランのディレクトリと、終わる前に出た例外を書く
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_ENV = "YOMAWARI_WATCHDOG_STATE"


def child_state_path() -> Path | None:
    """ハーネス本体として見張られていれば、状態を書くファイル。"""
    p = os.environ.get(STATE_ENV)
    return Path(p) if p else None


def write_child_state(**values: Any) -> None:
    """ハーネス本体から、見張り役に知らせる値を書き足す（見張られていなければ何もしない）。"""
    path = child_state_path()
    if path is None:
        return
    try:
        state = read_state(path)
        state.update(values)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_state(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def running_game(events_path: Path) -> tuple[int, str, str] | None:
    """記録で、起動したあと終了（proc_exit）していないゲームの (pid, 実行ファイル, 起動した時刻)。"""
    launched: dict[int, tuple[int, str, str]] = {}
    try:
        with events_path.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if e.get("kind") == "launch" and e.get("source") == "harness":
                    d = e.get("data") or {}
                    cmd = d.get("cmd") or [""]
                    if isinstance(d.get("pid"), int):
                        launched[e.get("launch", 0)] = (d["pid"], str(cmd[0]), str(e.get("wall") or ""))
                elif e.get("kind") == "proc_exit":
                    launched.pop(e.get("launch", 0), None)
    except OSError:
        return None
    return max(launched.items())[1] if launched else None


def _same_process(proc: Any, exe: str, wall: str, slack_sec: float) -> bool:
    """記録にあるゲームのプロセスか: 実行ファイルが同じか、起動した時刻が近い（スクリプトから起動するゲームでは、
    実行ファイルが別のものになる）。pid が別のプロセスに使い回されていたら、どちらにも当たらない。"""
    from datetime import datetime

    if os.path.realpath(proc.exe()) == os.path.realpath(exe):
        return True
    try:
        return abs(proc.create_time() - datetime.fromisoformat(wall).timestamp()) <= slack_sec
    except ValueError:
        return False


def stop_game(pid: int, exe: str, wall: str, timeout: float, slack_sec: float = 10.0) -> str | None:
    """記録にあるゲームがまだ動いていれば終了させ、そのことを返す。別のプロセスに pid が使い回されていれば触らない。"""
    import psutil

    try:
        proc = psutil.Process(pid)
        if not _same_process(proc, exe, wall, slack_sec):
            return None
        proc.terminate()
        try:
            proc.wait(timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        return f"残っていたゲーム（pid {pid}）を終了した"
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as e:
        return f"残っていたゲーム（pid {pid}）を終了できなかった: {e}"


@dataclass
class Supervisor:
    argv: list[str]  # ハーネス本体のコマンド
    runs_dir: Path
    rules_path: Path
    hang_sec: float
    quit_timeout_sec: float
    poll_sec: float = 1.0
    notes: list[str] = field(default_factory=list)
    _proc: subprocess.Popen[bytes] | None = None
    _forced: bool = False

    def _forward(self, signum: int, _frame: Any) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if signum == signal.SIGQUIT:
            self._forced = True
            proc.kill()
            signal.signal(signal.SIGQUIT, signal.SIG_DFL)  # もう一度押せば、見張り役も待たずに止まる
        elif signum == signal.SIGINT:
            proc.send_signal(signal.SIGINT)
        else:
            proc.send_signal(signal.SIGTERM)

    def _stop_hung(self, proc: subprocess.Popen[bytes]) -> None:
        proc.terminate()
        try:
            proc.wait(self.quit_timeout_sec + 20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def run(self) -> tuple[int, Path | None]:
        """ハーネス本体を動かして見張る。(終了コード, 後始末で作り直したレポート。作り直していなければ None)。"""
        state_path = self.runs_dir / "_watchdog" / f"{os.getpid()}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.unlink(missing_ok=True)
        handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
        old = {s: signal.getsignal(s) for s in handled}
        try:
            self._proc = subprocess.Popen(self.argv, env={**os.environ, STATE_ENV: str(state_path)},
                                          start_new_session=True)
            for s in handled:
                signal.signal(s, self._forward)
            code, why = self._watch(self._proc, state_path)
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        state = read_state(state_path)
        run_dir = Path(state["run_dir"]) if state.get("run_dir") else None
        report = None
        if run_dir is not None and not self._finished(run_dir):
            report = self._clean_up(run_dir, why or self._why(code), state.get("error"))
            code = 2
        state_path.unlink(missing_ok=True)
        return (code if code >= 0 else 2), report

    def _watch(self, proc: subprocess.Popen[bytes], state_path: Path) -> tuple[int, str]:
        while True:
            try:
                code = proc.wait(self.poll_sec)
                return code, ""
            except subprocess.TimeoutExpired:
                pass
            run_dir = read_state(state_path).get("run_dir")
            events = Path(run_dir) / "events.jsonl" if run_dir else None
            if events is None or (Path(run_dir) / "report.md").exists():
                continue  # まだランを始めていないか、もうレポートを書いている
            try:
                idle = time.time() - events.stat().st_mtime
            except OSError:
                continue
            if idle >= self.hang_sec:
                self._stop_hung(proc)
                return proc.returncode, f"ハーネス本体の記録（events.jsonl）が {idle:.0f} 秒増えなかったため、固まったとみなして止めた"

    def _why(self, code: int) -> str:
        if self._forced:
            return "Ctrl+\\ で、終了処理を待たずに止めた"
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = str(-code)
            return f"ハーネス本体がシグナル（{name}）で終わった"
        return f"ハーネス本体が終了処理まで進まずに終わった（終了コード {code}）"

    @staticmethod
    def _finished(run_dir: Path) -> bool:
        from .post.rebuild import load_summary

        summary = load_summary(run_dir)
        return summary is not None and bool(summary.ended) and (run_dir / "report.md").exists()

    def _clean_up(self, run_dir: Path, why: str, error: str | None) -> Path | None:
        from .platform import get_platform
        from .post.rebuild import rebuild_report
        from .savestate import SaveState

        notes = [f"見張り役（ウォッチドッグ）: {why}。ゲームの終了・セーブの復元・レポートの作り直しを、見張り役がした"]
        if error:
            notes.append(f"ハーネス本体の例外: {error.strip().splitlines()[-1]}（全文は {run_dir / 'harness_error.txt'}）")
            try:
                (run_dir / "harness_error.txt").write_text(error, encoding="utf-8")
            except OSError:
                pass
        game = running_game(run_dir / "events.jsonl")
        if game is not None:
            done = stop_game(*game, timeout=self.quit_timeout_sec)
            if done:
                notes.append(done)
        try:
            notes += SaveState(self.runs_dir, get_platform()).restore()
        except Exception as e:  # 復元の失敗は必ずレポートに残す
            notes.append(f"セーブ・設定の復元に失敗しました: {e!r}（yomawari restore で再試行できます）")
        self.notes = notes
        try:
            return rebuild_report(run_dir, self.rules_path, notes)
        except (OSError, ValueError) as e:
            print(f"レポートを作り直せませんでした: {e}", file=sys.stderr)
            return None
