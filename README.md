# yomawari
Unity製ノベルゲームを自動でプレイしてくれる仕組み。

Unity のビルド（`.app` / `.exe`）を夜間に自動でプレイさせ、クラッシュ・ハング・進行不能・表示異常を Markdown のレポートにまとめる。設計は [DESIGN.md](DESIGN.md) を参照。

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
| `Ollama に接続できません` が注意 | フェーズ5まで使わないので、今は無視してよい |
| `未復元のセーブ退避があります` が注意 | 前回のランが途中で止まった。`yomawari restore` でセーブと PlayerPrefs を戻す |
| `caffeinate が見つかりません` が注意 | 通常の macOS には入っている。PATH に `/usr/bin` があるか確かめる |

### 7. ゲーム側の準備
ゲームの `Packages/manifest.json` にパッケージ `com.tunacook.yomawari` を足してビルドする。手順と Player Settings、Naninovel のゲームで自動で出るイベントは [docs/game-integration.md](docs/game-integration.md) を参照。`yomawari builds check mac_dev` で `boot を受信しました` と出ればよい。

### 補足
- Ollama（画像判定）はフェーズ5から使う。今は入れなくても動く。
- 夜間に回すときは、画面の自動ロックとスクリーンセーバーを止めておく。ほかの注意点は [DESIGN.md §13](DESIGN.md#13-macos実行時の注意) を参照。
