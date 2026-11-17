"""設定を変えに行く（DESIGN.md §9「設定を変えに行く」）。

人が遊ぶときのように、ときどき設定画面を開いて値を変え、そのあとも遊び続けて壊れないかを見る。ランダムに押すだけでは、
設定画面を開く回数が少なく（タイトル画面のサブメニューの奥など）、まだ一度も変えていない設定が残りやすいので、
決まった間隔で「この設定を変えに行く」という目標を立てる。

- どのボタンが設定を変えるかは、押して settings の値だけが変わったかで覚えたもの（経験の "settings"）を使う。
  プロファイルには書かない（§9「プロファイルの言語」）
- 変えに行く設定は、このランで変わった回数がいちばん少ないもの。押せば今と違う値になるボタンを先に押す
- 設定の画面が出ていなければ、その画面を開いた操作の並び（入り口ごとに覚えたもの）を押しにいく。右クリックメニューの
  ように、いちばん外側の画面を右クリック・キーで開いたなら、その操作も押しにいく
- 画面モード・解像度・画質（GUARDED_KEYS）を変えるボタンは、ガードがイレギュラー操作でだけ押すもの（役割 system_setting）
  なので、ふだんは押さず、変えた値を覚えていないことが多い。プロファイルの [settings] offered にあれば、変えに行くときだけ
  押しにいく（まだ何を変えるか覚えていないボタンも押す）。変わったら、そのまま元の値（そのランで最初に見た値）に戻しにいく。
  ウィンドウの大きさが変わったままだと、ボタンが画面の外に出るなど、ほかの検出の邪魔になるため
- 押すとほかの設定もまとめて変わると分かったボタン（設定のリセットなど）は、ほかに押すものがないときだけ押す
- 区間（ガチャプレイ・ボタンの網羅）、ゲームの流れの途中、言語を戻しているあいだは、変えに行かない
- 決めた時間のうちに変えられなければ、あきらめて「ハーネスの記録」に書く
"""

from __future__ import annotations

import dataclasses
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .ops import Op


# Naninovel の標準の設定画面にある項目: パッケージの settings の値の名前 → レポートに出す名前（並びはレポートの表の順）。
# ゲームがどれをプレイヤーに変えさせるかは、プロファイルの [settings] offered に書く（DESIGN.md §9「設定を変えに行く」）
SETTING_ITEMS = {
    "locale": "言語",
    "reveal_speed": "本文の出る速さ",
    "auto_delay": "オートの待ち時間",
    "skip_mode": "スキップの方式",
    "master_volume": "全体の音量",
    "bgm_volume": "BGM の音量",
    "sfx_volume": "効果音の音量",
    "voice_volume": "ボイスの音量",
    "voice_locale": "ボイスの言語",
    "font": "フォント",
    "font_size": "文字の大きさ",
    "quality": "画質",
    "screen_mode": "画面モード",
    "resolution": "解像度",
}


# ガードがイレギュラー操作でだけ押すボタン（役割 system_setting）で変わる設定
GUARDED_KEYS = frozenset({"screen_mode", "resolution", "quality"})


def _menu(label: str) -> str:
    return label.split("/", 1)[0]


