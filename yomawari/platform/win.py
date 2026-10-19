"""Windows 対応はフェーズ7（後日）。差し替え口だけ用意しておく。"""

from __future__ import annotations


class WinPlatform:
    name = "win"

    def __getattr__(self, item: str):
        raise NotImplementedError("Windows 対応は未実装です（DESIGN.md フェーズ7）")
