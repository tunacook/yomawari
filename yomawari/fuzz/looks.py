"""ボタンの見た目から、押しても何も起きなさそうなものを見分ける（DESIGN.md §9「画像による推定」）。

ゲームのボタンは、表示はしたまま、明るさで「今は押せない」ことを表すことが多い（回想一覧のまだ開いていない
スロット、セーブがないときの「つづきから」など）。人がそれを見て察するのと同じく、同じ画面（menu）の中で
同じ種類に見えるボタン（同じ名前か、同じ大きさ）が min_group 個以上並ぶとき、画面の明るさを比べて、
いちばん明るいものより明らかに暗いものを「暗い」とする。

暗いボタンは押す順番を後回しにするだけで、押さなくはしない。押せないのが仕様かどうかは、押した結果で分かる。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..sensors.screen import Shot, brightness
from ..state import Target

# 同じ大きさとみなす、幅・高さの差（描画領域に対する割合）
_SIZE_STEP = 0.01


@dataclass(frozen=True)
class DimGroup:
    """暗いボタンがあった並び（記録用）。"""

    key: str  # 同じ名前なら "<menu>/<name>"、同じ大きさなら "<menu>/（同じ大きさのボタン）"
    total: int
    dim: int


def _groups(targets: list[Target], min_group: int) -> dict[str, list[Target]]:
    by_name: dict[str, list[Target]] = defaultdict(list)
    by_size: dict[tuple[str, int, int], list[Target]] = defaultdict(list)
    for t in targets:
        if t.kind != "button" or not t.visible:
            continue
        by_name[t.key].append(t)
        w, h = t.rect[2] - t.rect[0], t.rect[3] - t.rect[1]
        by_size[(t.menu, round(w / _SIZE_STEP), round(h / _SIZE_STEP))].append(t)
    out = {k: ts for k, ts in by_name.items() if len(ts) >= min_group}
    named = {t.label for ts in out.values() for t in ts}
    for (menu, w, h), ts in by_size.items():
        # 同じ名前の並びにすでに入っているボタンだけの組は除く（同じ並びを2回数えない）
        if len(ts) >= min_group and not all(t.label in named for t in ts):
            out[f"{menu}/（{w * _SIZE_STEP:.2f}x{h * _SIZE_STEP:.2f} のボタン）"] = ts
    return out


def dim_buttons(
    targets: list[Target], shot: Shot, ratio: float, min_gap: float, min_group: int = 3,
) -> tuple[set[str], list[DimGroup]]:
    """暗いボタンの label の集合と、暗いものがあった並び。

    暗い = 平均の明るさか、明るい部分の明るさのどちらかが、同じ並びでいちばん明るいものの ratio 倍より暗く、
    差が min_gap 以上。
    """
    dim: set[str] = set()
    found: list[DimGroup] = []
    for key, ts in _groups(targets, min_group).items():
        values = {t.label: v for t in ts if (v := brightness(shot, t.rect)) is not None}
        if len(values) < min_group:
            continue
        top_mean = max(v[0] for v in values.values())
        top_peak = max(v[1] for v in values.values())
        dark = {
            label for label, (mean, peak) in values.items()
            if (mean < top_mean * ratio and top_mean - mean >= min_gap)
            or (peak < top_peak * ratio and top_peak - peak >= min_gap)
        }
        if dark:
            dim |= dark
            found.append(DimGroup(key, len(values), len(dark)))
    return dim, found
