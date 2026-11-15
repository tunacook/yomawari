"""V-08（文字の速さの設定が本文に効いていない）・V-09（言語の設定が本文に効いていない）。"""

from yomawari.detect.engine import Engine
from yomawari.events import Event


def ev(kind, t, source="game", **data):
    return Event(source, kind, data, launch=0, t=t, wall=f"w{t}")


def feed(e, events):
    out = []
    for x in events:
        out += e.feed(x)
    return out


TEXT = "<b>ゆっくり</b>と本文が出てくる行です"  # タグを除いて 15 文字


def lines(speed, ms_per_char, n, t0, op_every=0):
    """reveal_speed を speed にしてから、1文字あたり ms_per_char で表示し終わる行を n 行。"""
    out = [ev("settings", t0, reveal_speed=speed, locale="ja-JP")]
    t = t0 + 1
    for i in range(n):
        out.append(ev("line", t, line_id=f"s:{i}", text=TEXT))
        if op_every and i % op_every == 0:
            out.append(ev("op", t + 0.001, source="harness", label="advance"))
        out.append(ev("line_done", t + 15 * ms_per_char / 1000, line_id=f"s:{i}"))
        t += 5
    return out


def test_v08_faster_setting_is_faster(repo_rules):
    e = Engine({"V-08": repo_rules["V-08"]})
    assert feed(e, lines(0, 17, 12, 0) + lines(0.5, 8, 12, 100) + lines(1, 0, 12, 200)) == []


def test_v08_setting_not_effective(repo_rules):
    e = Engine({"V-08": repo_rules["V-08"]})
    issues = feed(e, lines(0, 10, 12, 0) + lines(1, 10, 12, 100))
    assert [(i.rule_id, i.severity, i.evidence["slow"], i.evidence["fast"]) for i in issues] == [("V-08", "major", 0, 1)]


def test_v08_ignores_lines_with_harness_ops_and_instant_slow_side(repo_rules):
    # 遅い値の行がどれもハーネスの操作で飛ばされている: 比べる行がたまらない
    e = Engine({"V-08": repo_rules["V-08"]})
    assert feed(e, lines(0, 10, 12, 0, op_every=1) + lines(1, 10, 12, 100)) == []
    # 遅い値でも、もともと速い（スキップ中など）: 判定しない
    e = Engine({"V-08": repo_rules["V-08"]})
    assert feed(e, lines(0, 0.5, 12, 0) + lines(1, 0.5, 12, 100)) == []


def _lang(locale, texts, t0):
    return [ev("settings", t0, locale=locale)] + [ev("line", t0 + i + 1, line_id=f"l:{i}", text=x)
                                                  for i, x in enumerate(texts)]


EN = ["This is the first line.", "And this is the second.", "Third line of text here.", "Fourth one.", "Fifth line."]
JA = ["一行目の本文です。", "二行目の本文です。", "三行目です。", "四行目の本文。", "五行目の本文。"]


def test_v09_locale_effective(repo_rules):
    e = Engine({"V-09": repo_rules["V-09"]})
    assert feed(e, _lang("ja-JP", JA, 0) + _lang("en-US", EN, 10) + _lang("ja-JP", JA, 20)) == []


def test_v09_locale_not_effective(repo_rules):
    e = Engine({"V-09": repo_rules["V-09"]})
    issues = feed(e, _lang("ja-JP", JA, 0) + _lang("en-US", JA, 10))
    assert [(i.rule_id, i.evidence["locale"]) for i in issues] == [("V-09", "en-US")]


def test_v09_few_mixed_lines_and_unknown_locale_are_ok(repo_rules):
    e = Engine({"V-09": repo_rules["V-09"]})
    mixed = JA[:3] + ["DM", "Some English words here"]  # 短すぎる行は数えず、1 行だけ違う
    assert feed(e, _lang("ja-JP", mixed, 0)) == []
    e = Engine({"V-09": repo_rules["V-09"]})
    assert feed(e, _lang("xx-YY", JA, 0)) == []  # 見込みを書いていない言語
