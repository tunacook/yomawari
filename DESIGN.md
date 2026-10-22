# Yomawari 設計書

Unity製ノベルゲームのビルド（macOS `.app` / Windows `.exe`）を、人間が寝ている間や外出中に自動でプレイし、クラッシュ・ハング・進行不能・表示異常を検出して、翌朝読むMarkdownレポートにまとめるハーネス。

- 正式名: Yomawari（夜回り）
- CLIコマンド: `yomawari`（エイリアス `ymw`）
- リポジトリ/パッケージ名: `yomawari`

---

## 1. 目的と前提

### やりたいこと
- 意図しないUI操作・OS操作を、壊れやすいタイミングに差し込み、クラッシュや進行不能に気づく
- 無人で数時間〜一晩回し、翌朝レポートを読むだけで済む
- 問題を深刻度別に分類し、再現手順（操作列）と証跡を添える

### 前提・決定事項
| 項目 | 決定 |
|---|---|
| 対象 | Unityエディタではなくビルド済みの `.app` / `.exe`。Development Build でもリリースビルドでも動く |
| ゲーム→ハーネス | Player.log に出る `[YOMAWARI]` 接頭辞の1行JSONのみ（一方向） |
| ハーネス→ゲーム | OSレベルのマウス・キーボード入力のみ |
| ゲーム内Probe（ソケット等） | 作らない |
| 入力デバイス | 当面マウス＋キーボードのみ。ゲームパッドは後日 |
| 実行環境 | 主にM3 Mac（Apple Silicon）。Windowsは後日、同じハーネスをWindows機に置く |
| 負荷 | ローカル負荷はかけてよい |
| 通知 | 不要。完了後にMarkdownを開くだけ |
| AI | 判定と制御は原則コード（ルールベース）。AIはローカルモデル（Ollama）を補助的に使う |
| 言語 | Python 3.11+、arm64ネイティブ、環境管理は uv |

### やらないこと（現時点）
- ゲーム内にソケット型の操作受付Probeを入れること
- OCR（選択肢・ボタンの矩形はログで取得する）
- スマホ等への通知
- ゲームパッド入力（`InputDriver` の差し替え口だけ用意）
- シナリオ本文の内容の正しさの判定

---

## 2. システム構成

### システムごとの担当
| システム | 中身 |
|---|---|
| macOS標準機能 | `launchd`（時刻起動）、`caffeinate`（スリープ防止）、画面収録・入力API |
| Unityビルド（C#） | ゲーム本体、`com.tunacook.yomawari` パッケージ（`[YOMAWARI]` 行の出力） |
| Pythonハーネス | ウォッチドッグ（別プロセス）、ハーネス本体（センサー、ファジング、検出器、後処理） |
| Ollama（ローカル） | テキストモデル（操作選択・任意）、画像対応モデル（画面判定・誤検知除去・再現手順の文章化） |
| ファイル | `yomawari.toml`、`rules.yaml`、Player.log、`runs/` |

### システム間のやりとり
| 経路 | 手段 |
|---|---|
| launchd → ウォッチドッグ | 時刻起動（`caffeinate` でラップ） |
| ウォッチドッグ → ゲーム | プロセス起動（`-yomawari`、`-logFile` を自動付与） |
| ゲーム → ハーネス | `YomawariLog` → Player.log → センサーがtail |
| ハーネス → ゲーム | OS入力（`pyautogui`） |
| ハーネス → ゲーム（観測） | 画面キャプチャ、ウィンドウ情報、プロセス統計 |
| ハーネス ↔ Ollama | `http://localhost:11434` へのHTTP（`httpx`） |
| 後処理 → runs/ | JSONL、証跡、`report.md`、完了後に `open` |

### ハーネス本体内部のデータの流れ
```
センサー群 ──(イベント)──> イベントバス ──┬─> 状態トラッカー ──> 戦略(Decider) ──> ガード ──> InputDriver
                                         ├─> 検出器群（rules.yaml） ──> ISSUE候補
                                         └─> events.jsonl 書き込み
```
- `asyncio` で1プロセス内に並行実行
- 検出器は戦略とは独立にイベントを購読する

