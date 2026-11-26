"""やることの項目ごとの「次にやること」（DESIGN.md §12「レポート構成」）。

レポートを読んだ開発者が、項目ごとに次に何をすればよいかが分かるようにする。起きたことを書くだけでは、
ゲームを直すのか、プロファイルを直すのか、yomawari に渡すのかを、読む人が毎回切り分けることになる。

- 誰が直すかが分かっている項目は、直す側と、直す手がかり（ログ・画面の写し）を書く
- 未確定の項目は、何を見ればどちらか決まるか（画面の写しでゲームが止まっていたか、など）と、決まったあとにすることを書く
- 同じ原因の項目（エラーログのあとに本文が進まなくなった、など）は、先に片づける項目の番号を書く

文言はゲームに依らない。ゲーム固有の名前は、検出の中身（ログの文面・場所）から引くだけにする。
"""

from __future__ import annotations

from typing import Any, Callable

from ..events import Issue
from .stalls import Stall

LOG_RULES = ("L-01", "L-06")
# 画面が反応しない・進まない検出。画面の写しを見て、ゲームが止まっていたかで直す側が決まる
STUCK_RULES = ("S-01", "S-02", "S-03", "S-05", "S-06")
CRASH_RULES = ("C-01", "C-02", "H-01")
# 進めなかった区間で「先に見る」検出。進めなくなる種類（止まる・落ちる・反応しない）だけで、軽微なもの（重いフレーム・警告）は
# 区間の原因にならないので見ない
BLOCKING_RULES = (*STUCK_RULES, *CRASH_RULES, "C-03", "C-04", "Y-01")
PERF_RULES = ("R-02", "R-03", "R-04", "R-05", "R-07", "R-08", "R-09")

# ルールごとの次にやること（短い形, 詳しい形）。{see} は画面の写し・経過など、見るものへの案内
BY_RULE: dict[str, tuple[str, str]] = {
    "C-01": ("ゲーム: 落ちた原因を調べる", "ログの最後とクラッシュダンプから、落ちたところを調べて直す"),
    "C-02": ("ゲーム: ダンプから原因を調べる", "クラッシュダンプのスタックトレースから、落ちたところを調べて直す"),
    "C-03": ("ゲーム: 起動しない原因を調べる", "ログの最後を見て、起動の途中で止まったところを調べる。パッケージが入っていないビルドでも出る"),
    "C-04": ("ゲーム: 終了の処理を確かめる", "終了のボタンを押したあと、プロセスが残る原因（終了を待つ処理・確認のダイアログ）を調べる"),
    "H-01": ("ゲーム: 固まった原因を調べる", "ハートビートが止まった時刻のログと CPU 使用率から、メインスレッドが止まったところを調べる"),
    "F-01": ("ゲーム: セーブの書き出しを確かめる", "セーブを終えたのにファイルが更新されていない。セーブの書き出し先と、書き出しの完了を確かめる"),
    "F-03": ("ゲーム: 設定の保存を確かめる", "設定を変えて起動し直したら戻っていた。設定を保存する時機（画面を閉じたとき・終了のとき）を確かめる"),
    "R-01": ("ゲーム: メモリの増え方を調べる",
             "メモリのスナップショット（Memory Profiler など）を起動直後と増えたあとで撮って比べ、解放されていないものを探す。"
             "増えていった場面は経過を見る"),
    "R-10": ("前回までとの差を確かめる",
             "前回までのランからゲームを変えていなければ、測定の揺れとして無視してよい。変えていれば、変えたところを見る"),
    "S-03": ("ゲーム: 終わらない遷移を調べる",
             "{see}を見て、暗転・遷移が終わらなかったところ（場所の行のコマンド）を調べる。遷移の終わりを待つ処理が止まっていないか確かめる"),
    "A-01": ("ゲーム: BGM を止める処理を確かめる", "場所の行で、前の BGM を止めずに次の BGM を鳴らしていないか確かめる"),
    "A-02": ("ゲーム: タイトルに戻るときのボイスを確かめる", "タイトルに戻るときに、鳴っているボイスを止めているか確かめる"),
    "A-03": ("ゲーム: 音量の設定を手で確かめる",
             "設定の画面で音量を変え、音が変わるか手で確かめる。変わらなければ、設定の値が音に渡っているところを直す"),
    "V-04": ("ゲーム: ウィンドウの大きさを確かめる", "画面モードと解像度を変えたあと、描く大きさとウィンドウが合っているか手で確かめる"),
    "V-05": ("画面を見て、ボタンが画面の外か判断する",
             "{see}を見て、ボタン・選択肢が画面の外に出ていたか判断する。出ていればゲームの配置を直す。出ていなければ yomawari の誤検知として渡す"),
    "V-08": ("ゲーム: 文字の速さの設定を手で確かめる", "設定の画面で文字の速さを変え、本文の出る速さが変わるか手で確かめる"),
    "V-09": ("ゲーム: 言語の設定を手で確かめる", "設定の画面で言語を変え、本文がその言語になるか手で確かめる"),
    "V-03": ("ハーネスの環境を確かめる", "ゲームのウィンドウが画面に出ていなかった。ラン中に最小化・別の操作をしていないか確かめる"),
    "V-07": ("画面を見て、押せないように見えるか判断する",
             "{see}を見て、そのボタンが押せないように見えるか判断する。見えなければ yomawari の誤検知として渡す。"
             "見えるなら、押せる（別の画面が出る）のが仕様か確かめ、仕様でなければゲームを直す"),
    "Y-01": ("yomawari: 止まった原因を調べる",
             "ハーネスの操作が止まっていた。{see}と events.jsonl の最後の操作から、ハーネスが何を待って止まったかを調べる"),
}

