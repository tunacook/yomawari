using UnityEngine;

namespace Yomawari
{
    /**
     * <summary>
     * UI の矩形を、ハーネスが使う画面基準の正規化座標（左上原点、0〜1）にする。DESIGN.md §3
     * </summary>
     */
    public static class YomawariRect
    {
        static readonly Vector3[] corners = new Vector3[4];

        // スクリーン座標（左下原点、ピクセル）での外接矩形。Canvas の外にあれば false
        public static bool TryGetScreen(RectTransform rt, out Vector2 min, out Vector2 max)
        {
            min = max = default;
            var canvas = rt.GetComponentInParent<Canvas>();
            if (canvas == null) return false;
            canvas = canvas.rootCanvas;
            var cam = canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera;

            rt.GetWorldCorners(corners);
            min = new Vector2(float.MaxValue, float.MaxValue);
            max = new Vector2(float.MinValue, float.MinValue);
            foreach (var c in corners)
            {
                var p = RectTransformUtility.WorldToScreenPoint(cam, c);
                min = Vector2.Min(min, p);
                max = Vector2.Max(max, p);
            }
            return max.x > min.x && max.y > min.y;
        }

        // [x0,y0,x1,y1] の JSON。取れなければ null
        public static string Json(RectTransform rt)
        {
            return TryGetScreen(rt, out var min, out var max)
                ? YomawariFormat.Rect(min.x, min.y, max.x, max.y, Screen.width, Screen.height)
                : "null";
        }
    }
}
