namespace Yomawari
{
    /**
     * <summary>
     * ボタンの、外からは分からない状態をハーネスに伝える（DESIGN.md §9「ゲーム固有の仕様」②）。
     * Selectable と同じ GameObject のコンポーネントに実装する。
     * </summary>
     */
    public interface IYomawariButtonState
    {
        // true のあいだ、ボタンは押せない（押しても何も起きないのが仕様）。menu の locked になる
        bool YomawariLocked { get; }
    }
}
