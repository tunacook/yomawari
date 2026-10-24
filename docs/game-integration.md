# ゲーム側の組み込み

Yomawari でゲームを自動プレイするために、ゲーム側でやることをまとめる。ハーネスとゲームのやりとりは、ゲーム→ハーネスが Player.log の `[YOMAWARI]` 行、ハーネス→ゲームが OS のマウス・キーボード入力だけ（[DESIGN.md §1](../DESIGN.md#1-目的と前提)）。ゲームに操作を受け付ける口は作らない。

行の形式とイベントの一覧は [DESIGN.md §3](../DESIGN.md#3-ゲーム側-yomawarilogc) を参照。

## 1. 前提

| 項目 | 内容 |
|---|---|
| Unity | `package.json` の下限は 2022.3。確認しているのは 6000.3 だけ |
| スクリプトバックエンド | Mono / IL2CPP のどちらでもよい（IL2CPP のストリッピングで消えないようにしてある） |
| ビルド | Development Build でもリリースビルドでもよい。出力するかどうかは起動引数 `-yomawari` だけで決まる |
| Naninovel | 1.21 以上なら、本文・選択肢・メニューなどのイベントが自動で出る（[4.](#4-naninovel-のゲーム)） |

## 2. パッケージを入れる

`Packages/manifest.json` の `dependencies` に1行足す。`#` の後ろは yomawari のタグかコミット。

```json
"com.tunacook.yomawari": "https://github.com/tunacook/yomawari.git?path=unity/com.tunacook.yomawari#<ref>"
```

パッケージのテストを Test Runner に出す場合は `testables` にも足す。

```json
"testables": ["com.tunacook.yomawari"]
```

ゲームのコードから呼ぶ必要はない（`RuntimeInitializeOnLoadMethod` で始まる）。ゲーム本体の asmdef からパッケージを参照しないこと。外すときは manifest の1行を消すだけで済むようにしておく。

## 3. Player Settings とビルド

- **Use Player Log**：有効のまま（既定）。ハーネスは `-logFile` で Player.log の場所を指定する
- **Stack Trace Logging**：Error / Exception は Script Only 以上にする（`log_error` の `stack` に入る）。Log / Warning は任意
- **Run In Background**：`yomawari.toml` の `[expect] run_in_background` と合わせる。無効だと、フォーカスを失ったときに heartbeat も止まる
- **フルスクリーン**：Fullscreen Window（ボーダーレス）にする。ハーネスは `-screen-fullscreen 0 -screen-width 1280 -screen-height 720` のような引数で起動する（`yomawari.toml` の `builds.<名前>.args`）
- **Bundle Identifier**（macOS）：セーブと PlayerPrefs の退避・復元は `~/Library/Application Support/<バンドルID>/` と `~/Library/Preferences/<バンドルID>.plist` を対象にする。体験版と製品版で ID を分けていれば、互いのセーブを触らない
- **Steamworks**：`SteamAPI_RestartAppIfNecessary` で Steam 経由に再起動されると、ハーネスがプロセスを見失う。ビルドの隣に `steam_appid.txt` を置くか、`-yomawari` のときは呼ばない

パッケージの参照を変えたら、**ビルドし直してから**確かめる（古い `.app` のままだと何も変わらない）。

## 4. Naninovel のゲーム

Naninovel 1.21 以上が入っていると、`Yomawari.Naninovel` がコンパイルされ、次のイベントが自動で出る。

| ev | 元にしているもの |
|---|---|
| `scene` | `IScriptPlayer.OnExecute`（メイントラックのスクリプトが変わったとき） |
| `line` / `line_done` | `ITextPrinterManager.OnPrintStarted` / `OnPrintFinished` |
| `choices` | 表示中の `ChoiceHandlerButton`（0.2 秒ごとに確認） |
| `choices_done` | `IChoiceHandlerActor.OnChoiceHandled` |
| `save` / `load` | `IStateManager.OnGameSave*` / `OnGameLoad*` |
| `menu` / `menu_close` | `IUIManager` が管理する UI の表示・非表示と、その中の押せる `Selectable`（0.2 秒ごとに確認） |
| `transition` | `ISceneTransitionUI` / `ILoadingUI` の表示・非表示 |

注意点:
- **ボタンの名前**：`menu` の `buttons[].name` は GameObject の名前。ハーネスはこの名前で「押してはいけないボタン」（終了、外部リンク、セーブ削除など）を判断するので、`Button (3)` のような名前ではなく、役割が分かる名前にしておく
- **独自の演出 UI**：`CustomUI` を継承した独自の暗転などは、`transition` ではなく `menu` として出る（ボタンが0個の `menu`）。暗転として扱いたい場合は、[5.](#5-ゲーム固有のイベントを足す) の方法で `transition` を出す
- **UI の位置**：矩形は `RectTransform` の四隅を画面座標にして出す。Canvas が Screen Space - Camera のときは `worldCamera` を使う

## 5. ゲーム固有のイベントを足す

Naninovel のイベントで取れないもの（独自の暗転、ミニゲームなど）は、ゲームのリポジトリに小さな asmdef を作って出す。

- asmdef は `Yomawari` を参照し、ゲーム本体の asmdef からは参照しない（ゲーム本体を Yomawari に依存させない）
- イベント名とフィールドは DESIGN.md §3 の一覧に合わせる。新しいイベントが必要なら、先に yomawari 側（ハーネス・パッケージ・§3）に足す

```csharp
using Yomawari;

// 例: 独自の暗転 UI の表示・非表示で transition を出す
void OnCurtainVisibilityChanged(bool visible)
{
    YomawariLog.Emit("transition",
        "\"phase\":" + YomawariFormat.Str(visible ? "start" : "end") +
        ",\"name\":" + YomawariFormat.Str("CurtainUI"));
}
```

`YomawariLog.Emit` は `-yomawari` がないときは何もしないので、呼び出し側で判定しなくてよい。矩形は `YomawariRect.Json(rectTransform)` で作れる。

## 6. Naninovel 以外のエンジン

> **この節は未検証。** 作者が確かめたのは Unity + Naninovel（Rachel）だけ。以下は各エンジンの仕組みから考えた方針で、API 名やファイルの場所は、実際に組み込むときに各エンジンの文書で確かめること。

ハーネスが見ているのはイベントの名前とフィールド（DESIGN.md §3）だけで、Naninovel には依存していない。エンジンごとに違うのは「イベントをどう出すか」と、Unity 以外では「ハーネスがどう起動し、どこからログを読むか」の2点。

| エンジン | イベントの出し方 | ハーネス側の変更 |
|---|---|---|
| Unity（エンジン問わず） | 本体（`Yomawari`）だけで boot・heartbeat・log_error が出る | 不要 |
| 宴（Unity） | ゲーム側のコードから `YomawariLog.Emit` | 不要 |
| Ren'Py | Ren'Py のスクリプト（Python）から `[YOMAWARI]` 行を書く | 起動・ログの読み先・セーブの場所 |
| ティラノスクリプト | JavaScript のプラグインから `[YOMAWARI]` 行を出す | 起動・ログの読み先・セーブの場所 |

### Unity（エンジン問わず）
- 本体だけで、起動しない（C-03）、ハング（H-01）、異常終了・クラッシュ（C-01・C-02）、エラーログ（L-01）は検出できる
- 自動プレイ（クリック・選択肢の操作）には `line` / `choices` / `menu` などが要る。自作のエンジンなら、[5.](#5-ゲーム固有のイベントを足す) の方法でゲーム側の asmdef から出す
- ボタンの検出は uGUI の `Selectable` を見ている。UI Toolkit の UI には別の対応が要る

### 宴（Unity）
- 宴は Unity のアセットなので、本体（boot・heartbeat・log_error）はそのまま動く
- 宴は UPM パッケージではなく `Assets/` に入れる形で配られるため、パッケージ側の `versionDefines` では宴の有無を判定できない。また宴のコードが Assembly-CSharp に入る場合、パッケージの asmdef からは参照できない。そのため、**変換層はパッケージではなくゲーム側に置く**（宴のコードと同じアセンブリか、宴を参照できる asmdef）
- 本文・選択肢は `AdvEngine` のページや選択肢の管理（`AdvPage`、`AdvSelectionManager` など）のイベントから `line` / `line_done` / `choices_done` を出す
- 宴の UI は uGUI なので、表示中の `Selectable` から `menu` のボタンを拾う考え方（`YomawariUiWatcher`）は流用できる。ただし「どの UI が開いているか」の判定は、Naninovel の `IUIManager` の代わりに宴の仕組みで行う

### Ren'Py
Unity ではないので、ゲーム側・ハーネス側の両方に作業が要る。

- **イベント**：Ren'Py のスクリプトに Python のフックを足し、`[YOMAWARI]` 行をファイルに書く
  - `line`：キャラクターのコールバック（`config.all_character_callbacks`）
  - `scene`：ラベルのコールバック（`config.label_callback`）
  - `heartbeat`：定期コールバック（`config.periodic_callbacks`）で1秒ごと
  - `log_error`：例外が起きると Ren'Py は `traceback.txt` を書くので、ハーネスがそのファイルの出現を見てもよい
  - `choices` / `menu`：画面（screen）の表示状態と、表示中のボタンの位置を取って出す。正規化座標はウィンドウではなく Ren'Py の仮想解像度（`config.screen_width` / `config.screen_height`）で割る
  - `-yomawari` の代わりに、環境変数などで有効・無効を切り替える
- **ハーネス**：起動（Ren'Py が書き出した `.app` / `.exe`）、ログの読み先（Player.log の代わりに上のファイル）、セーブの場所（Ren'Py のセーブフォルダ）を、エンジンごとに切り替えられるようにする必要がある。今は Unity 前提で書かれている（`launcher.py`、`platform/`）

### ティラノスクリプト
Unity ではないので、Ren'Py と同じくゲーム側・ハーネス側の両方に作業が要る。

- **イベント**：プラグイン（JavaScript）を足し、タグの実行（本文の表示、選択肢のボタン、ラベルへのジャンプ、セーブ・ロード）をフックして出す
  - 画面は HTML なので、ボタンの位置は `getBoundingClientRect()` を `window.innerWidth` / `innerHeight` で割れば正規化座標になる。Unity より正確に取れる
  - `log_error`：`window.onerror` と `unhandledrejection` で拾う
  - `heartbeat`：`requestAnimationFrame` で数えて1秒ごとに出す（描画が止まると途絶える）
  - 出力先：PC 版のランタイム（Electron / NW.js）で Node の `fs` が使えればファイルに書く。使えなければ `console.log` を標準出力に出す設定にして、ハーネスが標準出力を読む
- **ハーネス**：Ren'Py と同じく、起動・ログの読み先・セーブの場所の切り替えが要る。ブラウザ版はウィンドウとプロセスの扱いが変わるので対象外

### 画面の仕様の自動読み取り（DESIGN.md §9「ゲーム固有の仕様」①）
3層の分け方（① エンジンの標準 UI から自動で読み取る、② ゲームのコードがインターフェースで伝える、③ ゲームのプロファイル）は、どのエンジンでも同じ。② と ③ はそのまま使えるが、① はエンジンごとに作り直す。

| エンジン | ① が読む標準 UI | 置き場所 |
|---|---|---|
| Unity / 宴 | uGUI / TextMeshPro の `Selectable`、`ScrollRect`、`RectMask2D` / `Mask`、`Dropdown` / `TMP_Dropdown` | パッケージ（宴は UI が uGUI なので同じ考え方で読める。どの UI が開いているかの判定だけ宴向けに作る） |
| Ren'Py | screen 言語の `textbutton` / `imagebutton`（`sensitive` が押せるかどうか）、`viewport`（スクロール）、`vbox` / `hbox` | ゲームのスクリプト（`.rpy`） |
| ティラノスクリプト | DOM の `glink` などのボタン要素、`disabled`、スクロールする要素（`scrollHeight` > `clientHeight`） | プラグイン（JavaScript） |

どのエンジンでも、出すイベントの形（`menu` の `buttons: [{name, rect, type, locked}]`、`scroll` など）を DESIGN.md §3 に合わせれば、ハーネス側は変えなくてよい。

### Unity 以外を扱うときにハーネスで変えるところ
- `launcher.py`：実行ファイルの解決と起動引数（`-yomawari -logFile` は Unity 専用）
- `sensors/logtail.py`：読むファイル。`[YOMAWARI]` 行の形式は同じにすれば、パースはそのまま使える
- `platform/`：セーブ・設定の場所、クラッシュダンプの場所
- これらを「エンジンの設定」として `yomawari.toml` の `builds.<名前>` で選べるようにするのが自然（未着手）

## 7. 確かめる

```sh
yomawari builds check <ビルド名>          # boot を受信しました と出ればよい
yomawari inspect -b <ビルド名>            # 画面に出ている menu とボタン名、描画領域を表示する
yomawari inspect -b <ビルド名> --click TitleUI/SettingsButton   # ボタンを押して、入力が届くか確かめる
```

`inspect` が表示するボタン名（`<menu>/<ボタン>`）は、`yomawari.toml` の `[guard.buttons]` にそのまま書ける。セーブと PlayerPrefs は `run` と同じく、起動前に退避して終了後に戻す。

ハーネスを通さずに、出力を直接見ることもできる（実行ファイル名は `ls <.app>/Contents/MacOS/` で調べる）。

```sh
"<.app>/Contents/MacOS/<実行ファイル名>" -yomawari -logFile ~/Desktop/ymw.log
grep YOMAWARI ~/Desktop/ymw.log
```

- `-yomawari` を付けないときに `[YOMAWARI]` 行が0行であること
- タイトル画面で `menu` にボタンが並び、本文で `line` が、選択肢で `choices` が出ること

### boot が出ないとき
1. パッケージを変えたあとにビルドし直したか
2. ビルドに Yomawari のコードが入っているか：`<.app>/Contents/Resources/Data/il2cpp_data/Metadata/global-metadata.dat` に `YomawariLog` という文字列があるか（IL2CPP の場合）
3. 起動引数に `-yomawari` が付いているか（`runs/<ラン ID>/events.jsonl` の `launch` の `cmd`）