---

## 3. ゲーム側: YomawariLog（C#）

ゲーム側は UPM パッケージ `com.tunacook.yomawari`（このリポジトリの `unity/com.tunacook.yomawari/`）として提供する。ゲームの `Packages/manifest.json` に Git URL を1行足すだけで入り、ゲームのコードからは呼ばない（`RuntimeInitializeOnLoadMethod` で自動的に始まる）。ゲーム本体はこのパッケージに依存しないので、外すときは manifest の1行を消すだけでよい。

- `Debug.Log` で `[YOMAWARI]` 接頭辞の1行JSONを出す（Player.log に混ざって書かれる）。`LogOption.NoStacktrace` を付け、Log にスタックを付ける設定でも行にスタックを付けない
- `Application.logMessageReceived` を購読し、Error / Exception / Assert を `log_error` イベントとして出す。Player.log の本文からは Log と Error を区別できない（Stack Trace Logging の設定次第でどちらにもスタックが付く）ため、L-01 はこのイベントで判定する。メインスレッドのログだけが対象（別スレッドの例外は後日対応）
- 数値はロケールに関係なく `CultureInfo.InvariantCulture` で書く（小数点がカンマになる環境でも JSON が壊れないように）

### パッケージの構成
| asmdef | 中身 | 依存 |
|---|---|---|
| `Yomawari` | `YomawariFormat`（行の組み立て・エスケープ・矩形の正規化）、`YomawariLog`（boot・log_error）、`YomawariHeartbeat`、`YomawariRect`（`RectTransform` → 正規化矩形） | なし |
| `Yomawari.Tests.Editor` | EditMode テスト（ゲームの manifest の `testables` に入れると Test Runner に出る） | `Yomawari` |
| `Yomawari.Naninovel` | `YomawariNaninovel`（サービスのイベントから `scene` / `line` / `line_done` / `choices_done` / `save` / `load`）、`YomawariUiWatcher`（表示中の UI を 0.2 秒ごとに見て `menu` / `menu_close` / `choices` / `transition`）。Naninovel 1.21 以上があるときだけコンパイル（`versionDefines` + `defineConstraints`） | `Yomawari`、Naninovel、uGUI |

ゲームへの組み込み手順は [docs/game-integration.md](docs/game-integration.md)。ゲーム固有の UI（Naninovel のイベントで取れないもの）を観察する場合は、ゲームのリポジトリ側に小さな asmdef を作り、そこからゲームの UI を読んで `YomawariLog.Emit` を呼ぶ。ゲーム本体の asmdef からは参照しない。

### 有効化
- 起動引数 `-yomawari` があるときだけ出力（リリースビルドの製品版では出ない）
- `[Conditional]` 属性は使わない（リリースで消えるため）。実行時フラグで判定

### 行フォーマット
- 1イベント＝1行。本文中の改行・引用符は必ずエスケープ
- 共通フィールド: `seq`（連番、欠落検出用）、`t`（realtimeSinceStartup）、`f`（frameCount）、`ev`
- 矩形は画面に対する正規化座標 `[x0, y0, x1, y1]`（0〜1、左上原点）。`RectTransformUtility.WorldToScreenPoint` で四隅を取り `Screen.width/height` で割る。Unityのスクリーン座標は左下原点なのでy反転に注意

