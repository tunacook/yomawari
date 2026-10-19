# Yomawari

Unity製ノベルゲームのビルド（.app / .exe）を夜間に自動プレイし、クラッシュ・ハング・進行不能・表示異常をMarkdownレポートにまとめるハーネス。設計の詳細は `DESIGN.md` を参照すること。実装を始める前に必ず読む。

## 原則
- ゲームとの通信は一方向ずつ: ゲーム→ハーネスは Player.log の `[YOMAWARI]` 行、ハーネス→ゲームは OS のマウス・キーボード入力。ゲーム内に操作受付の口は作らない
- 判定と制御はコード（`rules.yaml` によるルールベース）。AI（Ollama）は操作選択と画像判定の補助のみ。Critical は AI の判定から出さない
- 閾値・除外・猶予期間は `rules.yaml` / `yomawari.toml` に置き、コードに直書きしない
- OS差分は `yomawari/platform/` と `yomawari/input/` に閉じ込め、上位層はOSを意識しない
- 座標は常にクライアント領域に対する0〜1の正規化座標で扱う
- ゲーム側は `unity/com.tunacook.yomawari/`（UPM パッケージ）。`[YOMAWARI]` 行の形式を変えるときは、パッケージ・ハーネス・`DESIGN.md` §3 を同じ PR で変える。パッケージの `.meta` は Unity プロジェクトから `file:` パスで参照して Editor に生成させ、コミットする

## 環境
- Python 3.11+、arm64 ネイティブ、uv で管理
- 開発用インストール: `uv tool install --editable .`
- コマンド: `yomawari`（エイリアス `ymw`）
- 主な実行環境は M3 Mac。Windows 対応は後日

## 作業の進め方
- `DESIGN.md` の「実装フェーズ」の順に進める。現在のフェーズ: 1（骨組み）
- 各フェーズで、実ゲームなしで動くテスト（Player.log サンプルを流し込むモック）を書く
- `yomawari.toml` と `runs/` はコミットしない。`yomawari.example.toml` と `rules.yaml` はコミットする
- テストを通すために `rules.yaml` の判定条件を緩めない。条件変更が必要なら理由を示して確認を求める
