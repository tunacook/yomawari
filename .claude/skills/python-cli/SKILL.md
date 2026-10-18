---
name: python-cli
description: uv で管理する Python 製 CLI ツール・スクリプトを新規作成または拡張するときの手順と規約。pyproject.toml の作成、typer/rich による CLI、console_scripts エントリ、`uv tool install --editable .`、ログ出力、設定ファイルの読み書き、サブプロセス管理とシグナル、終了コード、非対話環境（launchd/cron）での振る舞い、pytest によるテストを扱う。Python でコマンドやハーネス・自動化スクリプトを実装・追加・修正するときに使う。
---

# Python CLI 実装の規約（uv + typer）

## 0. 着手前
- プロジェクトの CLAUDE.md や設計書を先に読み、そちらの指示を優先する。
- 実装計画とファイル構成を先にユーザーに示してから書き始める。
- 環境確認: `uv --version`、`uname -m`。Apple Silicon では **arm64 ネイティブの Python** を使う。
  - `/usr/local/bin/python3` は Intel 版 Homebrew 由来の x86_64 のことがある。`file $(which python3)` で確かめる。
  - uv がない場合: `brew install uv`（`/opt/homebrew` の arm64 brew）。Xcode のライセンス未同意などで brew が使えなければ、公式インストーラ `curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh` を使う（`~/.local/bin` に入る）。

## 1. 進め方（PR の分け方）
新しいコマンドは、次の順に PR を分けて出す（PR の作り方は create-pr skill）。
1. **スケルトン**: pyproject.toml、エントリポイント、設計にある全サブコマンドと引数。中身は「受け取った引数をログに出して終わる」だけにする。テストは全コマンドが起動して終了コード0を返すこと。
2. **中身**: フェーズごと、または機能ごとに実装する。まだ実装しないコマンドは、スケルトンのログ出力のまま残す。

## 2. プロジェクト骨格
```toml
[project]
name = "<pkg>"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["typer>=0.12", "rich>=13"]

[project.scripts]
<cmd> = "<pkg>.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["<pkg>"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.uv]
# x86_64 のシステム Python を拾わないよう、uv 管理の Python に限る
python-preference = "only-managed"
```
- `.python-version` を置き、`uv.lock` はコミットする。
- 依存は **今の PR で実際に import するものだけ** 入れる。将来使うものは、使うときに `uv add` する。
- `uv sync` は **パッケージのディレクトリと `__init__.py` を作ってから** 実行する。先に実行すると editable の `.pth` が作られず `ModuleNotFoundError` になる。そうなった場合は `uv sync --reinstall-package <pkg>`。
- 確認は `uv run <cmd> --help` で行う。開発用インストールは `uv tool install --editable .`（PATH に `~/.local/bin` が要る）。

## 3. CLI（typer）
```python
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)

def main() -> None:
    app(prog_name="<cmd>")   # エイリアスから起動してもヘルプ表示を揃える
```
- サブコマンドの群は `sub = typer.Typer(no_args_is_help=True); app.add_typer(sub, name="builds")`。
- 終了コードは設計書で決め、`raise typer.Exit(code)` で返す。想定外の例外は `main()` で捕まえ、「ツール自体のエラー」のコード（例: 2）で終わる。traceback は stderr かログに残す。
- help 文字列の `[...]` は rich のマークアップとして消える（`[run].hours` が `.hours` と表示される）。角括弧は使わないか、エスケープする。
- **非対話環境**: 入力を求める前に `sys.stdin.isatty()` を確認する。端末でなければ待たずに、理由を出して決めた終了コードで終わる（launchd や cron で止まったままにならないように）。
- 対話入力は `rich.prompt.Prompt` / `Confirm`。パス入力は前後の引用符を除き、`\ ` をアンエスケープし、`~` を展開する（Finder からのドラッグ&ドロップに対応するため）。

