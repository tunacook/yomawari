# yomawari
Unity製ノベルゲームを自動でプレイしてくれる仕組み。

Unity のビルド（`.app` / `.exe`）を、人がゲームを見ていない間（寝ている間・外出中・仕事中など）に自動でプレイさせ、クラッシュ・ハング・進行不能・表示異常を Markdown のレポートにまとめる。設計は [DESIGN.md](DESIGN.md) を参照。

## 対応環境

いまは macOS と Naninovel の組み合わせだけに対応している。

| ノベルゲームのエンジン | macOS（Apple Silicon） | Windows |
|---|---|---|
| Naninovel 1.21 以上 | 対応 | 未対応（DESIGN.md フェーズ7） |
| Naninovel を使わない Unity のゲーム | 未対応 | 未対応 |

Naninovel がないと、パッケージのうち、画面のボタン・セリフの行・選択肢をハーネスに知らせる部分（`Yomawari.Naninovel`）がコンパイルされない。起動・エラーのログは出るが、ハーネスは画面にどのボタンやセリフが出ているかを知れない。

## 技術スタック

| 層 | 使っているもの | 用途 |
|---|---|---|
| ハーネス本体 | Python 3.11 以上（arm64 ネイティブ）、asyncio | ゲームの起動・監視・操作・判定を1つのイベントループで回す |
| パッケージ管理 | [uv](https://docs.astral.sh/uv/)、`pyproject.toml`、`uv.lock`、hatchling | Python 本体・依存の取得、`yomawari` コマンドのインストール |
| CLI・表示 | [Typer](https://typer.tiangolo.com/)、[Rich](https://rich.readthedocs.io/) | `yomawari run` / `doctor` / `builds` などのコマンドと、ラン中の進捗表示 |
| 設定 | TOML（[tomlkit](https://tomlkit.readthedocs.io/)、標準の `tomllib`）、YAML（[PyYAML](https://pyyaml.org/)） | `yomawari.toml`（tomlkit。対話登録で書き足してもコメントが残る）、ゲームのプロファイル（tomllib）、検出ルール `rules.yaml` と役割のキーワード（YAML） |
| プロセス監視 | [psutil](https://psutil.readthedocs.io/) | ゲームの生存・終了コード・CPU・メモリ |
| 入力 | [PyAutoGUI](https://pyautogui.readthedocs.io/) | OS のマウス・キーボード入力でゲームを操作する（ゲーム側に操作を受け付ける仕組みは作らない） |
| macOS の API | [PyObjC](https://pyobjc.readthedocs.io/)（Quartz・Cocoa）、ctypes | ウィンドウの位置と倍率、前面のアプリ、画面収録・アクセシビリティの権限の確認 |
| 画像の判定 | [Ollama](https://ollama.com/)（ローカル）＋画像対応モデル `qwen2.5vl:7b`、[httpx](https://www.python-httpx.org/) | ボタンの見た目からの役割の推定などの補助。判定の主役はルール（`rules.yaml`）で、AI の答えから Critical は出さない |
| テスト | [pytest](https://docs.pytest.org/) | 実ゲームなしで、Player.log のサンプルや模擬ゲームを流し込んで確かめる |
| ゲーム側 | Unity 6000.3 以上、UPM パッケージ `com.tunacook.yomawari`（C#）、Naninovel 1.21 以上（[対応環境](#対応環境)を参照） | 起動引数 `-yomawari` のときだけ Player.log に `[YOMAWARI]` 行（1行 JSON）を出す |

ゲームからハーネスへは Player.log の `[YOMAWARI]` 行、ハーネスからゲームへは OS の入力、の一方向ずつで通信する。主な実行環境は Apple Silicon の Mac。Windows 対応は後日（DESIGN.md フェーズ7）。

## 環境構築

OS ごとの手順は次のファイルにまとめている。

| OS | 手順 | 状況 |
|---|---|---|
| macOS（Apple Silicon） | [docs/setup-macos.md](docs/setup-macos.md) | 主な実行環境。動作確認済みの macOS のバージョンも載せている |
| Windows | [docs/setup-windows.md](docs/setup-windows.md) | 未実装（DESIGN.md フェーズ7）。予定の手順 |

ゲーム側の準備（パッケージ `com.tunacook.yomawari` の導入）はどの OS でも共通で、[docs/game-integration.md](docs/game-integration.md) を参照。

## 使い方

### ランを回す
```zsh
yomawari run --hours 0.5                    # 既定の目的（play: 通常のプレイ）で 30 分
yomawari run --purpose buttons --hours 1    # 目的を選ぶ
yomawari run -p gacha --seed 42             # 乱数シードを固定して再現する
```
終わると `runs/<日時>_<ビルド>/report.md` が開く。ハーネスが OS のマウス・キーボードで操作するので、ラン中は Mac に触らない。

画面をロックしない。macOS はロック中、ほかのアプリを前面に出すことも、マウス・キーボードの入力を送ることもできない。ハーネスはランのあいだ `caffeinate` で自動のスリープを止めるが、手でロックする・蓋を閉じるのは防げない。画面の自動ロックとスクリーンセーバーも止めておく。ほかの注意点は [DESIGN.md §13](DESIGN.md#13-macos実行時の注意) を参照。

夜に画面を消して回すときは `--display-off` を付ける（ゲームが起動して `run.display_off_delay_sec` 秒後に `pmset displaysleepnow` で画面を消す）。その前に、システム設定の「ロック画面」で、ディスプレイがオフのときにパスワードを求めるのを「しない」か長い時間にしておく（すぐロックする設定なら、開始時に注意を出す）。ロックなどで操作できなかった時間は、レポートの「ハーネスの記録」に出る。画面を消しているあいだは macOS が描画を間引くので、FPS・CPU・GPU・メモリの判定（R 系）をしない（外した時間は「ハーネスの記録」に出る）。

#### よく使う組み合わせ
`--hours` は小数でも書ける（`0.25` で 15 分、`1.5` で 1 時間 30 分）。省くと `run.hours`（8 時間）回す。

```zsh
# 寝る前に、夜通し回す（8 時間。ゲームが起動したら画面を消す）
yomawari run --display-off

# 前の夜のランの続きから回す（序盤を毎晩読み直さない）
yomawari run --continue --display-off

# 外出中や仕事中に、時間を決めて回す
yomawari run --hours 2
yomawari run --hours 4.5 -p stuck            # 進行不能を探す

# ハーネスやビルドを直したあと、短く動きを確かめる
yomawari run --hours 0.25

# ボタンの不具合を探す（暗いボタンも押す）
yomawari run -p buttons --hours 3

# 決まった画面で遊ぶ前提で回す（ランのあいだは解像度・画面モードを変えさせない）
yomawari run --screen 1920x1080
yomawari run --fullscreen                    # フルスクリーンで（大きさはゲームに任せる）
yomawari run --fullscreen --screen 1920x1080 # 1920x1080 のフルスクリーンで

# 前のランと同じ乱数で回す（シードはレポートの「ランの情報」にある。ゲームの時間の進み方で操作はずれるので、まったく同じにはならない）
yomawari run --seed 898481646 --hours 1

# 覚えたことを消してから回す（ハーネスの仕様が大きく変わったあとなど）
yomawari run --forget --hours 1

# 登録した別のビルドを回す
yomawari run -b mac_release --display-off
```

| オプション | 内容 | 既定 |
|---|---|---|
| `--purpose` / `-p` | ランの目的（下の表）。`yomawari.toml` で足した目的も選べる | `yomawari.toml` の `run.purpose`（`play`） |
| `--hours` | 実行時間（時間） | `run.hours`（8） |
| `--mode` | 目的の操作のモードを上書きする。`random`（通常の操作だけ）/ `fuzz`（通常の操作＋イレギュラー操作）/ `aging`（操作しない） | 目的の mode |
| `--seed` | 乱数シード。`auto` は毎回変える | `run.seed`（`auto`） |
| `--build` / `-b` | 登録済みビルドの名前 | `default_build` |
| `--build-path` | その場限りで使うビルドのパス（登録しない） | |
| `--no-open` | 終わったあとにレポートを開かない | |
| `--display-off` | ゲームが起動したあと、画面を消す（macOS: `pmset displaysleepnow`） | `run.display_off`（false） |
| `--continue` / `--from-start` | 前のランが終わったときのセーブを置き、その続きから遊ぶ（タイトルの「続きから」と、いちばん新しいセーブを選ぶ。見つからなければはじめから）／はじめから遊ぶ | `run.continue`（false） |
| `--screen` | ランの画面の前提。`1920x1080` なら、その大きさのウィンドウで起動し（ビルドの `args` の画面の指定は置き換える）、ランのあいだは解像度・画面モードを変えるボタンを押さない。途中で変わったら起動し直し、起動したときから違えばランを止める | `run.screen`（空: 決めない） |
| `--fullscreen` / `--windowed` | ボーダーレスのフルスクリーンで起動し、ランのあいだは変えさせない（`--screen` があればその大きさで）／ウィンドウで起動する | `run.fullscreen`（false） |
| `--input` | 入力の方式。`mouse`（マウス中心）/ `keyboard`（キーボード中心。本文送りを Enter・Space、メニューを Esc で押し、ボタンは方向キーでたどって Enter で押す。本文が進まないキー・メニューが開かないキーはマウスに戻す。たどり着けなかったボタンとメニューを開けなかったことは「ハーネスの記録」に出る）。省略するとマウスだけ | `fuzz.input`（`mouse`） |
| `--input-mix` | 主でない方を混ぜる割合（0〜1）。`--input mouse --input-mix 0.2` なら、本文送り・メニュー・ボタンの 2 割をキーで押す | `fuzz.input_mix`（0） |
| `--no-watchdog` | 見張り役を使わない。ふだんは見張り役がハーネス本体を子プロセスで動かし、ハーネス本体が落ちたり固まったりしても、ゲームを終了し、セーブを戻し、レポートを作る | `run.watchdog`（true） |

### ランの目的
デバッグで見たいものに合わせて、操作の選び方とレポートで先に見せるルールを変える。レポートの「サマリ」に目的が載り、重点的に見るルールの指摘は深刻度ごとに先に並び、「（重点）」が付く。

| 目的 | 見たいもの | 動き方 | モード | 重点的に見るルール |
|---|---|---|---|---|
| `play`（既定） | 通常のプレイ | 人が遊ぶように本編を先へ進め、寄り道は少なめ。進め方の経験を使う。質問は、プロファイルに書いた正解で答え、ときどきわざと外れも答える（下の「質問の正解をプロファイルに書く」）。書いていない質問は、本文の文脈から AI（Ollama）に順位を付けさせて試す。壊れやすいタイミング（選択肢の直後・scene の境目など）に、ときどき（3%）エッジケースの操作を差し込む | random | |
| `stuck` | 進行不能 | 本編を進めつつ、ロード・回想などの寄り道と行き来もする。進めなくなったら、抜け出す操作を試さずにそのまま記録して再起動する | random | S-02, S-03, S-01, H-01 |
| `buttons` | ボタンの不具合 | 通常のプレイで進めながら、初めて来た画面で、ときどき 60 秒その画面のボタンを網羅的に押す（暗いボタンも押す。進め方の経験と AI の見当は使わない） | random | S-05, V-07, S-01 |
| `gacha` | 落ちる・固まる操作 | 通常のプレイで進めながら、初めて来た画面・scene で、ときどき 60 秒その場で、壊れやすいタイミング（本文・選択肢・scene・演出・セーブ・ロード・質問に答えた直後）に連打・メニューの開け閉め・巻き戻しなどのイレギュラーな操作を多めに差し込む | random（区間は fuzz） | C-01〜04, H-01, L-01 |
| `rta` | 最短で進めたときの不具合 | 進行につながった操作を強く優先し、寄り道と巻き戻しを避ける。エッジケースは差し込まない | random | S-02, S-03 |
| `aging` | 放置したときの不具合 | 操作せずに起動したままにする | aging | H-01, L-01, C-01, R-01, R-03 |

- ルールの ID は `rules.yaml` を参照。
- `buttons` と `gacha` は、ゲームのプロファイルに書いたゲームの流れ（`[[flow]]`。[DESIGN.md §9「ゲームの流れ」](DESIGN.md)）を使わない。それ以外は流れに沿って進める。
- 目的は `yomawari.toml` の `[purpose.<名前>]` で上書きしたり、新しく足したりできる。書き方と、`[purpose.<名前>.fuzz]`（`[fuzz]` の設定の上書き）に書ける項目は `yomawari.example.toml` のコメントにある。
- 目的ごとの動き方の考え方は [DESIGN.md §9「ランの目的」](DESIGN.md) を参照。

### 質問の正解をプロファイルに書く
選択肢や、推理パートで証拠を選ぶ画面のように答えを選んで進むゲームでは、質問ごとの正解をゲームのプロファイル（`yomawari.profile.toml`）の `[[flow]]` に `answers` として書ける。書いておくと、組み合わせを総当たりせずに先へ進める。下の画面・ボタン・scene の名前は例（[examples/profiles/unity-naninovel.profile.toml](examples/profiles/unity-naninovel.profile.toml)）。

```toml
# 本編の選択肢。正解は選択肢の本文で書く
[[flow]]
name = "選択肢で答える"
start = { choices = true }
steps = [{ choose = true, note = "選択肢を選ぶ" }]  # 選ぶ手順 1

[[flow.answers]]
at = "main/chapter1:120.0"       # 質問の本文の行（line_id。events.jsonl の line や report.md の「場所」に出る。ワイルドカード可）
at_text = "どうしよう……"         # at の行の本文（任意）。違えば行番号がずれたとみなし、この正解を使わない
pick = ["彼女を追いかける"]       # 選ぶ手順ごとに、選ぶものの画面に見える文字
note = "「ここで待つ」はバッドエンド"

# 推理パートで、ゲームが開いた証拠の一覧から、証拠を選んで突きつける（自作の画面）
[[flow]]
name = "証拠を突きつける"
start = { screen = "EvidenceListUI", by = "game" }
steps = [
  { screen = "EvidenceListUI", press = "EvidenceSlot*", note = "証拠を選ぶ" },  # 選ぶ手順 1
  { screen = "EvidenceDetailUI", press = "PresentButton", note = "突きつける" },
]

[[flow.answers]]
# 外れへの返事のあとに同じ一覧を出し直すなど、質問の行がいくつもあるときは配列で書く
at = ["main/chapter2:310.0", "main/chapter2:331.0"]
pick = ["血の付いたハンカチ"]

# 正解がいくつもある質問は、同じ at で answers を並べる
[[flow.answers]]
at = ["main/chapter2:310.0", "main/chapter2:331.0"]
pick = ["駅の防犯カメラの映像"]

# 何も選ばずに一覧を閉じるのが正解の質問は、pick の代わりに、押すボタンを press に書く
[[flow.answers]]
at = "main/chapter4:52.0"
press = "EvidenceListUI/CloseButton"
note = "ここでは何も突きつけない"
```

| 項目 | 内容 |
|---|---|
| `at` | 質問の本文の行（`line_id`。ワイルドカード可）。いくつもあるときは配列で書く。流れを始めたときの直近の本文の行のうち、いちばん新しい行で当たったものを、その質問の正解とする |
| `pick` | 選ぶ手順（ワイルドカードで書いたボタン・選択肢を選ぶ手順。`once` の手順は数えない）ごとに、選ぶものの画面に見える文字。空白を除いて比べる。選択肢は本文で書く。画面から番号だけ・題名だけが読めることがあるので、1つの手順に書き方をいくつも書ける（`[["No.030*", "題名"], "屋上"]`）。ボタンの名前の番号（`#n`）はビルドで変わるので使わない |
| `press` | 何も選ばずに一覧を閉じるのが正解のときなど、選ぶ手順の代わりに押すボタン（`steps` の `press` と同じ書き方）。`pick` とどちらか一方だけ書く。`avoid` に書いたボタンでも、正解として押す |
| `note` | 説明（レポートに出す） |

- 正解を使うかはランの目的で決まる。`play` は書いた正解で答えるが、正解どおりの最短ばかりにならないよう、ときどき（30%）先にわざと外れを答えて、バッドエンドや外れの返事も確かめる。`rta` は外れを答えない。`stuck` は書いた正解を使わず、AI に順位を付けさせて答えを探す（[DESIGN.md §9「答えの選び方」](DESIGN.md)）
- 書いていない質問は、AI に候補の順位を付けさせて試し、見つけた正解をハーネスが覚える（`runs/_experience/<ゲーム>.json`）
- 選ぶ手順の数より多く `pick` を書くと、読み込むときにエラーになる

### そのほかのコマンド
`yomawari --help` で一覧が出る。主なものは次のとおり。

| コマンド | 内容 |
|---|---|
| `yomawari doctor` | 権限・設定・Ollama などの準備ができているか確かめる |
| `yomawari builds` | 登録済みのビルドを一覧する（`builds add` で登録） |
| `yomawari restore` | ランが途中で止まったときに、退避したセーブ・設定を戻す |
| `yomawari prune` | 古いラン（既定は 14 日より前。`run.keep_runs_days`）を消す。`--dry-run` で消すものを表示するだけ。`yomawari run` も始めるときに消す |
| `yomawari experience forget` | ランをまたいで覚えたこと（`runs/_experience/<ゲーム>.json`）を消す。ハーネスの仕様が大きく変わったときなどに使う。`yomawari run --forget` でも、ランの前にすべて消せる |
