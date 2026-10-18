# yomawari
Unity製ノベルゲームを自動でプレイしてくれる仕組み。

Unity のビルド（`.app` / `.exe`）を夜間に自動でプレイさせ、クラッシュ・ハング・進行不能・表示異常を Markdown のレポートにまとめる。設計は [DESIGN.md](DESIGN.md) を参照。

## 環境構築（macOS / Apple Silicon）

### 1. uv を入れる
```zsh
# arm64 の Homebrew（/opt/homebrew）を使う
/opt/homebrew/bin/brew install uv
```
- 「Xcode のライセンスに同意していない」というエラーで止まる場合は、先に `sudo xcodebuild -license accept` を実行する。
- brew を使わない場合は、公式インストーラ `curl -LsSf https://astral.sh/uv/install.sh | sh` でも入る（インストール先は `~/.local/bin`）。

### 2. PATH を通す
`uv tool` で入れたコマンドは `~/.local/bin` に置かれる。`~/.zshrc` に次の行がなければ足し、ターミナルを開き直す。
```zsh
export PATH="$HOME/.local/bin:$PATH"
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
「システム設定 → プライバシーとセキュリティ」で、yomawari を実行するターミナルアプリに **画面収録** と **アクセシビリティ** の権限を付ける。そのあと `yomawari doctor` で、すべて OK になることを確かめる。

### 7. ゲーム側の準備
Unity のビルドに `YomawariLog`（[DESIGN.md §3](DESIGN.md#3-ゲーム側-yomawarilogc)）を組み込む。起動引数 `-yomawari` を付けたときに Player.log へ `[YOMAWARI]` 行が出ればよい。`yomawari builds check mac_dev` で確かめられる。

### 補足
- Ollama（画像判定）はフェーズ5から使う。今は入れなくても動く。
- 夜間に回すときは、画面の自動ロックとスクリーンセーバーを止めておく。ほかの注意点は [DESIGN.md §13](DESIGN.md#13-macos実行時の注意) を参照。