## 4. ログ
- 標準の `logging` を使い、ハンドラは `rich.logging.RichHandler` にする。設定はエントリポイントのコールバックで1回だけ行う。
- ルートのオプションに `--verbose/-v`（DEBUG）を用意する。ライブラリ側のモジュールは `log = logging.getLogger(__name__)` を使い、`print` しない。
- ユーザーへの結果表示（表・レポートのパス）は `Console` で出し、経過や診断はログで出す。

## 5. 設定
- 読むだけなら `tomllib`。**書き戻す場合は `tomlkit`** を使い、ユーザーのコメントや並び順を保つ。
- 設定ファイルの場所は `--config` > 環境変数 > カレントディレクトリ、の順で解決する。相対パスは cwd ではなく、設定ファイルのあるディレクトリを基準にする。
- 閾値・猶予時間・除外リストはコードに直書きせず、設定ファイルかルールファイルに置く。不足しているキーは起動時にエラーにする。
- ローカルのパスや秘密を含む設定はコミットしない。代わりに `*.example.toml` をコミットする。

## 6. サブプロセス・非同期・シグナル
- 長時間の監視は `asyncio` で行う。ブロッキングな処理は `asyncio.to_thread` に逃がす。
- 子プロセスは `subprocess.Popen` で起動して PID を持つ（macOS の `.app` は `open` を使わず、`Contents/MacOS/<CFBundleExecutable>` を直接起動する）。
- `start_new_session=True` で起動すると、Ctrl+C が子に直接届かない。その代わり **親が死ぬと子が孤児として残る**。そのため SIGINT と SIGTERM の両方で終了処理が走るようにする: `loop.add_signal_handler(signal.SIGTERM, main_task.cancel)` を登録し、`CancelledError` を捕まえて `finally` で後始末する。
- 子の終了は terminate → タイムアウト → kill の順。「自分で終了させた」ことをフラグで持ち、異常終了と区別する。
- 時刻は、監視には `time.monotonic()`、記録には wall clock（ISO 8601）を使う。テストで clock を注入できる形にする。

## 7. ファイル・OS の安全
- ユーザーのデータを消す・上書きする処理では、`rmtree` を使わずに退避先へ `move` し、復元できる状態を保つ。
- 処理中であることを示すマーカーファイルを置き、異常終了後の次回起動時に未完了を検出して復元できるようにする。
- macOS の `defaults export <domain>` は、**存在しないドメインでも終了コード0で空の plist を書く**。存在するかどうかは中身で判定する。`defaults delete` の後には、cfprefsd が空の plist ファイルを残すことがある。
- OS 固有の処理は `platform/` に閉じ込め、上位層に `sys.platform` の分岐を書かない。未対応の OS では `NotImplementedError` を投げて明示する。
- macOS の権限確認（画面収録・アクセシビリティ）は、ctypes で `CGPreflightScreenCaptureAccess` / `AXIsProcessTrusted` を呼ぶ。権限は実行元のターミナルアプリに付く。

## 8. テスト
- `uv run pytest -q` で実行する。外部プログラム（ゲームや GUI アプリ等）が要る部分は、同じ入出力をする小さなモック（Python スクリプト）を `tests/` に置いて差し替える。
- 単体テスト（パース・正規化・判定）と、モックを起動する短い E2E テストの両方を書く。E2E の待ち時間を縮めてもよいが、判定条件そのものは緩めない。
- CLI は `typer.testing.CliRunner` で確かめる。シグナルの扱いは、実際のコマンドを `subprocess.Popen` で起動して `send_signal` で確かめる。シェルの `&` で起動したバックグラウンドプロセスは SIGINT を無視するうえ、`pkill -f` は自分のシェルにもマッチするので、シェルからの試験は当てにならない。
- 非同期のテストは `asyncio.run()` で包めば pytest-asyncio は要らない。
- テストの後に、子プロセスや一時ファイル（`~/Library/Preferences` など）が残っていないことを確かめる。
- 完了を報告する前に、テスト結果と `<cmd> --help` を確かめる。失敗やスキップがあれば、そのまま報告する。
