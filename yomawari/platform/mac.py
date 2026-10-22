"""macOS 固有の処理: .app の解決、実行形式の確認、保存場所、PlayerPrefs、権限確認。"""

from __future__ import annotations

import ctypes
import os
import plistlib
import struct
import subprocess
from pathlib import Path

from ..sensors.window import ScreenRect, WindowInfo
from . import ExecCheck, GameIdentity

_CPU_ARM64 = 0x0100000C
_CPU_X86_64 = 0x01000007
_CPU_NAMES = {_CPU_ARM64: "arm64", _CPU_X86_64: "x86_64"}

HOME = Path.home()

# 権限名 → (システム設定の URL のアンカー, 設定画面での表示名)
_PERMISSION_PANES = {
    "画面収録": ("Privacy_ScreenCapture", "画面収録とシステム音声録音"),
    "アクセシビリティ": ("Privacy_Accessibility", "アクセシビリティ"),
}


def app_bundle_of(exe: str) -> str | None:
    """実行ファイルのパスから、いちばん外側の .app を返す（VS Code の Helper.app などは外側のアプリにまとめる）。"""
    head, sep, _ = exe.partition(".app/")
    return head + ".app" if sep else None


def read_info_plist(app: Path) -> dict:
    with (app / "Contents" / "Info.plist").open("rb") as fp:
        return plistlib.load(fp)


def macho_archs(exe: Path) -> list[str] | None:
    """Mach-O なら含まれるアーキテクチャ名の一覧を返す。Mach-O でなければ None。"""
    with exe.open("rb") as fp:
        head = fp.read(8)
        if len(head) < 8:
            return None
        magic_be = struct.unpack(">I", head[:4])[0]
        if magic_be in (0xCAFEBABE, 0xCAFEBABF):  # fat / fat64（ビッグエンディアン）
            n = struct.unpack(">I", head[4:8])[0]
            size = 20 if magic_be == 0xCAFEBABE else 32
            archs = []
            for _ in range(n):
                ent = fp.read(size)
                cpu = struct.unpack(">i", ent[:4])[0] & 0xFFFFFFFF
                archs.append(_CPU_NAMES.get(cpu, hex(cpu)))
            return archs
        magic_le = struct.unpack("<I", head[:4])[0]
        if magic_le in (0xFEEDFACF, 0xFEEDFACE):
            cpu = struct.unpack("<i", head[4:8])[0] & 0xFFFFFFFF
            return [_CPU_NAMES.get(cpu, hex(cpu))]
    return None