### イベント一覧（初期）
| ev | 主なフィールド | 用途 |
|---|---|---|
| `boot` | `version`, `build_type`, `build_guid`, `unity` | 起動確認、ビルド取り違え検出（ビルド時刻は実行時に取れないため `build_guid` で代える） |
| `heartbeat` | `fps`, `gc_mem` | 1秒ごと。ハング検出、エイジング |
| `scene` | `name`（Naninovel ではスクリプトのパス） | 進行把握 |
| `line` | `label`, `line_id`, `speaker`, `text`, `printer`, `append` | 進行把握、テンポ。Naninovel では `label` はスクリプトのパス、`line_id` は `<パス>:<行>.<インライン>` |
| `line_done` | `line_id` | 文字送り完了。操作の待ち合わせ |
| `choices` | `items: [{id, text, rect, locked}]` | クリック候補。並びか矩形が変わったときにも出し直す |
| `choices_done` | `id`, `index` | 選択確定。`index` は直前の `choices` の中での順番（不明なら -1） |
| `menu` | `name`, `modal`, `buttons: [{name, rect}]` | メニュー表示とクリック候補。押せるボタンの並びか矩形が変わったときにも出し直す |
| `menu_close` | `name` | 応答確認 |
| `transition` | `phase: start/end`, `name` | 操作待ち、進行不能判定 |
| `resolution` | `w`, `h`, `mode` | 変化時のみ（`Update` で監視）。解像度テスト |
| `save` / `load` | `slot`, `type`, `phase: start/end` | セーブ検証、分岐の巻き戻し |
| `bgm_play` / `bgm_stop` / `voice` | `id` | 音声の整合性 |
| `error_shown` | `kind` | ゲームがエラー表示を出した（セーブ失敗等） |
| `log_error` | `type`（Error / Exception / Assert）, `message`, `stack` | `logMessageReceived` から出す。L-01 の判定 |

### Player Settings
- Use Player Log: 有効（デフォルト）
- Stack Trace Logging: Error/Exception は Script Only 以上（`log_error` の `stack` に入る）。Log / Warning は任意（スタックを付けても L-01 の判定には影響しない。`[YOMAWARI]` 行に付くスタックはハーネスが読み飛ばす）
- Development Build ではIL2CPPの場合「IL2CPP Stacktrace Information」を行番号付きに

---

## 4. Pythonパッケージ

### 言語に Python を選んだ理由
フェーズ1（Player.log の追跡、プロセス監視、ルール判定、レポート）はどの言語でも書ける。差が出るのはフェーズ2以降の次の3つで、どれも Python がいちばん素直に書ける。

| 用途 | Python | Go | Node（TypeScript） |
|---|---|---|---|
| 画像判定（差分、テンプレート照合、表示異常） | `opencv-python` / `numpy` をそのまま使える | gocv は cgo と OpenCV のビルドが必要 | sharp で縮小・差分はできるが、OpenCV 相当は保守が止まりがち |
| OS入力・ウィンドウ情報・スクショ | `pyautogui` / `pywinctl` / `mss`。足りなければ pyobjc で Quartz を直接呼べる | robotgo（cgo）か、CoreGraphics を cgo で書く | nut.js / robotjs などネイティブアドオン頼みで、保守がまちまち |
| Ollama | 公式クライアントがある。httpx でも足りる | HTTP で足りる | 公式クライアントがある |

Go や Node のほうが優れている点と、それを今は重視しない理由:
- Go は1バイナリで配布でき、uv も Python も入れずに動かせる。プロセス監視やタイムアウトも goroutine と context で堅く書ける。いまの実行環境は手元の Mac が主で、配布の必要はまだない
- Node は非同期 I/O を素直に書け、レポートを Web の画面にするなら強い。ただし重い部分（画像と OS 入力）ではネイティブアドオンの手間が上回る
- Python の弱点のうち、環境構築は uv で、型の弱さは型ヒントで補う。処理は毎秒のログと数秒おきの画像判定なので、速度は問題にならない

見直すきっかけ: 複数の機材に配って回したくなったとき。そのときも全部を書き換えず、起動と監視だけを Go にして画像判定は Python に残す、という分け方を先に考える。OS 差分は `platform/` と `input/` に閉じ込めてあるので、分けるときもこの境界を使える。

### 依存
`typer`, `rich`, `httpx`, `psutil`, `mss`, `pywinctl`, `pyautogui`, `numpy`, `opencv-python`, `pyyaml`
（`tomllib` は標準ライブラリ）

### pyproject.toml（抜粋）
```toml
[project]
name = "yomawari"
requires-python = ">=3.11"

[project.scripts]
yomawari = "yomawari.cli:main"
ymw = "yomawari.cli:main"
```
`main()` では `app(prog_name="yomawari")` としてヘルプ表示を統一する。開発時は `uv tool install --editable .`。

