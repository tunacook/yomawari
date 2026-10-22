using System;
using System.Globalization;
using System.Text;

namespace Yomawari
{
    /**
     * <summary>
     * Yomawari（夜間自動プレイのハーネス）向けの [YOMAWARI] 行を組み立てる。
     * Unity に依存しないので EditMode テストで確かめる。行の形式は yomawari リポジトリの DESIGN.md §3
     * </summary>
     */
    public static class YomawariFormat
    {
        public const string Prefix = "[YOMAWARI] ";
        public const string EnableArg = "-yomawari";

        public static bool IsEnabled(string[] args)
        {
            return args != null && Array.IndexOf(args, EnableArg) >= 0;
        }

        // 1イベント＝1行。body は "key":value をカンマでつないだもの（空でもよい）
        public static string Line(int seq, double t, int frame, string ev, string body)
        {
            var sb = new StringBuilder(Prefix.Length + 64 + (body?.Length ?? 0));
            sb.Append(Prefix)
                .Append("{\"seq\":").Append(seq.ToString(CultureInfo.InvariantCulture))
                .Append(",\"t\":").Append(t.ToString("F3", CultureInfo.InvariantCulture))
                .Append(",\"f\":").Append(frame.ToString(CultureInfo.InvariantCulture))
                .Append(",\"ev\":").Append(Str(ev));
            if (!string.IsNullOrEmpty(body)) sb.Append(',').Append(body);
            return sb.Append('}').ToString();
        }

        // JSON の文字列リテラルにする（改行・引用符・制御文字をエスケープ）
        public static string Str(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder(s.Length + 2).Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }

        // 数値はロケールに関係なく小数点を "." にする
        public static string Num(double v)
        {
            return v.ToString("0.###", CultureInfo.InvariantCulture);
        }

        // スクリーン座標（左下原点、ピクセル）の矩形を、左上原点の 0〜1 の [x0,y0,x1,y1] にする。DESIGN.md §3
        public static string Rect(double xMin, double yMin, double xMax, double yMax, double width, double height)
        {
            if (width <= 0 || height <= 0) return "null";
            return "[" + Coord(xMin / width) + "," + Coord(1 - yMax / height) + "," +
                   Coord(xMax / width) + "," + Coord(1 - yMin / height) + "]";
        }

        static string Coord(double v)
        {
            return v.ToString("0.####", CultureInfo.InvariantCulture);
        }
    }
}
