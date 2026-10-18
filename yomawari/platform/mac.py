"""macOS 固有の処理: .app の解決、実行形式の確認、保存場所、PlayerPrefs、権限確認。"""

from __future__ import annotations

import ctypes
import os
import plistlib
import struct
import subprocess
from pathlib import Path

from . import ExecCheck, GameIdentity

_CPU_ARM64 = 0x0100000C
_CPU_X86_64 = 0x01000007
_CPU_NAMES = {_CPU_ARM64: "arm64", _CPU_X86_64: "x86_64"}

HOME = Path.home()


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

    def open_file(self, path: Path) -> None:
        subprocess.run(["open", str(path)], check=False)