DEFAULT = ("内容を見て、不具合か誤検知か判断する",
           "{see}を見て、ゲームの不具合か、yomawari の誤検知かを判断する。不具合ならゲームを直し、誤検知なら yomawari に渡す")


def _see(it: Any) -> str:
    """見るものの案内。再起動の前に残した画面の写しがあれば、それを先に挙げる。"""
    first: Issue | None = it.issues[0] if it.issues else None
    if first is not None and (first.evidence.get("snapshot") or {}).get("png"):
        return "最初の検出のときの画面（下の「画面」）と経過"
    return "下の経過"


def _for_issue_item(it: Any, n_of: Callable[[tuple[str, ...]], int | None],
                    error_before: Callable[[Issue], Issue | None]) -> tuple[str, str]:
    from .digest import WHO_GAME, WHO_HARNESS, WHO_HARNESS_STOPPED, item_key

    first: Issue = it.issues[0]
    rule = first.rule_id
    if it.who == WHO_HARNESS:
        return ("yomawari: 誤検知か確かめる",
                f"根拠（{it.basis}）が正しければ、yomawari の誤検知。この項目を yomawari 側に渡す")
    if it.who == WHO_HARNESS_STOPPED:
        short, long = BY_RULE["Y-01"]
        return short, long.format(see=_see(it))
    # 同じ起動の直前にゲームのエラーログがあれば、それが原因の見込み。先に片づける項目を指す
    if rule not in LOG_RULES:
        causes = [e for e in (error_before(i) for i in it.issues) if e is not None]
        if causes and len(causes) * 2 >= len(it.issues):
            n = n_of(item_key(causes[0]))
            where = f"#{n} の" if n is not None else "直前の"
            return (f"{where}エラーを先に直す",
                    f"{len(causes)}/{len(it.issues)} 回で、直前にゲームがエラーを出していた（{where}エラーログ）。"
                    "先にそのエラーを直し、次のランで消えるか確かめる")
    if rule in LOG_RULES and it.who == WHO_GAME:
        if rule == "L-01":
            return ("ゲーム: エラーを直す",
                    "ログの文面とスタックトレース（下の「ログ」）から、エラーを出しているところを直す")
        return ("ゲーム: 警告を確かめる（軽微）",
                "警告を出しているところを確かめる。直さないと決めたら、プロファイルの [rules.L-06] ignore_patterns に書くと次から出ない")
    if rule in BY_RULE:
        short, long = BY_RULE[rule]
        return short, long.format(see=_see(it))
    if rule in STUCK_RULES:
        return ("画面を見て、ゲームが止まっていたか判断する",
                f"{_see(it)}を見て、ゲームが本当に入力を受けていなかったか判断する。止まっていたら、経過の最後の操作を"
                "手で同じように押して再現し、ゲームを直す。止まっていなければ yomawari の誤検知として渡す")
    if rule in PERF_RULES:
        return ("ゲーム: 重くなる場面を確かめる（軽微なら後回し）",
                "場所の行・コマンドをプロファイラーで見て、重いフレームの中身を確かめる。体感で気にならなければ後回しでよい")
    short, long = DEFAULT
    return short, long.format(see=_see(it))


