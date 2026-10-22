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
「システム設定 → プライバシーとセキュリティ」で、yomawari を実行するターミナルアプリに **画面収録** と **アクセシビリティ** の権限を付ける。`yomawari doctor` を実行すると、権限が足りないときに付ける対象のアプリと手順を表示する。`yomawari doctor --fix` で、OS の確認ダイアログとシステム設定の該当画面を開ける。付けたらそのアプリを ⌘Q で終了して起動し直し、もう一度 `yomawari doctor` で、すべて OK になることを確かめる。

### 7. ゲーム側の準備
ゲームの `Packages/manifest.json` にパッケージ `com.tunacook.yomawari` を足してビルドする。手順と Player Settings、Naninovel のゲームで自動で出るイベントは [docs/game-integration.md](docs/game-integration.md) を参照。`yomawari builds check mac_dev` で `boot を受信しました` と出ればよい。

### 補足
- Ollama（画像判定）はフェーズ5から使う。今は入れなくても動く。
- 夜間に回すときは、画面の自動ロックとスクリーンセーバーを止めておく。ほかの注意点は [DESIGN.md §13](DESIGN.md#13-macos実行時の注意) を参照。
