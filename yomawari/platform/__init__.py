"""OS 固有の差分。上位層は get_platform() の戻り値だけを使い、OS を意識しない。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..sensors.window import WindowInfo


@dataclass
class GameIdentity:
    """Unity の Company Name / Product Name。保存場所の推定に使う。"""

    company: str | None
    product: str | None
    bundle_id: str | None = None


@dataclass
class ExecCheck:
    ok: bool
    messages: list[tuple[str, str]]  # (level: ok/warn/error, message)


class Platform(Protocol):
    name: str

    def resolve_executable(self, build_path: Path) -> Path: ...
    def check_executable(self, exe: Path) -> ExecCheck: ...
    def guess_identity(self, build_path: Path) -> GameIdentity: ...
    def default_save_dir(self, ident: GameIdentity) -> Path | None: ...
    def default_prefs_domain(self, ident: GameIdentity) -> str | None: ...
    def crash_dir(self) -> Path: ...
    def crash_suffixes(self) -> tuple[str, ...]: ...
    def prefs_export(self, domain: str, dest: Path) -> bool: ...
    def prefs_replace(self, domain: str, src: Path | None) -> None: ...
    def check_permissions(self) -> dict[str, bool | None]: ...
    def permission_help(self, missing: list[str]) -> list[str]: ...
    def request_permissions(self, missing: list[str]) -> None: ...
    def window_info(self, pid: int) -> WindowInfo | None: ...
    def frontmost_pid(self) -> int | None: ...
    def activate(self, pid: int) -> None: ...
    def open_file(self, path: Path) -> None: ...


def get_platform() -> Platform:
    if sys.platform == "darwin":
        from .mac import MacPlatform

        return MacPlatform()
    if sys.platform == "win32":
        from .win import WinPlatform

        return WinPlatform()
    raise NotImplementedError(f"未対応の OS です: {sys.platform}")
