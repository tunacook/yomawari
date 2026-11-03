# com.tunacook.yomawari

[Yomawari](https://github.com/tunacook/yomawari)（夜間自動プレイのハーネス）のゲーム側。起動引数 `-yomawari` を付けたときだけ、Player.log に `[YOMAWARI]` 接頭辞の1行 JSON を出す。付けないときは何もしない。行の形式はリポジトリの `DESIGN.md` §3。

ゲームのコードから呼ぶ必要はない（`RuntimeInitializeOnLoadMethod` で自動的に始まる）。

| ev | 出すタイミング |
|---|---|
| `boot` | 起動直後（`version`, `build_type`, `build_guid`, `unity`） |
| `heartbeat` | 1 秒ごと（`fps`, `gc_mem`） |
| `log_error` | Error / Exception / Assert のログ（`type`, `message`, `stack`） |
| `resolution` | 起動直後と、解像度か画面モードが変わったとき（`w`, `h`, `mode`） |

Naninovel 1.21 以上が入っていると、`Yomawari.Naninovel` が `scene` / `line` / `line_done` / `choices` / `choices_done` / `menu` / `menu_close` / `transition` / `save` / `load` も出す。ゲームへの組み込み手順はリポジトリの `docs/game-integration.md`。

## ボタンについて出すこと

`menu` のボタンには、種類（`type`）、文字（`text`）、役割（`role`）、スクロール領域で隠れている（`hidden`）が付く（`DESIGN.md` §3、§9「ゲーム固有の仕様」①）。どれもエンジンの標準の部品から自動で読み取り、ゲーム側で Yomawari のための部品を付ける必要はない（`DESIGN.md` §1）。

- 文字: ボタンの子の `Text` / `TMP_Text` の文字列（改行を詰め、40 文字まで）。文字が画像に焼き込まれたボタンでは出ない
- 役割: Naninovel の `TitleExitButton`（`exit`）、`ControlPanelTitleButton`（`to_title`）、`TitleNewGameButton`（`new_game`）と、言語・解像度・画面モード・画質の設定の Dropdown（`system_setting`）のときだけ出る。それ以外のボタンの役割は、ハーネスが文字・名前・見た目から推定し、押した結果で確定する（`DESIGN.md` §9「役割の推定と判定」）
- 押せないボタン（`interactable` が false）は出さない

## 導入

`Packages/manifest.json` の `dependencies` に追加する。`#` の後ろはタグかコミット。

```json
"com.tunacook.yomawari": "https://github.com/tunacook/yomawari.git?path=unity/com.tunacook.yomawari#<ref>"
```

テストを Test Runner に出すには `testables` にも追加する。

```json
"testables": ["com.tunacook.yomawari"]
```

## 開発

`.meta` はパッケージに含める（Git から入れたパッケージは書き換え不可で、`.meta` がないファイルは無視される）。ファイルを足したら、Unity プロジェクトから `file:` パスでこのディレクトリを参照し、Editor に `.meta` を生成させてからコミットする。
