# ゲームのプロファイルの記述例

`yomawari.profile.toml` は、ゲームのリポジトリに置いてチームで共有する「テストのやり方」の設定（DESIGN.md §9「ゲーム固有の仕様」③）。ここには、エンジンごとの記述例を置く。

| ファイル | エンジン | 状態 |
|---|---|---|
| [unity-naninovel.profile.toml](unity-naninovel.profile.toml) | Unity + Naninovel（選択肢と、推理パートで証拠を選ぶ画面がある、一般的なノベルゲームの例） | 文字と名前による役割の推定と、押した結果での確定（②）は実装済み。見た目（画像）による推定は未実装 |
| [tyrano.profile.toml](tyrano.profile.toml) | ティラノスクリプト | 未検証（ゲーム側のプラグインもハーネス側の対応もない） |
| [renpy.profile.toml](renpy.profile.toml) | Ren'Py | 未検証（同上） |

## 書き方の考え方
ゲームのコードには、Yomawari の概念（インターフェース、コンポーネント、専用の属性など）を入れない（DESIGN.md §1）。プロファイルにも、ゲームの中の名前（UI・ボタン・ラベルの名前）をなるべく書かない。

- ボタンの種類・スクロール・隠れているか、エンジンの標準部品の役割は、エンジンごとのパッケージ・プラグインが自動で読み取る（①）
- 自作のボタンの役割（終了・ブラウザを開く・タイトルへ・画面の設定）は、ハーネスがボタンの文字・名前・見た目から推定し、押した結果で確定する（②）。押しても何も起きないボタンは、押した結果から後回しにする
- プロファイルに書くのは、テストの方針だけ。役割ごとの押し方を既定値から変えるもの（`[guard.roles]`）、押してはいけないボタン（`[guard.buttons]` の `deny`）、画面ごとの重み（`[screens.<menu>]`）、既知で無害なエラー（`[rules.<ID>]`）
- 画面を動かす手段（ドラッグ・方向キーなど）は書かない。モーダルな画面では、ハーネスがいつも試す
- 遊び方のサイクル（どの画面で何をすると次に進むか）は `[[flow]]` に書く（DESIGN.md §9「ゲームの流れ」）。質問の正解を書くなら `[[flow.answers]]` に、質問の本文の行（`at`）と、選ぶ手順ごとに選ぶものの画面に見える文字（`pick`）を書く（DESIGN.md §9「答えの選び方」。書き方の例は [unity-naninovel.profile.toml](unity-naninovel.profile.toml) とリポジトリの README「質問の正解をプロファイルに書く」）。正解を書いても、ランの目的によっては使わず、使うときもときどきわざと外れを答える

どのエンジンでも、プロファイルの書き方は同じになる。エンジンごとに違うのは、① で何を読み取れるかだけ。

| ① で読み取れること | Unity + Naninovel | ティラノスクリプト | Ren'Py |
|---|---|---|---|
| 押すと終了する（`exit`） | Naninovel の `TitleExitButton` | ボタンの飛び先のラベルが `[close]` を実行する | `action` が `Quit(confirm=False)`（確認画面の「はい」も含む） |
| ブラウザを開く（`external`） | —（② で覚える） | 飛び先が `[web]` を実行する | `action` が `OpenURL` |
| タイトルへ戻る（`to_title`） | Naninovel の `ControlPanelTitleButton` | `[button role="title"]` | `action` が `MainMenu` |
| 画面・言語の設定（`system_setting`） | Naninovel の `GameSettings*Dropdown`（言語・解像度・画面モード・画質） | `[button role="fullscreen"]` | `action` が `Preference("display", …)`、`Language` |
| 押せない（`locked`） | Naninovel の選択肢の `Locked`、`interactable` | `disabled` | `sensitive`（`action` の `get_sensitive()`。ギャラリーのロック中のボタンも insensitive になる） |
| ほかの UI に覆われている（`covered`） | ボタンの中心に EventSystem のレイキャストを当て、先に当たるのがほかの UI か（UI ごとの Canvas の Order in Layer が反映される） | ボタンの中心で `document.elementFromPoint` を呼び、返る要素がそのボタン（か子）でないか（レイヤーの `z-index` が反映される） | ボタンより手前のレイヤー・`zorder` に、`modal True` の screen か、その位置を覆う displayable があるか |
| スクロールの先（`scroll`） | `ScrollRect` | `scrollHeight` > `clientHeight` の要素 | `viewport` / `vpgrid` の `yadjustment` |

① で読み取れないもの（表の「—」と、自作の部品すべて）は、ハーネスが推定し、押した結果で確定する。Ren'Py は、ボタンの動作が `action` のオブジェクトとして画面に残るので、3つの中ではいちばん多くを ① で読み取れる。

ティラノスクリプトと Ren'Py の欄は、各エンジンの仕組みから考えた方針で、確かめていない（`docs/game-integration.md` §6 と同じ扱い）。タグの属性や API の名前は、組み込むときに各エンジンの文書で確かめること。