### ディレクトリ構成
```
yomawari/
├─ yomawari/
│   ├─ cli.py            # typer エントリポイント
│   ├─ config.py         # yomawari.toml の読み書き、ビルド解決、対話登録
│   ├─ watchdog.py       # 別プロセス。ハーネス本体とゲームの起動・再起動・時間予算・復元
│   ├─ launcher.py       # ビルド起動、.app の実行ファイル解決、引数付与
│   ├─ bus.py            # asyncio イベントバス
│   ├─ state.py          # 状態トラッカー
│   ├─ sensors/
│   │   ├─ logtail.py    # Player.log tail、[YOMAWARI] 行と Unity の Error/Exception のパース
│   │   ├─ screen.py     # mss によるキャプチャ、リングバッファ
│   │   ├─ window.py     # pywinctl によるウィンドウ矩形
│   │   ├─ process.py    # psutil
│   │   └─ crash.py      # クラッシュダンプフォルダ監視
│   ├─ platform/
│   │   ├─ mac.py        # OS固有の差分（パス、権限確認、caffeinate 等）
│   │   └─ win.py
│   ├─ input/
│   │   ├─ base.py       # InputDriver インターフェース
│   │   └─ mouse_kb.py   # pyautogui 実装
│   ├─ fuzz/
│   │   ├─ loop.py       # トリガー → 操作 → 応答確認
│   │   ├─ ops_ui.py     # UI上のイレギュラー操作カタログ
│   │   ├─ ops_os.py     # OS上の操作カタログ
│   │   ├─ guard.py      # 禁止操作、押す直前の存在確認
│   │   ├─ coverage.py   # 状態×操作の試行回数
│   │   └─ deciders.py   # Decider: random / local(Ollama) / jev(将来)
│   ├─ detect/
│   │   ├─ engine.py     # rules.yaml を読んで評価
│   │   └─ visual.py     # 黒画面・静止・SSIM 等の画像特徴量
│   ├─ judge/
│   │   └─ ollama_vlm.py # Judge: 画像対応モデルでの判定
│   ├─ post/
│   │   ├─ aggregate.py  # 重複排除、統合、前回比較
│   │   ├─ minimize.py   # 操作列の最小化（デルタデバッギング）
│   │   └─ report.py     # Markdown 生成
│   └─ replay.py
├─ unity/
│   └─ com.tunacook.yomawari/   # ゲーム側の UPM パッケージ（§3）
├─ rules.yaml
├─ yomawari.example.toml
├─ tests/
└─ pyproject.toml
```

---

## 5. CLI

### コマンド
```bash
yomawari run [--build NAME | --build-path PATH] [--hours 8] [--mode fuzz|aging|random] [--seed N] [--no-open]
yomawari replay <issue-id>
yomawari minimize <issue-id>
yomawari report <run-id>
yomawari builds list | add NAME PATH | edit NAME | check NAME
yomawari doctor
yomawari schedule install --at 01:00 | uninstall
yomawari restore
```

### ビルドの解決順
1. `--build-path`（その場限り、登録しない）
2. `--build NAME`
3. `yomawari.toml` の `default_build`

### 初回の対話登録
- ビルド未登録で `run` した場合、対話でパスを入力させる（Finderからのドラッグ&ドロップ可）
- 入力の正規化: 前後の引用符除去、`\ ` のアンエスケープ、`~` 展開
- 検証: 存在するか / `.app` なら `Contents/Info.plist` の `CFBundleExecutable` を解決できるか / 今のOSで実行できる形式か / 起動して `boot` 行が出るか（出なければ保存はするが警告）
- 名前を尋ね（デフォルトはアプリ名の小文字）、`yomawari.toml` に保存。`default_build` 未設定ならそれにする
- **標準入力が端末でない場合（launchd等）は入力を待たず終了コード2で終了**
- `schedule install` 時にビルド未登録なら、そこで登録を促す
- 表示は `rich` の `Prompt` / `Confirm`

### 実行中の表示
```
[01:23:45] mac_dev  ops 3,412  restarts 2  issues: C1 M4 m7
           last: fullscreen_toggle @ ch2_park_03
```
Ctrl+C でもゲーム終了・セーブ復元・途中までのレポート出力を行ってから終わる。

