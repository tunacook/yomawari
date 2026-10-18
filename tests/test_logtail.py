import asyncio
from pathlib import Path

from yomawari.sensors.logtail import InvalidRecord, LogParser, UnityEntry, YomawariRecord, tail_file


def parse_all(data: bytes, chunk: int | None = None) -> list:
    p = LogParser()
    out = []
    if chunk is None:
        out += p.feed(data)
    else:
        for i in range(0, len(data), chunk):
            out += p.feed(data[i : i + chunk])
    out += p.feed(b"\n") + p.flush()
    return out


def test_sample_log_records(sample_log: Path):
    recs = parse_all(sample_log.read_bytes())
    yom = [r for r in recs if isinstance(r, YomawariRecord)]
    assert [r.data["ev"] for r in yom] == ["boot", "heartbeat", "line", "heartbeat", "heartbeat"]
    assert [r.data["seq"] for r in yom] == [0, 1, 2, 3, 5]
    line = yom[2].data
    assert line["speaker"] == "レイチェル"
    assert line["text"] == "「おはよう。\"今日\"も\\n晴れね」"

    invalid = [r for r in recs if isinstance(r, InvalidRecord)]
    assert len(invalid) == 1 and '"seq":4' in invalid[0].raw

    entries = [r for r in recs if isinstance(r, UnityEntry)]
    by_msg = {e.message: e for e in entries}
    exc = by_msg["NullReferenceException: Object reference not set to an instance of an object"]
    assert exc.has_stack
    assert exc.stack[0] == "Rachel.Scenario.Runner:Step () (at Assets/Scripts/Runner.cs:42)"
    assert exc.stack[-1].startswith("(Filename:")
    assert by_msg["Save slot 1 not found"].has_stack
    assert not by_msg["Loading scene Title"].has_stack
    assert not by_msg["Just a normal info log without stack"].has_stack
    assert not by_msg["Initialize engine version: 2022.3.20f1 (61c2feb0970d)"].has_stack


def test_chunked_feed_is_same(sample_log: Path):
    """1バイトずつ届いても（UTF-8 の途中で切れても）同じ結果になる。"""
    data = sample_log.read_bytes()
    whole = parse_all(data)
    for size in (1, 7, 64):
        assert parse_all(data, size) == whole


def test_crlf_and_trailing_partial_line():
    p = LogParser()
    recs = p.feed(b'[YOMAWARI] {"seq":0,"ev":"boot"}\r\n[YOMAWARI] {"seq":1,')
    assert len(recs) == 1 and recs[0].data["ev"] == "boot"
    recs = p.feed(b'"ev":"heartbeat"}\n')
    assert recs[0].data["seq"] == 1


def test_tail_file_follows_appends_and_truncation(tmp_path: Path):
    path = tmp_path / "player.log"
    got: list = []

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(tail_file(path, got.extend, stop, 0.01, 0.05))
        await asyncio.sleep(0.05)  # まだファイルがない
        with path.open("ab") as fp:
            fp.write(b'[YOMAWARI] {"seq":0,"ev":"boot"}\n[YOMAWARI] {"seq":1,"ev":"heart')
        await asyncio.sleep(0.1)
        assert [r.data["ev"] for r in got] == ["boot"]
        with path.open("ab") as fp:
            fp.write(b'beat"}\nSome message\n')
        await asyncio.sleep(0.15)  # flush_after 経過で保留中のエントリが確定する
        path.write_bytes(b'[YOMAWARI] {"seq":0,"ev":"boot"}\n')  # 上書き起動
        await asyncio.sleep(0.1)
        stop.set()
        await task

    asyncio.run(scenario())
    kinds = [r.data["ev"] if isinstance(r, YomawariRecord) else r.message for r in got]
    assert kinds == ["boot", "heartbeat", "Some message", "boot"]


def test_unity6_with_log_stacks(fixtures_dir=Path(__file__).parent / "fixtures"):
    """Log にもスタックを付ける設定（Unity 6、Filename 行なし）の Player.log。"""
    recs = parse_all((fixtures_dir / "player_unity6_log_stacks.log").read_bytes())
    yom = [r.data for r in recs if isinstance(r, YomawariRecord)]
    assert [d["ev"] for d in yom] == ["boot", "log_error", "heartbeat"]
    assert yom[1]["type"] == "Exception" and yom[1]["stack"].startswith("Game.Scenario.Runner:Step()")
    entries = {e.message: e for e in recs if isinstance(e, UnityEntry)}
    # [YOMAWARI] 行に付いたスタックは読み飛ばし、エントリにしない
    assert not any("YomawariLog:Emit" in m or m.startswith("UnityEngine.DebugLogHandler") for m in entries)
    panel = entries["[Panel] SetVisibility(False): char=(null)"]
    assert panel.stack == [
        "UnityEngine.DebugLogHandler:Internal_Log(LogType, LogOption, String, Object)",
        "Game.PanelUI:SetVisibility(Boolean)",
        "Game.UI.CustomUI:Awake()",
    ]
    assert entries["NullReferenceException: Object reference not set to an instance of an object"].has_stack
    assert "Input System module state changed to: Shutdown." in entries
