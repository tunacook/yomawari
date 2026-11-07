"""ボタンの見た目から、押しても何も起きなさそうなものを見分ける（DESIGN.md §9「画像による推定」）。

ゲームのボタンは、表示はしたまま、明るさで「今は押せない」ことを表すことが多い（回想一覧のまだ開いていない
スロット、セーブがないときの「つづきから」など）。人がそれを見て察するのと同じく、同じ画面（menu）の中で
同じ種類に見えるボタン（同じ名前か、同じ大きさ）が min_group 個以上並ぶとき、画面の明るさを比べて、
いちばん明るいものより明らかに暗いものを「暗い」とする。比べる相手には、スクロールする前に見た同じ並びの
いちばん明るいものも含める（見えている分がすべて暗いときにも、暗いと分かるように）。

暗いボタンは押す順番を後回しにするだけで、押さなくはしない。押せないのが仕様かどうかは、押した結果で分かる。
文字で「空」と分かるボタン（"Empty" など。rules.yaml の inactive_hints）も、同じく後回しにする。
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import yaml

from ..sensors.screen import Shot, brightness, vividness
from ..state import Target

# 同じ大きさとみなす、幅・高さの差（描画領域に対する割合）
_SIZE_STEP = 0.01

Values = dict[str, tuple[float, float]]  # label → (平均の明るさ, 明るい部分の明るさ)


def groups(targets: list[Target], min_group: int = 3) -> dict[str, list[Target]]:
    """同じ種類に見えるボタンの並び。キーは同じ名前なら "<menu>/<name>"、同じ大きさなら "<menu>/（WxH のボタン）"。"""
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


def measure(targets: list[Target], shot: Shot, min_group: int = 3) -> dict[str, Values]:
    """並びごとに、ボタンの明るさを測る。測れたボタンが min_group 個に満たない並びは除く。"""
    out: dict[str, Values] = {}
    for key, ts in groups(targets, min_group).items():
        values = {t.label: v for t in ts if (v := brightness(shot, t.rect)) is not None}
        if len(values) >= min_group:
            out[key] = values
    return out


def disabled_looks(targets: list[Target], labels: set[str] | list[str], shot: Shot, fade_ratio: float,
                   grey_ratio: float, min_gap: float) -> dict[str, str]:
    """明るさでは差がない並びで、押せない（disabled）ときの一般的な見せ方をしているボタン → 見せ方（faded / grey）。

    UI デザインの定石では、押せないボタンは「暗くする」（brightness で見る）のほかに、「不透明度を下げる」（後ろが透けて、
    濃淡と鮮やかさがどちらも小さくなる: faded）、「色を抜いて灰色にする」（濃淡は残るが、鮮やかさがなくなる: grey）で表す。
    並びでいちばん大きいものと比べ、faded は濃淡と鮮やかさのどちらも fade_ratio 倍より小さいもの、grey は鮮やかさが
    grey_ratio 倍より小さいもの。どちらも差が min_gap 以上のときだけ。並びがもともと色のないボタンなら grey とはしない。"""
    rects = {t.label: t.rect for t in targets}
    vals = {label: v for label in labels if label in rects and (v := vividness(shot, rects[label])) is not None}
    if len(vals) < 3:
        return {}
    top_c = max(v[0] for v in vals.values())
    top_s = max(v[1] for v in vals.values())
    out: dict[str, str] = {}
    for label, (c, s) in vals.items():
        if c < top_c * fade_ratio and top_c - c >= min_gap and s < top_s * fade_ratio and top_s - s >= min_gap:
            out[label] = "faded"
        elif s < top_s * grey_ratio and top_s - s >= min_gap:
            out[label] = "grey"
    return out


def dark(values: Values, ref: tuple[float, float], ratio: float, min_gap: float) -> set[str]:
    """暗いボタン: 平均か明るい部分のどちらかが、ref（並びでいちばん明るいもの）の ratio 倍より暗く、差が min_gap 以上。"""
    top_mean, top_peak = ref
    return {
        label for label, (mean, peak) in values.items()
        if (mean < top_mean * ratio and top_mean - mean >= min_gap)
        or (peak < top_peak * ratio and top_peak - peak >= min_gap)
    }


def alike(a: tuple[float, float], b: tuple[float, float], ratio: float, min_gap: float) -> bool:
    """同じような明るさ: どちらも、もう一方と比べて暗い（dark）とはならない。"""
    return not dark({"a": a}, b, ratio, min_gap) and not dark({"b": b}, a, ratio, min_gap)


def brighter(now: tuple[float, float], before: tuple[float, float], ratio: float, min_gap: float) -> bool:
    """now が before よりはっきり明るい: 平均と明るい部分のどちらでも、before が now と比べて暗い（dark の基準）。
    片方だけでは、後ろの絵の揺れで明るく見えることがあるので、両方を見る。"""
    (bm, bp), (nm, np_) = before, now
    return bm < nm * ratio and nm - bm >= min_gap and bp < np_ * ratio and np_ - bp >= min_gap


def letters(text: str | None, masked_chars: str = "") -> int:
    """読める文字（伏せ字の文字を除いた、英字・かな・漢字など）の数。数字・記号・空白は数えない。"""
    return sum(1 for c in text or "" if c.isalpha() and c not in masked_chars)


def brightest(values: Values) -> tuple[float, float]:
    return max(v[0] for v in values.values()), max(v[1] for v in values.values())


# --- 文字で「空」と分かるボタン ---


def load_inactive_words(path: Path) -> list[str]:
    """rules.yaml の inactive_hints.text（空のスロットなどを表す語）。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    words = ((doc.get("inactive_hints") or {}).get("text")) or []
    return [str(w) for w in words if w]


def load_masked_chars(path: Path) -> str:
    """rules.yaml の inactive_hints.masked（伏せ字の文字）。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    chars = ((doc.get("inactive_hints") or {}).get("masked")) or []
    return "".join(str(c) for c in chars if c)


def masked(text: str | None, chars: str) -> bool:
    """伏せ字（「？？？」）。番号（「No.021 ？？？」）は除いて見る。

    英数字・空白・番号に使う記号を除いた残りが伏せ字だけ、または伏せ字が2文字以上あって残りの半分以上を占める
    （OCR が「？」の一部を別の文字に読み違えることがあるため）。題名の終わりの「？」1文字は伏せ字としない。"""
    if not chars or not text:
        return False
    body = [c for c in text if not (c.isascii() and (c.isalnum() or c.isspace() or c in ".#:-_/()[]")) and not c.isspace()]
    n = sum(1 for c in body if c in chars)
    return bool(body) and (n == len(body) or (n >= 2 and n * 2 >= len(body)))


def inactive(targets: list[Target], words: list[str], masked_chars: str = "") -> set[str]:
    """文字が「空」を表す語を含む、または伏せ字だけのボタンの label。英数字の語は単語として照らし合わせる（大文字小文字は区別しない）。"""
    if not words and not masked_chars:
        return set()
    return {t.label for t in targets if t.kind == "button" and text_inactive(t.text, words, masked_chars)}


def text_inactive(text: str | None, words: list[str], masked_chars: str = "") -> bool:
    """文字が「空」を表す語を含む、または伏せ字だけでできている。"""
    return bool(text) and (any(_match(w, text) for w in words) or masked(text, masked_chars))


def _match(word: str, text: str) -> bool:
    if word.isascii():
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return word in text