### 終了コード
| コード | 意味 |
|---|---|
| 0 | 完了、Criticalなし |
| 1 | 完了、Criticalあり |
| 2 | ハーネス自体のエラー |

### doctor の確認項目
- Python が arm64 で動いているか（`platform.machine()`）
- ターミナルに「画面収録」「アクセシビリティ」権限があるか
- Ollama が起動しており、設定のモデルが pull 済みか
- 登録ビルドのパスが存在するか

---

## 6. 設定ファイル

`yomawari.toml` は `.gitignore` 対象。`yomawari.example.toml` をコミットする。

```toml
default_build = "mac_dev"

[builds.mac_dev]
path = "~/Builds/Rachel_Dev/Rachel.app"
args = ["-screen-fullscreen", "0", "-screen-width", "1280", "-screen-height", "720"]
watermark_mask = { anchor = "bottom-right", w = 220, h = 40 }   # Development Build の透かし

[run]
hours = 8
seed = "auto"
open_report = true
keep_runs_days = 14

[expect]                      # Player Settings の期待値（判定に使う）
run_in_background = false
resizable_window = true
allow_fullscreen_switch = true

[exclude]
dark_labels = ["ch1_blackout"]   # 意図的な暗転・長い静止演出

[decider]
kind = "random"              # random | local | jev
model = ""

[judge]
kind = "local"               # local | none
endpoint = "http://localhost:11434"
model = "<画像対応モデル名:タグ>"
```

---

## 7. ラン・ライフサイクルとウォッチドッグ

1. **準備**: セーブ（`persistentDataPath`）と設定（PlayerPrefs）を退避、テスト用初期セーブを配置、`runs/<id>/` 作成
2. **起動**: `-yomawari -logFile runs/<id>/player.log` を付与して起動、`boot` を待つ（30秒）
3. **実行**: ファジングループ。クラッシュ時は証跡回収→再起動→継続
4. **終了処理**: ゲーム終了、クラッシュダンプ回収、セーブ・設定の復元
5. **後処理**: 集約・最小化・画像判定・Markdown生成・`open`

ウォッチドッグ（別プロセス）の責務:
- ハーネス本体とゲームの起動・再起動（ハーネス自体の異常もレポートに記録）
- 時間予算、同一箇所での連続クラッシュ時の打ち切り
- 終了時のセーブ・設定の復元。異常終了した場合は次回起動時に未復元を検出して復元
- `caffeinate -dimsu` の下で動く

保存場所（macOS、ビルド）:
- セーブ: `~/Library/Application Support/<バンドルID>/`（`Info.plist` の `CFBundleIdentifier`。例: `com.Company.Product`）
- PlayerPrefs: `~/Library/Preferences/<バンドルID>.plist`
- `~/Library/Application Support/<Company>/<Product>/` と `~/Library/Preferences/unity.<company>.<product>.plist` はエディタ側のもの。ビルドの退避・復元の対象にしない
- Player.log 既定: `~/Library/Logs/<Company>/<Product>/Player.log`
- クラッシュ: `~/Library/Logs/DiagnosticReports/*.ips`

