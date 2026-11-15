# 環境構築（Windows）

Windows の PC で yomawari を動かすまでの手順。macOS は [setup-macos.md](setup-macos.md)、使い方はリポジトリの [README](../README.md#使い方) を参照。

> Windows 対応はまだ実装していない（DESIGN.md フェーズ7）。以下は予定の手順で、作者は Windows の実機で確かめていない。今は OS の処理（`yomawari/platform/win.py`）が未実装なので、`doctor` / `run` / `inspect` は「Windows 対応は未実装です」で止まる。テストも、モックゲームを `.app` に包んで macOS の処理で動かすものが多く、Windows では失敗する。

## 1. uv を入れる
PowerShell で次のどちらかを実行する。インストール先は `%USERPROFILE%\.local\bin` で、インストーラが PATH に足す。
```powershell
winget install --id=astral-sh.uv -e
# または
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
入れたら PowerShell を開き直し、`uv --version` で確かめる。

## 2. リポジトリの準備とテスト
```powershell
git clone git@github.com:tunacook/yomawari.git; cd yomawari
uv sync
uv run python -c "import platform; print(platform.machine())"   # 通常の PC なら AMD64
uv run pytest -q
```
- 今は macOS 前提のテスト（`tests/conftest.py` の `.app` を使うもの）が失敗する。Windows 対応のときに、OS ごとにモックゲームの包み方を分ける。
- `doctor` の Python の確認は今は arm64 だけを OK にしているので、Windows 対応のときに AMD64 も OK にする。

## 3. コマンドとして入れる・ビルドの登録
```powershell
uv tool install --editable .
Copy-Item yomawari.example.toml yomawari.toml
yomawari builds add win_dev C:\Builds\MyGame\MyGame.exe
```
ビルドは `.exe` と同じフォルダの `<名前>_Data`、`UnityPlayer.dll` などをまとめて置いたまま登録する（`.exe` だけを別の場所にコピーしない）。

## 4. 権限と実行環境
macOS の「画面収録」「アクセシビリティ」に当たる許可は Windows にはないが、次の点に注意する。
- 管理者権限をそろえる。ゲームを管理者として起動し、yomawari を通常の権限で起動すると、Windows がマウスとキーボードの入力を止める（UIPI）。両方とも通常の権限で動かす。
- 画面の拡大率（「設定 → システム → ディスプレイ → 拡大縮小」）が 100% 以外だと、座標の計算がずれやすい。Windows 対応ではハーネスを Per-Monitor DPI Aware にして、macOS の Retina と同じく倍率を求める予定。確かめるまでは 100% にしておくのが無難。
- 画面をロックしない・リモートデスクトップを切らない。ロック中や、リモートデスクトップを最小化・切断した状態では、入力が届かず、スクリーンショットも真っ黒になる。リモートで見る場合は、セッションを切らずに残すか、別の方法（VNC など）を使う。
- スリープと更新を止める。「電源とバッテリー」でスリープと画面オフを「なし」にし、Windows Update のアクティブ時間に夜間を含めるか、更新を一時停止する。
- 「応答不可」（集中モード）をオンにして、通知がゲームの前に出ないようにする。

## 5. ゲーム側の準備
macOS と同じく、ゲームの `Packages/manifest.json` にパッケージ `com.tunacook.yomawari` を足し、Windows（x86_64）向けにビルドする。手順は [game-integration.md](game-integration.md)、セーブ・PlayerPrefs・クラッシュダンプの場所は [DESIGN.md §7](../DESIGN.md#7-ラン・ライフサイクルとウォッチドッグ) を参照。

## 6. Ollama（画像による判定）を入れる
入れなくても動く（[macOS の 7](setup-macos.md#7-ollama画像による判定を入れる) と同じ）。Ollama 自体は Windows 版があり、yomawari とは HTTP（`http://localhost:11434`）でやり取りするので、OS による違いはない見込み（作者は Windows で確かめていない）。
```powershell
winget install --id=Ollama.Ollama -e   # 入れるとログイン時に自動で起動する（タスクトレイに常駐）
ollama pull qwen2.5vl:7b               # 約 6GB
ollama list                            # qwen2.5vl:7b が出ればよい
```
`yomawari.toml` の `[judge]` は macOS と同じ（`kind = "local"`、`model = "qwen2.5vl:7b"`）。GPU のメモリが足りない PC では遅くなるので、そのときは `[judge] kind = "none"` にする。

## 7. ランを回す（予定）
Windows 対応のあとは macOS と同じコマンドを PowerShell で実行する予定（オプションは [README の「使い方」](../README.md#使い方) と同じ）。
```powershell
yomawari doctor                  # 準備ができているか確かめる
yomawari run --hours 0.5         # 既定の目的（play）で 30 分
yomawari run -p stuck --hours 8  # 夜間に 8 時間
```
- 終わると `runs\<日時>_<ビルド>\report.md` が `.md` に関連付けたアプリで開く（関連付けがなければ、開くアプリを選ぶ画面が出る。`--no-open` で開かない）
- ハーネスが OS のマウス・キーボードで操作し、画面を撮るので、ラン中はその PC に触らない。ゲームのウィンドウを最前面に置いたまま、ほかのウィンドウで隠さない
- macOS の `caffeinate`（ランのあいだスリープを止める）と `--display-off`（画面を消す）に当たる処理は、Windows 対応のときに決める。それまでは 4 の電源の設定でスリープを止めておく。画面を消すなら、「設定 → アカウント → サインイン オプション」で、スリープから戻ったときにサインインを求めないようにする（ロックされると入力が届かない）
- 決まった時刻に始める `yomawari schedule install` は、macOS（launchd）だけ実装した。Windows ではまだ使えないので、手で始める

### セーブを手で退避しておく
ハーネスはロード・セーブを試すため、ランのあいだにゲームのセーブを書き換える。macOS ではランの前にセーブと PlayerPrefs を退避し、終わったら戻す（途中で止まったら `yomawari restore`）。Windows ではこの退避と復元もまだ実装していないので、大事なセーブがある PC では、ランの前に手で控えておく。`<Company>` と `<Product>` は、Unity の Player Settings の Company Name と Product Name。
```powershell
$save = "$env:USERPROFILE\AppData\LocalLow\<Company>\<Product>"
$prefs = "HKCU\Software\<Company>\<Product>"
New-Item -ItemType Directory -Force "$HOME\yomawari-backup" | Out-Null
Copy-Item -Recurse $save "$HOME\yomawari-backup\save"            # セーブ
reg export $prefs "$HOME\yomawari-backup\prefs.reg" /y           # PlayerPrefs（レジストリ）
```
戻すときは、ランのあいだに増えたセーブや設定が残らないよう、ゲームを終了してから、消して戻す。
```powershell
Remove-Item -Recurse -Force $save
Copy-Item -Recurse "$HOME\yomawari-backup\save" $save
reg delete $prefs /f
reg import "$HOME\yomawari-backup\prefs.reg"
```
クラッシュしたときのダンプは `%TEMP%\<Company>\<Product>\Crashes\` に出る。
