"""ゲームの代わりに Player.log へ [YOMAWARI] 行を書くモック。

Unity と同じく -yomawari があるときだけ [YOMAWARI] 行を出し、-logFile のパスに書く。
--mock-mode で振る舞いを切り替える:
  normal     boot の後ハートビートを出し続ける（SIGTERM で終了コード0）
  crash      --mock-after 秒後に終了コード3で落ちる
  hang       --mock-after 秒後にハートビートを止める（プロセスは生きたまま）
  noboot     何も出さずに生き続ける
  exception  boot の直後に例外（Player.log の本文と log_error イベント）を出し、その後は normal と同じ
  menus      boot の後に resolution とタイトルの menu を出し、--mock-after 秒後に設定画面（モーダル）を開く
  screen     boot の後に、起動の引数（-screen-width / -screen-height）の大きさで resolution を出す。--mock-resize があれば
             --mock-after 秒後にその大きさ（「WxH」）に変わる。--mock-ignore-screen なら引数を無視して 1280x720 のウィンドウのまま。
             -screen-fullscreen 1 ならフルスクリーン（大きさの引数がなければ 1280x720）
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
p.add_argument("-screen-width", type=int, default=1280)
p.add_argument("-screen-height", type=int, default=720)
p.add_argument("-screen-fullscreen")
p.add_argument("--mock-resize")
p.add_argument("--mock-ignore-screen", action="store_true")
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
        "MyGame.Scenario.Runner:Step () (at Assets/Scripts/Runner.cs:42)\n"
        "UnityEngine.EventSystems.ExecuteEvents:Execute (UnityEngine.EventSystems.IPointerClickHandler,UnityEngine.EventSystems.BaseEventData)\n"
        "\n"
        "(Filename: Assets/Scripts/Runner.cs Line: 42)\n"
        "\n"
    )
    # 実機の YomawariLog は logMessageReceived から log_warning / log_error を出す
    emit("log_warning", message="Scenario script 'main/s1' at line #12.0: Value 3 is out of range")
    emit(
        "log_error",
        type="Exception",
        message="NullReferenceException: Object reference not set to an instance of an object",
        stack="MyGame.Scenario.Runner:Step () (at Assets/Scripts/Runner.cs:42)\n",
    )
if a.mock_mode == "menus":
    emit("resolution", w=2560, h=1440, mode="Windowed")
    emit("menu", name="TitleUI", modal=False, buttons=[
        {"name": "NewGameButton", "rect": [0.1, 0.5, 0.3, 0.6]},
        {"name": "SettingsButton", "rect": [0.1, 0.7, 0.3, 0.8]},
    ])
if a.mock_mode == "screen":
    full = a.screen_fullscreen == "1" and not a.mock_ignore_screen
    w, h = (1280, 720) if a.mock_ignore_screen else (a.screen_width, a.screen_height)
    emit("resolution", w=w, h=h, mode="FullScreenWindow" if full else "Windowed")
opened = False
while True:
    elapsed = time.monotonic() - t0
    if a.mock_mode == "screen" and a.mock_resize and not opened and elapsed >= a.mock_after:
        opened = True
        rw, rh = (int(v) for v in a.mock_resize.split("x"))
        emit("resolution", w=rw, h=rh, mode="Windowed")
    if a.mock_mode == "menus" and not opened and elapsed >= a.mock_after:
        opened = True
        emit("menu", name="SettingsUI", modal=True, buttons=[{"name": "ReturnButton", "rect": [0.8, 0.9, 0.95, 0.98]}])
    if a.mock_mode == "crash" and elapsed >= a.mock_after:
        sys.exit(3)
    if not (a.mock_mode == "hang" and elapsed >= a.mock_after):
        emit("heartbeat", fps=60, gc_mem=12345678)
    time.sleep(a.mock_hb)