（Windows: セーブ `%USERPROFILE%\AppData\LocalLow\<Company>\<Product>\`、PlayerPrefs `HKCU\Software\<Company>\<Product>`、クラッシュ `%TEMP%\<Company>\<Product>\Crashes\`）

---

## 8. センサー

| センサー | 実装 | 出すイベント |
|---|---|---|
| ログ | Player.log を改行単位でtail（部分行はバッファ） | `[YOMAWARI]` 行のイベント、Unity の Error/Exception/Warning |
| 画面 | `mss`、ウィンドウ矩形で切り出し、直近30秒程度のリングバッファ | 静止度、単色度、フレーム |
| ウィンドウ | `pywinctl` | 矩形、最小化、フォーカス |
| プロセス | `psutil` | 生存、終了コード、CPU、RSS、スレッド数 |
| クラッシュ | フォルダ監視 | ダンプ出現 |

座標:
- 座標はクライアント領域に対する0〜1で扱い、絶対座標をキャッシュしない
- 操作前に毎回ウィンドウ矩形を取り直し、変わっていたらキャッシュ破棄
- 「正規化座標 → クライアント座標 → スクリーン座標」の変換を1か所に集約
- Retina: キャプチャはピクセル、入力はポイント単位（2倍差）に注意

---

## 9. ファジング

### 基本ループ
1. 通常進行（クリック送り、ランダムな選択）
2. トリガー発火 → イレギュラー操作を1つ差し込む
3. 応答確認（決まった操作に決まったイベントが返るか）
4. 異常なら証跡回収、必要なら再起動して継続

### トリガー
ログイベント（`transition` 開始、`choices` 表示、`save` 開始、`line` 表示直後など）の受信から0〜500msのランダム遅延で操作を差し込む。時間ベースのランダム差し込みも併用。

### UI上のイレギュラー操作
高速連打（左クリック/Enter/Space）、選択肢のダブルクリック・2択ほぼ同時クリック、フェード中のクリック/右クリック/ホイール/Esc、選択肢表示中のメニュー・バックログ・セーブ、セーブ中のロード、ロード中のタイトル戻り、スキップとオートの高速切り替え、スキップキー押しっぱなし、音声再生中のタイトル戻り、文字速度・音量の極端値、UI外クリック、ドラッグ

### OS上の操作
フォーカス喪失と復帰、最小化と復元、リサイズ・移動、フルスクリーン切替（Mac: Cmd+Ctrl+F）、Cmd+Q/閉じるボタン、セーブ中の強制終了、セーブフォルダ書き込み不可、壊れたセーブ・設定での起動、二重起動、高CPU負荷

（スリープ復帰・オーディオデバイス切替は手動チェックリストへ）

### 操作の選択（Decider）
```python
class Decider(Protocol):
    def choose(self, state: dict, candidates: list[Op]) -> list[tuple[Op, float]]: ...
