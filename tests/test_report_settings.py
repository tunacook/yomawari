"""レポートの「設定」の節（post/settings.py）。"""

from yomawari.post.settings import render


def _settings(launch, **values):
    base = {"locale": "ja-JP", "locales": ["ja-JP", "en-US"], "reveal_speed": 0.5, "auto_delay": 0.5,
            "master_volume": 1, "resolution": "1280x720", "font": None}
    return {"source": "game", "kind": "settings", "launch": launch, "data": {**base, **values}}


def _row(lines, key):
    return next(line for line in lines if f"| `{key}` |" in line)


def test_settings_section_with_profile():
    events = [
        _settings(0),
        _settings(0, reveal_speed=1),
        _settings(0, reveal_speed=1, resolution="1600x900"),
        _settings(1, reveal_speed=0.5, resolution="1600x900"),  # 起動し直したあとの最初の値は、変わったと数えない
        _settings(1, auto_delay=1, resolution="1600x900"),
    ]
    out = render(events, ["locale", "reveal_speed", "resolution", "quality"])
    assert out[0] == "## 設定"
    assert _row(out, "reveal_speed") == "| 本文の出る速さ | `reveal_speed` | 変えさせる | 1 回 | `0.5` |"
    assert _row(out, "resolution") == "| 解像度 | `resolution` | 変えさせる | 1 回 | `1600x900` |"
    assert _row(out, "auto_delay") == "| オートの待ち時間 | `auto_delay` | 外した | 1 回 | `1` |"
    assert _row(out, "quality") == "| 画質 | `quality` | 変えさせる | 値が出ていない | — |"  # 古いパッケージ
    assert _row(out, "font") == "| フォント | `font` | 外した | 0 回 | — |"
    assert not any("`locales`" in line for line in out)
    assert "- 変えさせる項目のうち、このランで一度も変わらなかったもの: `locale`" in out
    assert any(line.startswith("- 外した項目なのに、このランで変わったもの: `auto_delay`") for line in out)


def test_settings_section_without_profile_or_settings():
    out = render([_settings(0), _settings(0, master_volume=0)], None)
    assert any("プロファイルに書いていないので" in line for line in out)
    assert _row(out, "master_volume") == "| 全体の音量 | `master_volume` | — | 1 回 | `0` |"
    assert not any(line.startswith("- ") for line in out)
    assert render([{"source": "game", "kind": "line", "data": {}}], ["locale"]) == []
