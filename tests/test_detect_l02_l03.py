"""L-02（フォントにない文字）・L-03（リソースの読み込みに失敗）と、そのログを L-01・L-06 で数えないこと。"""

from yomawari.detect.engine import Engine
from yomawari.events import Event

GLYPH = ("The character with Unicode value \\u3042 was not found in the [Sans SDF] font asset or any potential "
         "fallbacks. It was replaced by Unicode character \\u25A1 in text object [NameText].")


def ev(kind, t, **data):
    return Event("game", kind, data, launch=0, t=t, wall=f"w{t}")


def feed(e, events):
    out = []
    for x in events:
        out += e.feed(x)
    return out


def test_l02_missing_glyph_per_font_and_char(repo_rules):
    e = Engine({k: repo_rules[k] for k in ("L-02", "L-06")})
    issues = feed(e, [ev("log_warning", 1, message=GLYPH), ev("log_warning", 2, message=GLYPH),
                      ev("log_warning", 3, message=GLYPH.replace("3042", "3044"))])
    assert [i.rule_id for i in issues] == ["L-02"] * 3  # L-06 では数えない
    assert issues[0].detail == "フォント Sans SDF にない文字「あ」（U+3042）を表示しようとしました（テキスト NameText）"
    assert issues[0].key == issues[1].key != issues[2].key


def test_l03_resource_load_error_is_not_l01(repo_rules):
    e = Engine({k: repo_rules[k] for k in ("L-01", "L-03")})
    msg = ("Async exception: Naninovel.Error: Failed to load 'chapter/side_story' resource of type "
           "'Naninovel.Script': make sure the resource is registered with a resource provider.")
    issues = feed(e, [ev("log_error", 1, type="Error", message=msg, stack="at X")])
    assert [(i.rule_id, i.severity, i.evidence["name"], i.evidence["type"]) for i in issues] == [
        ("L-03", "major", "chapter/side_story", "Naninovel.Script")]
    assert issues[0].detail == "リソースを読み込めませんでした: chapter/side_story（Naninovel.Script）（エラー）"


def test_l03_game_warning_and_missing_script(repo_rules):
    e = Engine({k: repo_rules[k] for k in ("L-03", "L-06")})
    issues = feed(e, [
        ev("log_warning", 1, message="Failed to load background texture: street_01"),
        ev("log_warning", 2, message="The referenced script (Unknown) on this Behaviour (Game Object 'Panel') is missing!"),
    ])
    assert [(i.rule_id, i.evidence["name"]) for i in issues] == [("L-03", "street_01"), ("L-03", "Unknown")]


def test_other_logs_still_go_to_l01_l06(repo_rules):
    e = Engine({k: repo_rules[k] for k in ("L-01", "L-02", "L-03", "L-06")})
    issues = feed(e, [ev("log_error", 1, type="Exception", message="NullReferenceException: Object reference"),
                      ev("log_warning", 2, message="Something else happened")])
    assert [i.rule_id for i in issues] == ["L-01", "L-06"]


def test_without_l02_l03_rules_logs_stay_in_l01_l06(repo_rules):
    e = Engine({k: repo_rules[k] for k in ("L-06",)})
    assert [i.rule_id for i in feed(e, [ev("log_warning", 1, message=GLYPH)])] == ["L-06"]
