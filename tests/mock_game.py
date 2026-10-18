"""ゲームの代わりに Player.log へ [YOMAWARI] 行を書くモック。

Unity と同じく -yomawari があるときだけ [YOMAWARI] 行を出し、-logFile のパスに書く。
--mock-mode で振る舞いを切り替える:
  normal     boot の後ハートビートを出し続ける（SIGTERM で終了コード0）
  crash      --mock-after 秒後に終了コード3で落ちる
  hang       --mock-after 秒後にハートビートを止める（プロセスは生きたまま）
  noboot     何も出さずに生き続ける
  exception  boot の直後に例外とスタックトレースを出し、その後は normal と同じ
"""

import argparse
import json
import signal
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("-logFile")
p.add_argument("-yomawari", action="store_true")
p.add_argument("--mock-mode", default="normal")
p.add_argument("--mock-after", type=float, default=1.0)
p.add_argument("--mock-hb", type=float, default=0.2)
a, _ = p.parse_known_args()

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
out = open(a.logFile, "w", encoding="utf-8", buffering=1) if a.logFile else sys.stdout
t0 = time.monotonic()
seq = 0


def emit(ev, **body):
    global seq
    if not a.yomawari:
        return
    rec = {"seq": seq, "t": round(time.monotonic() - t0, 3), "f": seq, "ev": ev, **body}
    seq += 1
    out.write("[YOMAWARI] " + json.dumps(rec, ensure_ascii=False) + "\n")


out.write("Mono path[0] = '/mock/Data/Managed'\nInitialize engine version: 2022.3.0f1\n")
if a.mock_mode == "noboot":
    while True:
        time.sleep(1)

emit("boot", version="0.0.1-mock", build_type="development", build_time="2026-10-18T00:00:00")
emit("scene", name="Title")
if a.mock_mode == "exception":
    out.write(
        "NullReferenceException: Object reference not set to an instance of an object\n"
        "Rachel.Scenario.Runner:Step () (at Assets/Scripts/Runner.cs:42)\n"
        "UnityEngine.EventSystems.ExecuteEvents:Execute (UnityEngine.EventSystems.IPointerClickHandler,UnityEngine.EventSystems.BaseEventData)\n"
        "\n"
        "(Filename: Assets/Scripts/Runner.cs Line: 42)\n"
        "\n"
    )
while True:
    elapsed = time.monotonic() - t0
    if a.mock_mode == "crash" and elapsed >= a.mock_after:
        sys.exit(3)
    if not (a.mock_mode == "hang" and elapsed >= a.mock_after):
        emit("heartbeat", fps=60, gc_mem=12345678)
    time.sleep(a.mock_hb)