def for_stall(st: Stall, cfg: dict[str, Any]) -> tuple[str, str, bool]:
    """進めなかった区間の次にやること。3 つめは、ハーネスの遠回りと判断したか。"""
    read = st.reread + st.flow_new
    blocking = [r for r, _ in st.issues.most_common() if r in BLOCKING_RULES]
    if blocking:
        rules = "・".join(blocking)
        return (f"このあいだの検出（{rules}）を先に見る",
                f"このあいだに {rules} が出ていた。その項目を先に片づけ、次のランで区間が消えるか確かめる", False)
    if (read and st.reread / read >= float(cfg["report_detour_reread_share"])
            and st.loads >= int(cfg["report_detour_min_loads"])):
        return ("ゲーム側は対応不要（ハーネスの遠回り）",
                f"ゲームは進めていた。ハーネスがロードを {st.loads} 回して、読んだことのある本文を {st.reread} 行読み直していた"
                "（新しく読んだのは {0} 行）。ゲーム側の対応は不要".format(st.flow_new), True)
    if "ランが終わるまで" in st.ended:
        return ("先へ進む方法があるか確かめる",
                "ランが終わるまで、この行より先に進めなかった。経過の最後を見て、行き止まり（作りかけ・エンディング）か、"
                "先へ進む操作があるのにハーネスが見つけられなかったかを判断する。後者なら、その操作（選択肢の正解など）を"
                "プロファイルに書く", False)
    return ("経過を見て、進めなかった理由を判断する",
            "経過を見て、ゲームが進めない状態だった（不具合）か、ハーネスが進め方を見つけられなかった（プロファイルに"
            "正解を書く）かを判断する", False)


def assign(items: list[Any], cfg: dict[str, Any], error_before: Callable[[Issue], Issue | None]) -> None:
    """項目ごとに next_short・next を決める。項目の番号（表の #）を指すので、並べたあとに呼ぶ。
    進めなかった区間は、誰が直すかで並び順が変わるので、項目を作るときに決めている（digest._stall_items）。"""
    numbers = {it.key: n for n, it in enumerate(items, 1) if it.key is not None}
    for it in items:
        if it.next:
            continue
        if it.issues:
            it.next_short, it.next = _for_issue_item(it, numbers.get, error_before)
        elif it.kind == "short_stalls":
            it.next_short = "個別の対応は不要"
            it.next = "同じ場所が何度も出ていれば、長い区間と同じように経過を見る"
        elif it.kind == "suspects":
            it.next_short = "プロファイルの正解が古くないか確かめる"
            it.next = ("記録ごとに、プロファイルの正解（行・文字）がいまのゲームと合っているか確かめる。古ければプロファイルを直す。"
                       "合っているのに外れていれば、ゲームを確かめる")
        elif it.kind == "silent":
            it.next_short = "押せないのが仕様か確かめる"
            it.next = ("ボタンごとに、押しても何も起きないのが仕様か確かめる。仕様なら、プロファイルの [screens.<menu>] silent に"
                       "書くと次から一覧に出ない。仕様でなければゲームを直す")
