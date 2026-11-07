"""状態トラッカー。ゲームのイベント（[YOMAWARI] 行）から、いま画面に何が出ているかを保つ。

操作の選択（Decider）とガードはこの状態だけを見る。座標は rect のまま（正規化座標）で持ち、
画面座標への変換は押す直前に input/pointer.py で行う。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .events import Event

Rect = tuple[float, float, float, float]  # 正規化座標 [x0, y0, x1, y1]、左上原点


def _rect(v: Any) -> Rect | None:
    if not isinstance(v, list) or len(v) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(n) for n in v)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _opt_str(v: Any) -> str | None:
    return str(v) if v else None


@dataclass(frozen=True)
class Target:
    """押せるもの。kind は button（menu のボタン）か choice（選択肢）。"""

    kind: str
    menu: str  # choice のときは "choices"
    name: str  # button は GameObject 名、choice は選択肢の id
    rect: Rect
    text: str | None = None
    index: int | None = None
    nth: int = 0  # 同じ menu に同じ名前のボタンが複数あるとき、何番目か（セーブスロットなど）
    type: str | None = None  # button / toggle / dropdown / slider / input / other（DESIGN.md §3）
    role: str | None = None  # exit / external / to_title / system_setting / new_game / deny
    locked: bool = False  # ゲームが「いまは押せない（押しても何も起きないのが仕様）」と伝えたもの
    hidden: bool = False  # ゲームが「スクロール領域のマスクで隠れている」と伝えたもの
    covered: bool = False  # ゲームが「中心を押すと、上に重なったほかの UI に当たる」と伝えたもの

    @property
    def key(self) -> str:
        """[guard.buttons] で使う "<menu の name>/<ボタンの name>"。"""
        return f"{self.menu}/{self.name}"

    @property
    def label(self) -> str:
        """同じ名前のボタンを区別する名前。操作の記録と試行回数の数え方に使う。

        選択肢は本文で表す（Naninovel の選択肢の id は表示のたびに変わり、周回をまたいで数えられないため）。
        """
        if self.kind == "choice" and self.text:
            return f"{self.menu}/{self.text}"
        return f"{self.key}#{self.nth}" if self.nth else self.key

    @property
    def on_screen(self) -> bool:
        """中心が描画領域の中にあるか（ほかの UI に覆われているかは問わない）。"""
        if self.hidden:
            return False
        cx, cy = (self.rect[0] + self.rect[2]) / 2, (self.rect[1] + self.rect[3]) / 2
        return 0 <= cx <= 1 and 0 <= cy <= 1

    @property
    def visible(self) -> bool:
        """見えていて押せるか。ゲームが hidden / covered と伝えたもののほか、中心が描画領域の外にあるもの（hidden を出さない
        古いパッケージやエンジンでは、スクロールで隠れた項目の矩形が画面の外のまま来る）も見えていないとする。"""
        if self.hidden or self.covered:
            return False
        cx, cy = (self.rect[0] + self.rect[2]) / 2, (self.rect[1] + self.rect[3]) / 2
        return 0 <= cx <= 1 and 0 <= cy <= 1


@dataclass
class Menu:
    name: str
    modal: bool
    buttons: list[Target]
    order: int  # 表示された順番（大きいほど新しい）
    # 開いている間に本文（line）が進んだ。本文を読んでいる途中に重なって出ている画面（回想中の「本編へ戻る」など）と確かめた
    read_over: bool = False
    # スクロール領域ごとの、表示される範囲（DESIGN.md §3 の scroll の rect）。一覧の目標へ向かう向きを決めるのに使う
    scroll: tuple[Rect, ...] = ()

    @property
    def visible_buttons(self) -> list[Target]:
        return [b for b in self.buttons if b.visible]

    @property
    def area(self) -> Rect | None:
        """ボタン全体を囲む矩形を描画領域で切り取ったもの。スクロールの操作をこの上でする。"""
        if not self.buttons:
            return None
        x0 = max(0.0, min(b.rect[0] for b in self.buttons))
        y0 = max(0.0, min(b.rect[1] for b in self.buttons))
        x1 = min(1.0, max(b.rect[2] for b in self.buttons))
        y1 = min(1.0, max(b.rect[3] for b in self.buttons))
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


@dataclass
class GameState:
    launch: int = -1
    booted: bool = False
    scene: str | None = None
    line: dict[str, Any] | None = None
    line_done: bool = False
    menus: dict[str, Menu] = field(default_factory=dict)
    choices: list[Target] = field(default_factory=list)
    # 選び終えた選択肢の id（choices_done）。選んだあと消えていく途中のボタンを、古いパッケージ（0.7.0 まで）が
    # 同じ id の choices として出し直し、消えたことは伝えないので、それを選択肢として扱わない。
    # ロールバック・ロードで同じ選択肢が出直したときに無視しないように、次の scene・ロードで忘れる。
    # 次の本文では忘れない（選んだ直後の本文のあとに出し直すことがある。run 2026-11-06_1757 で、0.7.0 のビルドが
    # 選んだあとの本文 160 行目のあとに同じ id の選択肢を 3 回出し直し、ハーネスは 2 分半のあいだ消えた選択肢の位置を
    # 押し続けた）。選択肢の id は出るたびに変わる（同じ選択肢でも、出直せば別の id になる）ので、長く覚えても無視し続けない
    handled_choices: set[str] = field(default_factory=set)
    # 最後に選ばれた選択肢の id（choices_done）。scene が変わっても消さない。ハーネスが押した選択肢が本当に選ばれたかを
    # これで確かめる（run 2026-11-06_2334: 「話さない」を押しても選ばれず、あとで別の操作で「話す」が選ばれたのに、
    # 「話さない」を答えたと覚え、バッドエンドから戻ったときに外れとした）
    chosen: str | None = None
    transitions: set[str] = field(default_factory=set)
    resolution: dict[str, Any] | None = None
    saving: bool = False
    loading: bool = False
    last_game_t: float | None = None
    last_progress_t: float | None = None  # scene / line / choices が来た時刻
    # 役割の確定（DESIGN.md §9「役割の推定と判定」）に使う観測。
    # 起動して最初に出たボタンのある menu をタイトル画面として覚え、本編（line）を挟んで再び出た回数を数える
    title_menu: str | None = None
    title_returns: int = 0
    resolution_changes: int = 0  # resolution の値（w / h / mode）が変わった回数
    saves: int = 0  # セーブが始まった回数（押したボタンでセーブしたかを見るため。V-07）
    # 画面の見た目から、後ろに下がったと判定した menu（ハーネスが画像で判定して入れる。fuzz/loop.py の _look）。
    # 押せる候補から除く。閉じたら除く
    behind: set[str] = field(default_factory=set)
    # ボタン（label）→ 画面から読んだ文字（OCR）。ボタンの文字は、UI の部品の作りに頼らず、人と同じく画面から読む。
    # 出し直された menu にも引き継ぐ。閉じたら忘れる（ボタンの名前は並びの番号なので、次に開いたときは中身が違うことがある）
    read_text: dict[str, str] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)  # 閉じた menu（ハーネスが、その画面で読んだ文字の記録を捨てるため）
    _played_since_title: bool = False
    _order: int = 0

    def feed(self, ev: Event) -> None:
        if ev.source == "harness" and ev.kind == "launch":
            # 起動し直したら状態を捨てる
            self.__dict__.update(vars(GameState(launch=ev.launch)))
            return
        if ev.source != "game":
            return
        self.last_game_t = ev.t
        d = ev.data
        handler = getattr(self, f"_on_{ev.kind}", None)
        if handler is not None:
            handler(d, ev.t)

    # --- イベントごと ---

    def _on_boot(self, d: dict[str, Any], t: float) -> None:
        self.booted = True

    def _on_resolution(self, d: dict[str, Any], t: float) -> None:
        res = {"w": d.get("w"), "h": d.get("h"), "mode": d.get("mode")}
        if self.resolution is not None and res != self.resolution:
            self.resolution_changes += 1
        self.resolution = res

    def _on_scene(self, d: dict[str, Any], t: float) -> None:
        self.scene = d.get("name")
        self.last_progress_t = t
        self.handled_choices.clear()

    def _on_line(self, d: dict[str, Any], t: float) -> None:
        self.line = {k: d.get(k) for k in ("label", "line_id", "speaker", "text")}
        self.line_done = False
        self.last_progress_t = t
        for m in self.menus.values():
            m.read_over = True
        if self.title_menu is not None and self.title_menu not in self.menus:
            self._played_since_title = True

    def _on_line_done(self, d: dict[str, Any], t: float) -> None:
        self.line_done = True

    def _on_menu(self, d: dict[str, Any], t: float) -> None:
        name = str(d.get("name"))
        buttons = []
        seen: dict[str, int] = {}
        for b in d.get("buttons") or []:
            r = _rect(b.get("rect")) if isinstance(b, dict) else None
            if r is not None:
                bname = str(b.get("name"))
                nth = seen.get(bname, 0)
                label = f"{name}/{bname}#{nth}" if nth else f"{name}/{bname}"
                buttons.append(Target(
                    "button", name, bname, r, nth=nth,
                    text=self.read_text.get(label),  # ボタンの文字は画面から読む（パッケージの text は使わない）
                    type=_opt_str(b.get("type")), role=_opt_str(b.get("role")), locked=bool(b.get("locked")),
                    hidden=bool(b.get("hidden")), covered=bool(b.get("covered")),
                ))
                seen[bname] = seen.get(bname, 0) + 1
        prev = self.menus.get(name)
        if self.title_menu is None and buttons:
            # 最初はボタンなし（フェードイン中で押せない）で出て、あとからボタン付きで出し直すことがあるので、
            # 新しく出たときに限らず、最初にボタンを持った menu をタイトルとする
            self.title_menu = name
        elif prev is None and name == self.title_menu and self._played_since_title:
            self.title_returns += 1
            self._played_since_title = False
        if prev is None:
            self._order += 1
        scroll = tuple(r for s in d.get("scroll") or [] if isinstance(s, dict) and (r := _rect(s.get("rect"))) is not None)
        menu = Menu(name, bool(d.get("modal")), buttons, prev.order if prev else self._order, scroll=scroll)
        # 出し直し（ボタンが動いた・確認ダイアログの間だけ隠れた）では、本文が進んだことを忘れない。閉じたら忘れる
        menu.read_over = prev is not None and prev.read_over
        self.menus[name] = menu

    def _on_menu_close(self, d: dict[str, Any], t: float) -> None:
        name = str(d.get("name"))
        self.menus.pop(name, None)
        self.behind.discard(name)
        prefix = name + "/"
        self.read_text = {k: v for k, v in self.read_text.items() if not k.startswith(prefix)}
        self.closed.add(name)

    def _on_choices(self, d: dict[str, Any], t: float) -> None:
        raw = [c for c in d.get("items") or [] if isinstance(c, dict)]
        if any(str(c.get("id")) in self.handled_choices for c in raw):
            raw = []  # 選び終えた選択肢と同じ組（選ばなかったほうも含めて消えていく途中）
        items = []
        for i, c in enumerate(raw):
            r = _rect(c.get("rect"))
            if r is None or c.get("locked"):
                continue
            items.append(Target("choice", "choices", str(c.get("id")), r, c.get("text"), i))
        self.choices = items
        if items:
            self.last_progress_t = t

    def _on_choices_done(self, d: dict[str, Any], t: float) -> None:
        self.choices = []
        if d.get("id") is not None:
            self.handled_choices.add(str(d.get("id")))
            self.chosen = str(d.get("id"))

    def _on_transition(self, d: dict[str, Any], t: float) -> None:
        name = str(d.get("name"))
        if d.get("phase") == "start":
            self.transitions.add(name)
        else:
            self.transitions.discard(name)

    def _on_save(self, d: dict[str, Any], t: float) -> None:
        self.saving = d.get("phase") == "start"
        if self.saving:
            self.saves += 1

    def _on_load(self, d: dict[str, Any], t: float) -> None:
        self.loading = d.get("phase") == "start"
        self.handled_choices.clear()

    # --- 参照 ---

    @property
    def busy(self) -> bool:
        """暗転・セーブ・ロードの途中。通常の操作は待つ。"""
        return bool(self.transitions) or self.saving or self.loading

    def reading_with_buttons(self) -> bool:
        """本文を読んでいる途中で、操作のボタンが画面に出ている（回想中の「本編へ戻る」など）。

        モーダルも選択肢もなく、ボタンの見えている menu がすべて、開いている間に本文が進んだことがある（出たタイミングで
        推測せず、本文が進んだという結果で確かめる）。本文のあとに開いた画面（タイトルに戻った、ボタンで一覧を開いた）は、
        まだ本文が進んでいないので、本文を読んでいる途中ではない。
        """
        if self.line is None or self.choices or self.top_modal() is not None:
            return False
        shown = [m for m in self.menus.values() if m.visible_buttons]
        return bool(shown) and all(m.read_over for m in shown)

    def top_modal(self) -> Menu | None:
        """いちばん新しいモーダル。ボタンがすべてほかの UI に覆われているモーダルは、手前に出ていないので除く
        （ゲームが開いた選択画面と同時に右クリックメニューが開き、選択画面の下に隠れたときなど）。

        モーダルのボタンの一部がほかの UI に覆われていて、そのモーダルより後に出たモーダルでない画面に、覆われていない
        ボタンが見えているなら、その画面が上に重なって手前に出ている（ゲームがモーダルの印を付け忘れた画面）。人が見て
        手前にあるものを手前とし、その画面を返す（run 2026-11-07_1809: 一覧から開いた要約を、思い出から戻るロードのあと
        ゲームが一覧をモーダル・要約をモーダルでないと伝え、要約が上に出ているのに一覧を動かし続けた）。"""
        modals = [
            m for m in self.menus.values()
            if m.modal and m.name not in self.behind and not (m.buttons and all(b.covered for b in m.buttons))
        ]
        if not modals:
            return None
        top = max(modals, key=lambda m: m.order)
        if not any(b.covered and b.on_screen for b in top.buttons):
            return top
        over = [
            m for m in self.menus.values()
            if not m.modal and m.order > top.order and m.name not in self.behind and m.visible_buttons
            and not any(b.covered and b.on_screen for b in m.buttons)
        ]
        return max(over, key=lambda m: m.order) if over else top

    def targets(self) -> list[Target]:
        """いま押せるもの。モーダルな menu があれば、いちばん新しいモーダルのボタンだけ。画面の外のボタンは除く。"""
        top = self.top_modal()
        if top is not None:
            return top.visible_buttons
        out = [c for c in self.choices if c.visible]
        for m in sorted(self.menus.values(), key=lambda m: -m.order):
            if m.name not in self.behind:
                out.extend(m.visible_buttons)
        return out

    def find(self, key: str, nth: int = 0) -> Target | None:
        """"<menu>/<name>" で探す（同じ名前が複数あれば nth 番目）。押せるかどうか（モーダル）は問わない。"""
        for m in self.menus.values():
            for b in m.buttons:
                if b.key == key and b.nth == nth:
                    return b
        for c in self.choices:
            if c.key == key:
                return c
        return None

    def set_read_text(self, texts: dict[str, str]) -> None:
        """画面から読んだ文字を、いま出ているボタンに付ける。"""
        self.read_text.update(texts)
        for m in self.menus.values():
            m.buttons = [dataclasses.replace(b, text=texts[b.label]) if b.label in texts else b for b in m.buttons]

    def signature(self) -> tuple[Any, ...]:
        """画面の状態の要約。操作のあとにこれが変われば「反応があった」とみなす。

        ボタンの矩形は含めない（マウスを載せただけで変わることがあるため）。見えているボタンの並びは含める
        （スクロールすると変わる）。
        """
        return (
            self.scene,
            self.line["line_id"] if self.line else None,
            self.line_done,
            tuple(sorted((m.name, m.modal, tuple(b.label for b in m.visible_buttons)) for m in self.menus.values())),
            tuple(c.key for c in self.choices),
            self.busy,
        )

    def screens(self) -> tuple[str | None, frozenset[str]]:
        """画面の切り替わりを見るための要約: (scene, ボタンのある menu かモーダルな menu の名前)。

        signature と違い、本文の位置・ボタンのない通知（トーストなど）・ボタンの並びの変化は含めない。
        押せないことを知らせる演出では変わらず、別の画面が開いた・閉じたときに変わる。
        """
        return self.scene, frozenset(m.name for m in self.menus.values() if m.buttons or m.modal)

    def snapshot(self) -> dict[str, Any]:
        top = self.top_modal()
        return {
            "scene": self.scene,
            "line_id": self.line["line_id"] if self.line else None,
            "line_done": self.line_done,
            "menus": sorted(self.menus),
            "top_modal": top.name if top else None,
            "choices": len(self.choices),
            "busy": self.busy,
        }