```
- 初期実装は `random`: 「状態×操作」の試行回数を記録し、未試行を優先する重み付きランダム
- `local`: Ollama の小さいテキストモデル（JSONスキーマで出力を固定）
- `jev`: 将来の選択肢（TypeSafe Jev。「普通のプレイヤーがやりそうな操作」の確率が低いものを優先する使い方）

### ガード
- 「Exit」「タイトルへ」はイレギュラーモード以外では禁止
- データ削除・外部リンクは押さない
- 押す直前に、押す位置の要素が最新の状態に含まれているか確認
- 同一状態で同じ操作を繰り返そうとしたら次点へ

### 応答確認
イレギュラー操作の後に毎回、決まった操作（例: メニューを開く→`menu`、閉じる→`menu_close`、クリック→`line`/`line_done`）をして、一定時間内にイベントが返るか確認。

### 猶予期間
操作の種類ごとに、直後の画面系・進行系ルールの判定を止める時間を `rules.yaml` で設定（例: リサイズ2秒）。

---

## 10. 検出ルール

`rules.yaml` で定義し、検出エンジンがそれを読んで評価する。閾値・除外・猶予期間はコードに書かない。ルールは今後追加・変更する前提。

### 深刻度
| 深刻度 | 意味 |
|---|---|
| Critical | プレイ継続不能、またはデータ消失 |
| Major | 継続可能だが明らかな不具合 |
| Minor | 軽微 |
| Info | 判定材料、ハーネス自身の不調 |

### 初期ルール
| ID | 条件 | 深刻度 |
|---|---|---|
| C-01 | ハーネスが終了させていないのにプロセス消滅、または終了コード≠0 | Critical |
| C-02 | クラッシュダンプ出現 | Critical |
| C-03 | 起動から30秒以内に `boot` なし | Critical |
| C-04 | 終了操作から10秒経ってもプロセスが残る | Major |
| H-01 | プロセス生存中にハートビートが10秒途絶（CPU使用率を付記） | Critical |
| S-01 | 応答確認のイベントが5秒以内に来ない（ハートビートは生存） | Critical |
| S-02 | 本文表示中に20回クリックしても `line`/`choices`/`scene` が来ない | Critical |
| S-03 | `transition` 開始から10秒で終了が来ない | Critical |
| S-04 | 同じ行IDの並びが繰り返されるだけ（K回以上） | Major |
| S-05 | ログの矩形中心をクリックしても反応が3回連続でない | Major |
| L-01 | `log_error` イベント（Error / Exception / Assert） | Major（直後にS系ならCriticalに統合） |
| L-02 | TextMeshProの未収録文字警告 | Minor |
| L-03 | リソース読み込み失敗 | Major |
| L-04 | 同じ警告が1分間に100回以上 | Minor |
| L-05 | `[YOMAWARI]` の seq 欠落 | Info |
| V-01 | 黒・単色が3秒以上（`transition` 中と除外ラベルは除く） | Major |
| V-02 | 新しい `line` が来たのに画面差分がほぼゼロ | Major |
| V-03 | 復元操作後もウィンドウが見えない/最小化のまま | Major |
| V-04 | `resolution` イベントと実測の描画領域が不一致（Retina倍率考慮） | Major |
| V-05 | ログの選択肢・ボタン矩形が画面外 | Major |
| V-06 | 同ラベルのベースラインとのSSIMが閾値以下 | 候補（画像判定へ） |
| A-01 | `bgm_stop` なしで次の `bgm_play` | Major |
| A-02 | タイトル戻り後も音声イベントが続く | Minor |
| F-01 | `save` が来たのにセーブファイルの更新日時が変わらない | Critical |
| F-02 | セーブ中強制終了→再起動後のロードで例外、またはスロット消失 | Critical |
| F-03 | 設定変更→再起動後に値が戻っている | Major |
| F-04 | 書き込み不可フォルダでセーブ→クラッシュ（`error_shown` で継続できれば合格） | Critical |
| R-01 | 安定期間後のメモリ増加傾向が閾値超え（回帰直線の傾き） | Major |
| R-02 | 遷移中以外でFPS<20が10秒以上 | Minor |
| R-03 | スレッド数が単調増加 | Minor |

### 運用ルール
- 統合: 同時間帯のISSUEは原因側にまとめる（L-01直後のS-01は1件のCriticalとし、スタックを添付）
- 重複排除キー: ルールID＋行ID（または例外の型＋スタック先頭）。発生回数を表示
- ハーネス自身の不調（キャプチャ失敗、ログ読み漏れ）はレポート末尾の「実行環境」欄に分ける

---

## 11. 画像判定（Judge）

```python
class Judge(Protocol):
    def judge(self, shots: list[Path], context: dict) -> Verdict: ...
