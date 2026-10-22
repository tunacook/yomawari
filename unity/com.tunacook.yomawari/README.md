# com.tunacook.yomawari

[Yomawari](https://github.com/tunacook/yomawari)（夜間自動プレイのハーネス）のゲーム側。起動引数 `-yomawari` を付けたときだけ、Player.log に `[YOMAWARI]` 接頭辞の1行 JSON を出す。付けないときは何もしない。行の形式はリポジトリの `DESIGN.md` §3。

ゲームのコードから呼ぶ必要はない（`RuntimeInitializeOnLoadMethod` で自動的に始まる）。

| ev | 出すタイミング |
|---|---|
| `boot` | 起動直後（`version`, `build_type`, `build_guid`, `unity`） |
| `heartbeat` | 1 秒ごと（`fps`, `gc_mem`） |
| `log_error` | Error / Exception / Assert のログ（`type`, `message`, `stack`） |

Naninovel 1.21 以上が入っていると、`Yomawari.Naninovel` が `scene` / `line` / `line_done` / `choices` / `choices_done` / `menu` / `menu_close` / `transition` / `save` / `load` も出す。ゲームへの組み込み手順はリポジトリの `docs/game-integration.md`。

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