@dataclass
class SettingsTour:
    interval: float  # 変えに行く間隔（秒）。0 以下なら変えに行かない
    timeout: float  # 変えに行ってから、あきらめるまでの時間（秒）
    factor: float  # 設定を変えるボタンと、その画面を開くボタンの重みに掛ける
    next_t: float | None = None  # 次に変えに行く時刻（time.monotonic）
    target: str | None = None  # いま変えに行っている設定
    since: float = 0.0
    keys: set[str] = field(default_factory=set)  # ゲームが出した設定の名前
    changes: Counter[str] = field(default_factory=Counter)  # 設定 → このランで変わった回数（巡回のほかの押下も含む）
    tours: int = 0
    reached: Counter[str] = field(default_factory=Counter)  # 変えに行って変えた設定
    entries: Counter[str] = field(default_factory=Counter)  # 変えに行って変えたときの、設定の画面の入り口
    gave_up: Counter[str] = field(default_factory=Counter)  # 変えに行ったが、時間のうちに変えられなかった設定
    # プロファイルの [settings] offered: ゲームがプレイヤーに変えさせる設定。None（書いていない）なら、ゲームが出した設定すべて
    offered: frozenset[str] | None = None
    base: dict[str, Any] = field(default_factory=dict)  # GUARDED_KEYS の、このランで最初に見た値（変えたあと戻す値）
    restoring: bool = False  # 変えた GUARDED_KEYS の設定を、元の値に戻しにいっている
    restored: int = 0
    restore_failed: Counter[str] = field(default_factory=Counter)

    @property
    def enabled(self) -> bool:
        return self.interval > 0 and self.factor > 1.0

    def observe(self, settings: dict[str, Any] | None) -> None:
        if settings:
            self.keys |= {k for k in settings if k != "locales"}
            for k in GUARDED_KEYS & settings.keys():
                self.base.setdefault(k, settings[k])

    def changed(self, keys: set[str], entry: str | None, now: float, values: dict[str, Any] | None = None) -> None:
        """設定の値が変わった（どの押下でも）。変えに行っていた設定なら、変えに行くのを終える。
        画面モード・解像度・画質なら、元の値に戻しにいく（戻ったら終える）。"""
        self.changes.update(keys)
        if self.target is None or self.target not in keys:
            return
        value = (values or {}).get(self.target)
        if self.restoring:
            if value == self.base.get(self.target):
                self.restored += 1
                self._finish(now)
            return
        self.reached[self.target] += 1
        self.entries[entry or "（ゲームが開いた画面）"] += 1
        if self.target in GUARDED_KEYS and self.target in self.base and value != self.base[self.target]:
            self.restoring, self.since = True, now  # 同じ設定のまま、元の値に戻しにいく
            return
        self._finish(now)

    def _finish(self, now: float) -> None:
        self.target = None
        self.restoring = False
        self.next_t = now + self.interval

    def step(self, now: float, buttons: dict[str, dict[str, Any]], rng: random.Random,
             guarded: set[str] | frozenset[str] = frozenset()) -> None:
        """時間になったら変えに行く設定を選ぶ。変えに行っているあいだに時間が過ぎたら、あきらめる。
        guarded は、ガードがイレギュラー操作でだけ押す、画面の設定を変えるボタン（Guard.setting_labels）。"""
        if not self.enabled:
            return
        if self.next_t is None:
            self.next_t = now + self.interval
        if self.target is not None:
            if now - self.since >= self.timeout:
                (self.restore_failed if self.restoring else self.gave_up)[self.target] += 1
                self._finish(now)
            return
        if now < self.next_t:
            return
        known = {k for eff in buttons.values() for k in eff}
        if any(label not in buttons for label in guarded):
            known |= GUARDED_KEYS & self.keys  # まだ何を変えるか覚えていないボタンがある: 押してみる
        known = self._offered(known)
        if not known:
            return  # 設定を変えるボタンをまだ知らない（ランダムに押すうちに覚える）
        least = min(self.changes[k] for k in known)
        self.target = rng.choice(sorted(k for k in known if self.changes[k] == least))
        self.since = now
        self.tours += 1

    def apply(self, cands: list[Op], menus: set[str], settings: dict[str, Any] | None,
              buttons: dict[str, dict[str, Any]], routes: dict[str, list[list[str]]],
              guarded: set[str] | frozenset[str] = frozenset()) -> list[Op]:
        """変えに行っている設定を変えるボタンと、その画面を開くボタンを優先する。"""
        if self.target is None:
            return cands
        now = settings or {}
        want = {label for label, eff in buttons.items() if self.target in eff}
        if self.target in GUARDED_KEYS:
            want |= {label for label in guarded if label not in buttons}  # 何を変えるかまだ覚えていない
        if self.restoring:
            # 元の値にすると覚えたボタン。覚えていなければ、まだ押していないボタンを試す
            back = self.base.get(self.target)
            prefer = {label for label in want if label in buttons and buttons[label][self.target] == back} or {
                label for label in want if label not in buttons} or want
        else:
            # 押せば今と違う値になるボタンを先に（選ばれている値を押しても変わらない）
            prefer = {label for label in want
                      if label not in buttons or buttons[label][self.target] != now.get(self.target)} or want
        # ほかの設定もまとめて変えると分かったボタン（リセットなど）は、ほかに押すものがないときだけ
        # （変えに行くたびに音量・言語なども変わり、A-03 などの比べる区間を崩すため）
        prefer = {label for label in prefer
                  if label not in buttons or set(buttons[label]) <= {self.target}} or prefer
        targets = {_menu(label) for label in prefer}
        if targets & menus:
            boost = {label for label in prefer if _menu(label) in menus}
        else:
            boost = {b for m in targets for route in routes.get(m, []) for b in route}
        out = []
        for op in cands:
            t = op.target
            # 開き方の最初の一歩が右クリック・キーなら、その操作（ボタンでない）も押しにいく
            if (t is not None and t.kind == "button" and t.label in boost) or (t is None and op.label in boost):
                op = dataclasses.replace(op, novelty=False, by="settings").scaled("settings", self.factor)
            out.append(op)
        return out

    def _offered(self, keys: set[str]) -> set[str]:
        return keys if self.offered is None else keys & self.offered

    def notes(self, buttons: dict[str, dict[str, Any]]) -> list[str]:
        if not self.keys:
            return []
        out = []
        if self.tours:
            text = f"設定を変えに {self.tours} 回行った（変えられた {sum(self.reached.values())} 回"
            if self.gave_up:
                text += "、時間のうちに変えられなかった " + "、".join(f"{k} {n} 回" for k, n in sorted(self.gave_up.items()))
            text += "）"
            if self.entries:
                text += "。設定の画面の入り口: " + "、".join(f"`{e}` {n} 回" for e, n in self.entries.most_common())
            out.append(text)
        changed = [k for k in sorted(self.keys) if self.changes[k]]
        if changed:
            out.append("このランで変わった設定: " + "、".join(f"{k} {self.changes[k]} 回" for k in changed))
        known = {k for eff in buttons.values() for k in eff}
        # プロファイルで外した設定は、ボタンがなくて当然なので挙げない（変わったかは、レポートの「設定」の表で見る）
        never = sorted(self._offered(self.keys) - set(changed))
        if never:
            out.append("このランで一度も変わらなかった設定: " + "、".join(
                k + ("" if k in known else "（変えるボタンを見つけていない）") for k in never))
        if self.restored or self.restore_failed:
            text = f"画面の設定を変えたあと、元の値に戻した {self.restored} 回"
            if self.restore_failed:
                text += "、戻せなかった " + "、".join(f"{k} {n} 回" for k, n in sorted(self.restore_failed.items()))
            out.append(text)
        missing = sorted((self.offered or frozenset()) - self.keys)
        if missing:
            out.append("プロファイルの [settings] offered にあるのに、ゲームが値を出していない設定: " + "、".join(missing)
                       + "（パッケージが古いか、名前の書き間違い）")
        return out


