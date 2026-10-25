# ゲームのプロファイルの記述例

`yomawari.profile.toml` は、ゲームのリポジトリに置いてチームで共有する「テストのやり方」の設定（DESIGN.md §9「ゲーム固有の仕様」③）。ここには、エンジンごとの記述例を置く。

| ファイル | エンジン | 状態 |
|---|---|---|
| [unity-naninovel.profile.toml](unity-naninovel.profile.toml) | Unity + Naninovel（Rachel） | ① の Unity 版と ② は未実装。実装したあとの書き方 |
| [tyrano.profile.toml](tyrano.profile.toml) | ティラノスクリプト | 未検証（ゲーム側のプラグインもハーネス側の対応もない） |
| [renpy.profile.toml](renpy.profile.toml) | Ren'Py | 未検証（同上） |

## 書き方の考え方
プロファイルには、ゲームの中の名前（UI・ボタン・GameObject・ラベルの名前）をなるべく書かない。

- ボタンが押せるか（`locked`）、何をするボタンか（`role`）、スクロールの先に項目があるか（`scroll`）は、ゲーム側が `menu` イベントで伝える。エンジンの標準 UI から分かるものは自動で読み取り（①）、分からないものはゲームのコードやシナリオに書き足す（②）
- プロファイルに書くのは、テストの方針だけ。役割ごとの押し方を既定値から変えるもの（`[guard.roles]`）、画面ごとの重み（`[screens.<menu>]`）、既知で無害なエラー（`[rules.<ID>]`）
- 名前で書く項目（`[guard.buttons]`、`no_response_ok`、`scroll`）は、ゲーム側に手を入れられないときの最後の手段

どのエンジンでも、プロファイルの書き方は同じになる。エンジンごとに違うのは、`role` や `locked` をどこから読むか（① と ②）だけ。

| 伝えたいこと | Unity + Naninovel | ティラノスクリプト | Ren'Py |
|---|---|---|---|
| 押すと終了する（`exit`） | ① Naninovel の標準の終了ボタン ／ ② 自作のボタンに `YomawariButtonRole` を付ける | ① ボタンの飛び先のラベルが `[close]` を実行する ／ ② タグに `yomawari_role="exit"` | ① `action` が `Quit(confirm=False)`（確認画面の「はい」も含む） |
| ブラウザを開く（`external`） | ② `YomawariButtonRole` | ① 飛び先が `[web]` を実行する ／ ② `yomawari_role="external"` | ① `action` が `OpenURL` |
| タイトルへ戻る（`to_title`） | ① Naninovel の標準のボタン ／ ② `YomawariButtonRole` | ① `[button role="title"]` | ① `action` が `MainMenu` |
| 画面・言語の設定（`system_setting`） | ② `YomawariButtonRole`（`Dropdown` は何を変えるか分からない） | ① `[button role="fullscreen"]` ／ ② `yomawari_role="system_setting"` | ① `action` が `Preference("display", …)`、`Language` |
| 押せない（`locked`） | ① `interactable` ／ ② `IYomawariButtonState` | ② タグに `yomawari_locked="<式>"` | ① `sensitive`（`action` の `get_sensitive()`。ギャラリーのロック中のボタンも insensitive になる） |
| スクロールの先（`scroll`） | ① `ScrollRect` | ① `scrollHeight` > `clientHeight` の要素 | ① `viewport` / `vpgrid` の `yadjustment` |

Ren'Py は、ボタンの動作が `action` のオブジェクトとして画面に残るので、3つの中ではいちばん多くを ① で読み取れる。Unity は標準部品が「押すと何が起きるか」を持たないので、自作のボタンには ② が要る。

ティラノスクリプトと Ren'Py の欄は、各エンジンの仕組みから考えた方針で、確かめていない（`docs/game-integration.md` §6 と同じ扱い）。タグの属性や API の名前は、組み込むときに各エンジンの文書で確かめること。