class MacPlatform:
    name = "mac"

    def resolve_executable(self, build_path: Path) -> Path:
        if build_path.suffix == ".app":
            info = read_info_plist(build_path)
            exe_name = info.get("CFBundleExecutable")
            if not exe_name:
                raise ValueError(f"{build_path} の Info.plist に CFBundleExecutable がありません")
            return build_path / "Contents" / "MacOS" / exe_name
        return build_path

    def check_executable(self, exe: Path) -> ExecCheck:
        msgs: list[tuple[str, str]] = []
        if not exe.is_file():
            return ExecCheck(False, [("error", f"実行ファイルがありません: {exe}")])
        if not os.access(exe, os.X_OK):
            return ExecCheck(False, [("error", f"実行権限がありません: {exe}")])
        archs = macho_archs(exe)
        if archs is None:
            with exe.open("rb") as fp:
                if fp.read(2) == b"#!":
                    msgs.append(("warn", "Mach-O ではなくスクリプトです（モックゲーム等）"))
                    return ExecCheck(True, msgs)
            return ExecCheck(False, [("error", "macOS で実行できる形式（Mach-O）ではありません")])
        if "arm64" in archs:
            msgs.append(("ok", f"Mach-O ({', '.join(archs)})"))
        elif "x86_64" in archs:
            msgs.append(("warn", "x86_64 のみのビルドです。Rosetta で動きます"))
        else:
            return ExecCheck(False, [("error", f"未対応のアーキテクチャです: {archs}")])
        return ExecCheck(True, msgs)

    def guess_identity(self, build_path: Path) -> GameIdentity:
        if build_path.suffix != ".app":
            return GameIdentity(None, None)
        try:
            info = read_info_plist(build_path)
        except (OSError, plistlib.InvalidFileException):
            return GameIdentity(None, None)
        bundle_id = info.get("CFBundleIdentifier")
        product = info.get("CFBundleName")
        company = None
        if bundle_id:
            parts = bundle_id.split(".")
            if len(parts) >= 3:
                company = parts[1]
                product = product or parts[-1]
        return GameIdentity(company, product, bundle_id)

    def default_save_dir(self, ident: GameIdentity) -> Path | None:
        # ビルドはバンドルID形式を使う。Company/Product 形式はエディタ側のもので、
        # バンドルIDが分からないときの予備としてだけ使う
        base = HOME / "Library" / "Application Support"
        if ident.bundle_id:
            return base / ident.bundle_id
        if ident.company and ident.product:
            return base / ident.company / ident.product
        return None

    def default_prefs_domain(self, ident: GameIdentity) -> str | None:
        # ビルドの PlayerPrefs はバンドルID。unity.<company>.<product> はエディタ側のもの
        if ident.bundle_id:
            return ident.bundle_id
        if ident.company and ident.product:
            return f"unity.{ident.company}.{ident.product}"
        return None

    def crash_dir(self) -> Path:
        return HOME / "Library" / "Logs" / "DiagnosticReports"

    def crash_suffixes(self) -> tuple[str, ...]:
        return (".ips", ".crash")

    def prefs_export(self, domain: str, dest: Path) -> bool:
        """PlayerPrefs を書き出す。ドメインが存在しなければ False。

        cfprefsd がキャッシュを持つため plist ファイルを直接コピーせず defaults を使う。
        """
        r = subprocess.run(["defaults", "export", domain, str(dest)], capture_output=True)
        if r.returncode != 0:
            return False
        # 存在しないドメインでも終了コード0で空の plist を書き出すため、中身で判定する
        try:
            with dest.open("rb") as fp:
                empty = not plistlib.load(fp)
        except (OSError, plistlib.InvalidFileException):
            empty = True
        if empty:
            dest.unlink(missing_ok=True)
            return False
        return True

    def prefs_replace(self, domain: str, src: Path | None) -> None:
        subprocess.run(["defaults", "delete", domain], capture_output=True)
        if src is not None:
            subprocess.run(["defaults", "import", domain, str(src)], check=True, capture_output=True)

    def check_permissions(self) -> dict[str, bool | None]:
        res: dict[str, bool | None] = {"画面収録": None, "アクセシビリティ": None}
        try:
            cg = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
            )
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            res["画面収録"] = bool(cg.CGPreflightScreenCaptureAccess())
        except (OSError, AttributeError):
            pass
        try:
            ax = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            res["アクセシビリティ"] = bool(ax.AXIsProcessTrusted())
        except (OSError, AttributeError):
            pass
        return res

    def permission_host(self) -> str | None:
        """権限を付ける対象のアプリ（yomawari を起動したターミナルなど）。親プロセスをたどって最初の .app。"""
        import psutil

        try:
            proc: psutil.Process | None = psutil.Process()
            while proc is not None:
                try:
                    app = app_bundle_of(proc.exe())
                except psutil.Error:
                    app = None
                if app is not None:
                    return app
                proc = proc.parent()
        except psutil.Error:
            pass
        return None

    def permission_help(self, missing: list[str]) -> list[str]:
        host = self.permission_host()
        target = f"「{Path(host).stem}」（{host}）" if host else "yomawari を実行しているターミナルアプリ"
        lines = [f"権限を付ける対象: {target}"]
        for perm in missing:
            pane = _PERMISSION_PANES[perm][1]
            lines.append(
                f"システム設定 → プライバシーとセキュリティ →（下にスクロール）{pane} を開き、{target} をオンにする"
                "（一覧になければ、一覧の下の「＋」で追加）"
            )
        lines.append("付けたあと、そのアプリを ⌘Q で終了して起動し直す（権限は起動時に読み込まれる）")
        lines.append("tmux や VS Code などの中で実行している場合も、対象は外側のアプリ")
        return lines

    def request_permissions(self, missing: list[str]) -> None:
        """OS の確認ダイアログを出し（設定の一覧に対象のアプリが載る）、設定画面を開く。"""
        if "アクセシビリティ" in missing:
            try:
                import objc
                from Foundation import NSDictionary

                ax = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
                )
                ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
                ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
                opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
                ax.AXIsProcessTrustedWithOptions(objc.pyobjc_id(opts))
            except (ImportError, OSError, AttributeError):
                pass
        if "画面収録" in missing:
            try:
                import Quartz

                Quartz.CGRequestScreenCaptureAccess()
            except (ImportError, AttributeError):
                pass
        for perm in missing:
            subprocess.run(
                ["open", f"x-apple.systempreferences:com.apple.preference.security?{_PERMISSION_PANES[perm][0]}"],
                check=False,
            )

    def open_file(self, path: Path) -> None:
        subprocess.run(["open", str(path)], check=False)

    # --- ウィンドウ（pyobjc。テストや権限のない環境でも import だけで落ちないよう、使うときに読み込む） ---

    def window_info(self, pid: int) -> WindowInfo | None:
        """pid のいちばん大きい通常ウィンドウ（レイヤー0）。外枠はポイント単位、メインディスプレイの左上原点。"""
        import Quartz

        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        best: WindowInfo | None = None
        for w in infos:
            if w.get(Quartz.kCGWindowOwnerPID) != pid or w.get(Quartz.kCGWindowLayer) != 0:
                continue
            b = w.get(Quartz.kCGWindowBounds) or {}
            rect = ScreenRect(float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0)))
            if rect.w <= 1 or rect.h <= 1:
                continue
            info = WindowInfo(pid, rect, bool(w.get(Quartz.kCGWindowIsOnscreen, False)))
            if best is None or rect.w * rect.h > best.bounds.w * best.bounds.h:
                best = info
        return best

    def frontmost_pid(self) -> int | None:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None

    def activate(self, pid: int) -> None:
        from AppKit import NSApplicationActivateAllWindows, NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(NSApplicationActivateAllWindows)