VOLUME_KEYS = frozenset({"master_volume", "bgm_volume"})


@dataclass
class VolumeHold:
    """音量を変えたら、A-03（音量の設定が音に効いていない）が比べる区間のあいだ、ほかの設定を変えない。

    A-03 は、変える前の window_sec と、変えて settle_sec たってからの window_sec の出力の大きさを比べ、そのあいだにほかの
    設定が変わっていれば判定しない。ハーネスが設定のボタンを続けて押すと、ほとんど判定できなくなるので（DESIGN.md §9
    「設定を変えに行く」）:

    - 音量（全体・BGM）が変わってから after 秒（settle_sec + window_sec）は、設定を変えると分かっているボタン（経験の
      "settings"）を押さず、イレギュラー操作もしない
    - 設定が変わってから before 秒（window_sec）は、音量を変えるボタンを押さない

    待つ長さは rules.yaml の A-03 から決める（watchdog が作る）。A-03 がなければ待たない。
    """

    before: float = 0.0
    after: float = 0.0
    values: dict[str, Any] = field(default_factory=dict)  # 最後に見た設定の値
    last_change: float | None = None  # 最後に設定が変わったのを見た時刻（time.monotonic）
    until: float = 0.0  # 音量を変えたあと、ほかの設定を変えずに待つ終わりの時刻
    holds: int = 0  # 音量が変わって待った回数

    @property
    def enabled(self) -> bool:
        return self.after > 0

    def observe(self, settings: dict[str, Any] | None, now: float) -> None:
        """いまの設定の値を見る。ハーネスが押したか、ゲームが変えたかは問わない。"""
        if not self.enabled or not settings:
            return
        values = {k: v for k, v in settings.items() if k != "locales"}
        if self.values and values != self.values:
            changed = {k for k in values.keys() | self.values.keys() if values.get(k) != self.values.get(k)}
            self.last_change = now
            if changed & VOLUME_KEYS:
                self.until = now + self.after
                self.holds += 1
        self.values = values

    def holding(self, now: float) -> bool:
        """音量を変えたあとの、ほかの設定を変えずに待つあいだか。"""
        return self.enabled and now < self.until

    def apply(self, cands: list[Op], buttons: dict[str, dict[str, Any]], now: float) -> list[Op]:
        """待つあいだは、設定を変えるボタンを候補から外す。"""
        if not self.enabled:
            return cands
        if now < self.until:
            drop = {label for label, eff in buttons.items() if eff}
        elif self.last_change is not None and now - self.last_change < self.before:
            drop = {label for label, eff in buttons.items() if VOLUME_KEYS & set(eff)}
        else:
            return cands
        return [op for op in cands if op.target is None or op.target.label not in drop]

    def notes(self) -> list[str]:
        if not self.holds:
            return []
        return [f"音量を変えたあと {self.holds} 回、A-03 が比べる {self.after:g} 秒のあいだ、ほかの設定を変えずに待った"]
