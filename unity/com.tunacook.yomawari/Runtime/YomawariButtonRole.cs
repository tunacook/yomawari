using UnityEngine;

namespace Yomawari
{
    public enum YomawariRole
    {
        None,
        Exit,           // 押すとゲームが終了する（確認ダイアログがあれば「はい」の側）
        External,       // ブラウザなど、ゲームの外のアプリを開く
        ToTitle,        // タイトルへ戻る
        SystemSetting,  // 画面モード・解像度・言語・画質など、ゲームの外側の設定を変える
        NewGame,        // 最初から始める
        Deny,           // 課金、データの送信など、マシンの外に影響が出る
    }

    /**
     * <summary>
     * ボタンの役割をハーネスに伝える（DESIGN.md §9 ガード）。menu の role になる。
     * Selectable と同じ GameObject に付け、Inspector で役割を選ぶ。
     * </summary>
     */
    [DisallowMultipleComponent]
    public sealed class YomawariButtonRole : MonoBehaviour
    {
        public YomawariRole role;

        // menu の role の値。None なら null
        public static string Name(YomawariRole role)
        {
            switch (role)
            {
                case YomawariRole.Exit: return "exit";
                case YomawariRole.External: return "external";
                case YomawariRole.ToTitle: return "to_title";
                case YomawariRole.SystemSetting: return "system_setting";
                case YomawariRole.NewGame: return "new_game";
                case YomawariRole.Deny: return "deny";
                default: return null;
            }
        }
    }
}
