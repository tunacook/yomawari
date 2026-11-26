"""ゲームが教えるキーの名前（Unity の Input System の名前）を、入力の送り手（InputDriver）のキーの名前にする。

パッケージは、Naninovel の入力に割り当てられたキーボードのキーを、Input System の名前（"ctrl"・"leftShift"・"tab"・"a"）で
出す（input イベントの skip_keys など。DESIGN.md §3）。ここでは、それを pyautogui のキーの名前にする。
分からない名前は None（そのキーは使わない）。
"""

from __future__ import annotations

NAMES = {
    "ctrl": "ctrl", "leftCtrl": "ctrlleft", "rightCtrl": "ctrlright",
    "shift": "shift", "leftShift": "shiftleft", "rightShift": "shiftright",
    "alt": "alt", "leftAlt": "altleft", "rightAlt": "altright",
    "tab": "tab", "space": "space", "enter": "enter", "numpadEnter": "enter", "escape": "esc",
    "backspace": "backspace", "upArrow": "up", "downArrow": "down", "leftArrow": "left", "rightArrow": "right",
}


def driver_key(name: str) -> str | None:
    """Input System のキーの名前 → 入力の送り手のキーの名前。"""
    if name in NAMES:
        return NAMES[name]
    if len(name) == 1 and name.isascii() and name.isalnum():
        return name.lower()
    if name.startswith("digit") and name[5:].isdigit() and len(name) == 6:
        return name[5:]
    if name[:1] == "f" and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return name
    return None