```

- ラン中ではなく後処理でまとめて実行（操作タイミングへの影響を避ける）
- 対象: V-06候補、V-01の閾値付近、イレギュラー操作直後、他ルールのISSUE候補、少数のランダム抽出
- 入力: 対象スクショ（長辺1024px程度、透かしマスク）、ログ矩形からの切り出し（本文枠・選択肢）、ベースライン（あれば）、ログ由来の期待値（scene, label, expected_text_visible, speaker, choices_count, menu_open, transition, resolution, last_operation）
- Ollama `/api/chat` に `images`（base64）、`format`（JSONスキーマ）、`temperature: 0`
- 出力スキーマ: `screen_state`, `text_window`, `choices_visible`, `ui_overlap`, `out_of_bounds`, `tofu_or_boxes`, `vs_baseline`, `confidence`（各項目に unknown / not_applicable を用意）
- モデルの答えはPythonのルールで深刻度に変換。**Criticalは画像判定から出さない**
- `confidence: low` は「要目視」欄へ
- Major判定は全体画像と切り出し画像で二重確認し、一致した場合のみ確定
- 日本語本文の読み取り・照合はさせない（文字の有無・欠け・豆腐まで）
- モデル名とタグを固定し、レポートに記録

---

## 12. 後処理とレポート

- 重複排除、統合、前回 `baseline.json` との比較（新規/解消/継続）
- 失敗した操作列の最小化（デルタデバッギング: 半分ずつ外して再生し、再現する限り削る）
- 画像判定による誤検知除去、再現手順の文章化（ローカルLLM）
- `report.md` を深刻度別に出力し、`open`（Windowsは `os.startfile`）

### レポート構成
1. サマリ（ビルド、実行時間、操作数、再起動回数、深刻度別件数、前回比）
2. Critical / Major / Minor / Info（各ISSUE: ルールID、発生回数、最初の発生時刻、行ID/シーン、最小化した操作列、`yomawari replay <id>` コマンド、スクショ、ログ抜粋、画像判定結果）
3. 要目視（confidence low）
4. カバレッジ（到達したラベル、試行した状態×操作の数）
5. 実行環境（ハーネス自身の不調、使用モデル）

各ISSUEに正誤のチェック欄を付け、判定精度の検証データとして蓄積できるようにする。

### runs/ 構成
```
runs/
├─ 2026-10-18_0100_mac_dev/
│   ├─ events.jsonl
│   ├─ player.log
│   ├─ shots/            # ISSUE_<ルールID>_step<番号>.png
│   ├─ crash/
│   ├─ saves/            # 問題発生直前のセーブ
│   └─ report.md
├─ coverage.json         # ラン間で引き継ぐ状態×操作の試行回数
└─ baseline.json         # 既知ISSUEのシグネチャ、ラベルごとのベースラインスクショ参照
```
古いランは `keep_runs_days` で自動削除。

---

## 13. macOS実行時の注意

- 画面がロックされるとOS入力もキャプチャも動かない。ラン中は自動ロック・スクリーンセーバーを止め、ログインしたままにする
- ゲームウィンドウは最前面・非遮蔽が前提。実行中はそのMacを他の作業に使えない
- ターミナル（または実行元）に「画面収録」「アクセシビリティ」権限が必要
- Python は arm64 ネイティブで。x86_64 Python が Rosetta で動くと依存ライブラリのアーキテクチャが食い違う
- Steamworks組み込み済みビルドでは `SteamAPI_RestartAppIfNecessary` によりSteam経由で再起動されプロセスを見失うことがある。ビルドの隣に `steam_appid.txt` を置くか、`-yomawari` 時は無効化
- フルスクリーンは Fullscreen Window（ボーダーレス）でテスト。切り替えアニメーション中はキャプチャが途切れるので矩形安定待ちを入れる

---

## 14. 実装フェーズ

1. **骨組み**: `pyproject.toml`、CLI（`run`/`builds`/`doctor`）、`yomawari.toml` と対話登録、ランチャ（`.app` 解決）、Player.log tail、`boot`/`heartbeat` の受信、C-01〜03・H-01・L-01、最小限の `report.md`、セーブ退避と復元
2. **ファジング**: イベントバス・状態トラッカー、`InputDriver`（マウス・キーボード）、座標変換、UI操作カタログ、`random` Decider、ガード、応答確認、S系ルール、`rules.yaml` エンジン
3. **画面とOS操作**: `mss` キャプチャとリングバッファ、V-01〜05、OS操作カタログ、F系・R系・A系ルール
4. **再現**: `events.jsonl` からの `replay`、`minimize`、前回比較、重複排除・統合
5. **画像判定**: Ollama Judge、V-06、誤検知除去、要目視欄、再現手順の文章化
6. **無人運転**: ウォッチドッグの別プロセス化、`schedule install`（launchd plist生成）、`caffeinate`、古いランの削除
7. **後日**: Windows対応、`local`/`jev` Decider、ゲームパッド（`InputDriver` 追加と `ui_focus`/`input_device` イベント）

各フェーズで、ゲームなしでも動くテスト（Player.log のサンプルを流し込むモックゲーム）を用意する。

---

## 15. 未確定事項

- ゲームのシナリオ実装の方式（自作か、既存のノベルエンジンか）。`YomawariLog.Emit` の呼び出し箇所が変わる
- 画像対応モデルとテキストモデルの具体的な選定（Macのメモリ量次第）
- ベースラインスクショの更新タイミング（手動承認か自動か）
- 意図的な暗転・静止演出ラベルの洗い出し
