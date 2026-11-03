# yomawari
Unity製ノベルゲームを自動でプレイしてくれる仕組み。

Unity のビルド（`.app` / `.exe`）を夜間に自動でプレイさせ、クラッシュ・ハング・進行不能・表示異常を Markdown のレポートにまとめる。設計は [DESIGN.md](DESIGN.md) を参照。

## 技術スタック

| 層 | 使っているもの | 用途 |
|---|---|---|
| ハーネス本体 | Python 3.11 以上（arm64 ネイティブ）、asyncio | ゲームの起動・監視・操作・判定を1つのイベントループで回す |
| パッケージ管理 | [uv](https://docs.astral.sh/uv/)、`pyproject.toml`、`uv.lock`、hatchling | Python 本体・依存の取得、`yomawari` コマンドのインストール |
| CLI・表示 | [Typer](https://typer.tiangolo.com/)、[Rich](https://rich.readthedocs.io/) | `yomawari run` / `doctor` / `builds` などのコマンドと、ラン中の進捗表示 |
| 設定 | TOML（[tomlkit](https://tomlkit.readthedocs.io/)、標準の `tomllib`）、YAML（[PyYAML](https://pyyaml.org/)） | `yomawari.toml`（tomlkit。対話登録で書き足してもコメントが残る）、ゲームのプロファイル（tomllib）、検出ルール `rules.yaml` と役割のキーワード（YAML） |
| プロセス監視 | [psutil](https://psutil.readthedocs.io/) | ゲームの生存・終了コード・CPU・メモリ |
| 入力 | [PyAutoGUI](https://pyautogui.readthedocs.io/) | OS のマウス・キーボード入力でゲームを操作する（ゲームに操作の受け口は作らない） |
| macOS の API | [PyObjC](https://pyobjc.readthedocs.io/)（Quartz・Cocoa）、ctypes | ウィンドウの位置と倍率、前面のアプリ、画面収録・アクセシビリティの権限の確認 |
| 画像の判定 | [Ollama](https://ollama.com/)（ローカル）＋画像対応モデル `qwen2.5vl:7b`、[httpx](https://www.python-httpx.org/) | ボタンの見た目からの役割の推定などの補助。判定の主役はルール（`rules.yaml`）で、AI の答えから Critical は出さない |
| テスト | [pytest](https://docs.pytest.org/) | 実ゲームなしで、Player.log のサンプルや模擬ゲームを流し込んで確かめる |
| ゲーム側 | Unity 2022.3 以上、UPM パッケージ `com.tunacook.yomawari`（C#）、Naninovel 1.21 以上（任意） | 起動引数 `-yomawari` のときだけ Player.log に `[YOMAWARI]` 行（1行 JSON）を出す |

ゲームからハーネスへは Player.log の `[YOMAWARI]` 行、ハーネスからゲームへは OS の入力、の一方向ずつで通信する。主な実行環境は Apple Silicon の Mac。Windows 対応は後日（DESIGN.md フェーズ7）。

## 環境構築（macOS / Apple Silicon）

### 1. uv を入れる
[uv](https://docs.astral.sh/uv/) は Python のパッケージ・プロジェクト管理ツール。このリポジトリでは、Python 本体の取得、仮想環境（`.venv`）の作成、依存パッケージのインストール、`yomawari` コマンドのインストールをすべて uv で行う（pip / venv / pyenv の代わり）。

pip / venv / pyenv ではなく uv を使う理由:
- **arm64 の Python を確実に使える**: Mac によっては `/usr/local/bin/python3` が x86_64（Rosetta）で、pip / venv はそれをそのまま使ってしまう。uv はマシンに合ったビルド済みの Python を自分で取得し、`pyproject.toml` の設定でシステムの Python を使わないようにしている
- **道具が1つで済む**: pyenv・venv・pip・pipx の役割を uv だけで担えるので、手順が `uv sync` と `uv tool install` の2つで済む
- **依存を固定できる**: `uv.lock` で依存のバージョンを固定し、別の Mac や Windows 機でも同じ環境を再現できる

`pyproject.toml` は標準形式なので、uv がなくても、arm64 の Python で `python -m venv .venv && .venv/bin/pip install -e .` とすれば動く（ただし `uv.lock` による固定は効かない）。

```zsh
brew install uv
```
- 「Xcode のライセンスに同意していない」というエラーで止まる場合は、先に `sudo xcodebuild -license accept` を実行する。
- brew を使わない場合は、公式インストーラ `curl -LsSf https://astral.sh/uv/install.sh | sh` でも入る（インストール先は `~/.local/bin`）。

### 2. PATH を通す
`uv tool` で入れたコマンドは `~/.local/bin` に置かれる。`~/.zshrc` に次の行がなければ足す。
```zsh
export PATH="$HOME/.local/bin:$PATH"
```
足したあとは、開いているターミナルで読み込み直す（新しいタブやウィンドウを開いてもよい）。読み込み直さないと `zsh: command not found: uv` になる。
```zsh
source ~/.zshrc
which uv    # ~/.local/bin/uv と出ればよい
```

### 3. リポジトリの準備とテスト
```zsh
git clone git@github.com:tunacook/yomawari.git && cd yomawari
uv sync            # arm64 の Python 3.12 を uv が自動で取得する
uv run python -c "import platform; print(platform.machine())"   # arm64 と出ればよい
uv run pytest -q
```
x86_64 の Python（`/usr/local/bin/python3` など）を拾わないよう、`pyproject.toml` で uv 管理の Python に固定している。

### 4. コマンドとして入れる
```zsh
uv tool install --editable .
yomawari --help    # ymw でも同じ
```
editable で入れているので、ブランチを切り替えたりコードを変えたりしても入れ直しは要らない。依存パッケージを変えたときだけ、もう一度 `uv tool install --editable .` を実行する。

### 5. 設定とビルドの登録
```zsh
cp yomawari.example.toml yomawari.toml     # yomawari.toml はコミットしない
yomawari builds add mac_dev ~/Builds/Rachel_Dev/Rachel.app
```
ビルドが未登録のまま `yomawari run` を実行した場合も、対話で登録できる。

### 6. macOS の権限
yomawari はゲームの画面を撮り（**画面収録**）、マウスとキーボードを操作する（**アクセシビリティ**）。権限は yomawari ではなく、それを起動した **ターミナルアプリ**（iTerm、ターミナル、VS Code など）に付ける。

1. `yomawari doctor` を実行する。権限が足りなければ NG と出て、その下に「権限を付ける対象」のアプリが表示される。
2. `yomawari doctor --fix` を実行する。OS の確認ダイアログが出て、対象のアプリが設定の一覧に（オフの状態で）追加され、システム設定の該当画面が開く。
3. 開いた画面で、対象のアプリをオンにする。
   - **アクセシビリティ**: システム設定 → プライバシーとセキュリティ →（下にスクロール）アクセシビリティ
   - **画面収録**: システム設定 → プライバシーとセキュリティ →（下にスクロール）画面収録とシステムオーディオ録音（上の「画面収録とシステムオーディオ録音」の一覧。下の「システムオーディオ録音のみ」ではない）
   - 「＋」と「−」は、各項目を開いた先の一覧の下にある。プライバシーとセキュリティの最初の画面には出てこない。
4. 対象のアプリを **⌘Q で終了して起動し直す**。権限はアプリの起動時に読み込まれるので、ウィンドウやタブを閉じるだけでは反映されない。
5. もう一度 `yomawari doctor` を実行し、2つとも OK になったことを確かめる。

#### doctor で NG・注意が出たとき
| 表示 | 対処 |
|---|---|
| `Python ... (x86_64)` が NG | Rosetta の Python で動いている。`uv python list` で arm64 の Python があるか確かめ、`uv tool install --editable . --force` で入れ直す。ターミナル自体が Rosetta で起動していないか（Finder でアプリの「情報を見る」→「Rosetta を使用して開く」がオフか）も確かめる |
| 画面収録権限・アクセシビリティ権限が NG | 上の手順。オンにしたのに NG のままなら、ターミナルアプリを ⌘Q で再起動したか確かめる。それでも NG なら、一覧からアプリを「−」で消し、`doctor --fix` か「＋」で追加し直す（ターミナルアプリを更新したあとによく起きる） |
| 権限を付ける対象が想定と違う | tmux・screen・VS Code・Claude Code の中で実行していても、対象は外側のアプリ。表示されたアプリに付ける |
| 権限が「確認できませんでした」 | macOS 以外か、OS の API を読めなかった。macOS なら `uv tool install --editable . --force` で依存（pyobjc）を入れ直す |
| `rules.yaml を読めません` などの rules.yaml の NG | リポジトリの `rules.yaml` を `git diff rules.yaml` で確かめ、壊れていれば `git checkout rules.yaml` で戻す |
| `yomawari.toml がありません` | 5 の手順で作る（`yomawari run` の対話登録でもよい） |
| `パスが存在しません` | ビルドを移動・削除した。`yomawari builds edit <名前>` でパスを直す |
| `実行ファイルを解決できません` / `Mach-O ではありません` | `.app` が壊れているか、Mac 向けのビルドではない。Unity で Mac 向けにビルドし直す |
| `x86_64 のみのビルドです` が注意 | 動くが Rosetta 経由になる。Unity の Build Settings で Architecture を Apple silicon か Intel 64-bit + Apple silicon にしてビルドし直す |
| `Ollama に接続できません` が注意 | 7 の手順で入れて起動する（`brew services start ollama`）。入れない場合は `[judge] kind = "none"` にすれば出なくなる（ボタンの見た目からの推定はしない） |
| `モデル '...' が見つかりません` が注意 | `ollama pull qwen2.5vl:7b` を実行するか、`[judge] model` を `ollama list` に出る名前に合わせる |
| `未復元のセーブ退避があります` が注意 | 前回のランが途中で止まった。`yomawari restore` でセーブと PlayerPrefs を戻す |
| `caffeinate が見つかりません` が注意 | 通常の macOS には入っている。PATH に `/usr/bin` があるか確かめる |

### 7. Ollama（画像による判定）を入れる
ボタンの見た目からの役割の推定（DESIGN.md §9「画像による推定」）など、画像の判定の補助に、ローカルの [Ollama](https://ollama.com/) と画像対応モデルを使う。入れなくても動く（そのときは、ボタンの文字と名前だけで役割を推定する）。

```zsh
brew install ollama
brew services start ollama        # ログイン時に自動で起動する（夜間のランでも使えるように）
ollama pull qwen2.5vl:7b          # 約 6GB。メモリ 16GB 以上の Mac を想定
ollama list                       # qwen2.5vl:7b が出ればよい
```
`yomawari.toml` の `[judge]` で、`kind = "local"`、`model = "qwen2.5vl:7b"` にする（`yomawari.example.toml` の既定値）。`yomawari doctor` で `Ollama: qwen2.5vl:7b は pull 済み` と出ればよい。
- `brew services` を使わず、そのときだけ `ollama serve` で起動してもよい（ランのあいだは閉じない）
- 別のモデルを使うときは、`ollama pull <モデル名:タグ>` して `[judge] model` を書き換える。画像を読めるモデル（vision 対応）を選ぶ

### 8. ゲーム側の準備
ゲームの `Packages/manifest.json` にパッケージ `com.tunacook.yomawari` を足してビルドする。手順と Player Settings、Naninovel のゲームで自動で出るイベントは [docs/game-integration.md](docs/game-integration.md) を参照。`yomawari builds check mac_dev` で `boot を受信しました` と出ればよい。

## 環境構築（Windows）
> **Windows 対応はまだ実装していない（DESIGN.md フェーズ7）。** 以下は予定の手順で、作者は Windows の実機で確かめていない。今の時点では、`doctor` / `run` / `inspect` は OS の処理（`yomawari/platform/win.py`）が未実装のため「Windows 対応は未実装です」で止まる。テストも、モックゲームを `.app` に包んで macOS の処理で動かすものが多く、Windows では失敗する。

### 1. uv を入れる
PowerShell で次のどちらかを実行する。インストール先は `%USERPROFILE%\.local\bin` で、インストーラが PATH に足す。
```powershell
winget install --id=astral-sh.uv -e
# または
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
入れたあとは、PowerShell を開き直してから `uv --version` で確かめる。

### 2. リポジトリの準備とテスト
```powershell
git clone git@github.com:tunacook/yomawari.git; cd yomawari
uv sync
uv run python -c "import platform; print(platform.machine())"   # 通常の PC なら AMD64
uv run pytest -q
```
- 今は macOS 前提のテスト（`tests/conftest.py` の `.app` を使うもの）が失敗する。Windows 対応のときに、OS ごとにモックゲームの包み方を分ける。
- `doctor` の Python の確認は今は arm64 だけを OK にしているので、Windows 対応のときに AMD64 も OK にする。

### 3. コマンドとして入れる・ビルドの登録
```powershell
uv tool install --editable .
Copy-Item yomawari.example.toml yomawari.toml
yomawari builds add win_dev C:\Builds\Rachel\Rachel.exe
```
ビルドは、`.exe` と同じフォルダの `<名前>_Data`、`UnityPlayer.dll` などをまとめて置いたまま登録する（`.exe` だけを別の場所にコピーしない）。

### 4. 権限と実行環境
macOS の「画面収録」「アクセシビリティ」に当たる許可は Windows にはないが、次の点に注意する。
- **管理者権限をそろえる**: ゲームを管理者として起動し、yomawari を通常の権限で起動すると、Windows がマウスとキーボードの入力を止める（UIPI）。両方とも通常の権限で動かす。
- **画面の拡大率**: 「設定 → システム → ディスプレイ → 拡大縮小」が 100% 以外だと、座標の計算がずれやすい。Windows 対応では、ハーネスを Per-Monitor DPI Aware にして、macOS の Retina と同じく倍率を求める予定。確かめるまでは 100% にしておくのが無難。
- **画面をロックしない・リモートデスクトップを切らない**: ロック中や、リモートデスクトップを最小化・切断した状態では、入力が届かず、スクリーンショットも真っ黒になる。リモートで見る場合は、セッションを切らずに残すか、別の方法（VNC など）を使う。
- **スリープと更新を止める**: 「電源とバッテリー」でスリープと画面オフを「なし」にする。Windows Update のアクティブ時間に夜間を含めるか、更新を一時停止する。
- **通知を止める**: 「応答不可」（集中モード）をオンにして、通知がゲームの前に出ないようにする。

### 5. ゲーム側の準備
macOS と同じく、ゲームの `Packages/manifest.json` にパッケージ `com.tunacook.yomawari` を足し、Windows（x86_64）向けにビルドする。手順は [docs/game-integration.md](docs/game-integration.md) を参照。セーブ・PlayerPrefs・クラッシュダンプの場所は [DESIGN.md §7](DESIGN.md#7-ラン・ライフサイクルとウォッチドッグ) に書いてある。

## 使い方

### ランを回す
```zsh
yomawari run --hours 0.5                    # 既定の目的（play: 通常のプレイ）で 30 分
yomawari run --purpose buttons --hours 1    # 目的を選ぶ
yomawari run -p gacha --seed 42             # 乱数シードを固定して再現する
```
終わると `runs/<日時>_<ビルド>/report.md` が開く。ラン中は Mac に触らない（ハーネスが OS のマウス・キーボードで操作するため）。

| オプション | 内容 | 既定 |
|---|---|---|
| `--purpose` / `-p` | ランの目的（下の表）。`yomawari.toml` で足した目的も選べる | `yomawari.toml` の `run.purpose`（`play`） |
| `--hours` | 実行時間（時間） | `run.hours`（8） |
| `--mode` | 目的の操作のモードを上書きする。`random`（通常の操作だけ）/ `fuzz`（通常の操作＋イレギュラー操作）/ `aging`（操作しない） | 目的の mode |
| `--seed` | 乱数シード。`auto` は毎回変える | `run.seed`（`auto`） |
| `--build` / `-b` | 登録済みビルドの名前 | `default_build` |
| `--build-path` | その場限りで使うビルドのパス（登録しない） | |
| `--no-open` | 終わったあとにレポートを開かない | |

### ランの目的
デバッグで見たいものに合わせて、操作の選び方とレポートで先に見せるルールを変える。レポートの「サマリ」に目的が載り、重点的に見るルールの指摘は深刻度ごとに先に並び、「（重点）」が付く。

| 目的 | 見たいもの | 動き方 | モード | 重点的に見るルール |
|---|---|---|---|---|
| `play`（既定） | 通常のプレイ | 人が遊ぶように本編を先へ進め、寄り道は少なめ。進め方の経験を使う | random | |
| `stuck` | 進行不能 | 本編を進めつつ、ロード・回想などの寄り道と行き来もする。進めなくなったら、抜け出す操作を試さずにそのまま記録して再起動する | random | S-02, S-03, S-01, H-01 |
| `buttons` | ボタンの不具合 | 画面のボタンを網羅的に押す（暗いボタンも押す）。進め方の経験は使わない | random | S-05, V-07, S-01 |
| `gacha` | 落ちる・固まる操作 | 連打・タイミングをずらした入力・イレギュラーな操作を多めにする | fuzz | C-01〜04, H-01, L-01 |
| `rta` | 最短で進めたときの不具合 | 進行につながった操作を強く優先し、寄り道と巻き戻しを避ける | random | S-02, S-03 |
| `aging` | 放置したときの不具合 | 操作せずに起動したままにする | aging | H-01, L-01, C-01 |

ルールの ID は `rules.yaml` を参照。目的は `yomawari.toml` の `[purpose.<名前>]` で上書きしたり、新しく足したりできる（書き方は `yomawari.example.toml` のコメント）。`[purpose.<名前>.fuzz]` は `[fuzz]` の設定の上書きで、どの項目があるかも `yomawari.example.toml` に書いてある。目的ごとの動き方の考え方は [DESIGN.md §9「ランの目的」](DESIGN.md) を参照。

### そのほかのコマンド
`yomawari --help` で一覧が出る。主なもの:

| コマンド | 内容 |
|---|---|
| `yomawari doctor` | 権限・設定・Ollama などの準備ができているか確かめる |
| `yomawari builds` | 登録済みのビルドを一覧する（`builds add` で登録） |
| `yomawari restore` | ランが途中で止まったときに、退避したセーブ・設定を戻す |

## 補足
- Ollama（画像の判定）は入れなくても動く（環境構築の 7）。
- 夜間に回すときは、画面の自動ロックとスクリーンセーバーを止めておく。ほかの注意点は [DESIGN.md §13](DESIGN.md#13-macos実行時の注意) を参照。
