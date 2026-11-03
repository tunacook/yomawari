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

## ボタンの役割と状態を伝える

`menu` のボタンには、種類（`type`）、役割（`role`）、押せない（`locked`）、スクロール領域で隠れている（`hidden`）が付く（`DESIGN.md` §9「ゲーム固有の仕様」）。種類と隠れているかは自動で読み取る。役割と押せないかは、標準の部品から分からないときだけゲーム側で伝える。

- 役割: ボタンと同じ GameObject に `YomawariButtonRole` を付け、Inspector で役割を選ぶ（終了・外部リンク・タイトルへ・画面や言語の設定・ニューゲーム・押さない）。Naninovel の `TitleExitButton`・`ControlPanelTitleButton`・`TitleNewGameButton` と、言語・解像度・画面モード・画質の設定の Dropdown は、付けなくても役割が出る
- 押せない: ボタンと同じ GameObject のコンポーネントに `IYomawariButtonState` を実装し、押せないあいだ `YomawariLocked` で `true` を返す（例: 劇中でアンロックされるまで押せないスロット）

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
