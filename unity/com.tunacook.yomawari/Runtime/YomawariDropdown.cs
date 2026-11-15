using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;

namespace Yomawari
{
    /**
     * <summary>
     * 開いているドロップダウンのリストを探す。標準の Dropdown と TMP_Dropdown は、開いているあいだのリスト（"Dropdown List"）を
     * 非公開のフィールド m_Dropdown に持つ（閉じると null）。どちらも同じ名前なので、型の名前とこのフィールドで見る
     * （Runtime のアセンブリは UI と TextMeshPro を参照しない。それらを継承した部品も、親の型をたどって見つける）
     * </summary>
     */
    public static class YomawariDropdown
    {
        static readonly Dictionary<Type, FieldInfo> fields = new();

        // c が開いているドロップダウンなら、そのリストの GameObject。ドロップダウンでない・閉じているなら null
        public static GameObject OpenList(Component c)
        {
            if (c == null) return null;
            var field = FieldOf(c.GetType());
            if (field == null) return null;
            var list = field.GetValue(c) as GameObject;
            return list != null && list.activeInHierarchy ? list : null;
        }

        static FieldInfo FieldOf(Type type)
        {
            if (fields.TryGetValue(type, out var f)) return f;
            for (var t = type; t != null && f == null; t = t.BaseType)
            {
                if (t.Name != "Dropdown" && t.Name != "TMP_Dropdown") continue;
                f = t.GetField("m_Dropdown", BindingFlags.NonPublic | BindingFlags.Instance);
                if (f != null && f.FieldType != typeof(GameObject)) f = null;
            }
            fields[type] = f;
            return f;
        }
    }
}
