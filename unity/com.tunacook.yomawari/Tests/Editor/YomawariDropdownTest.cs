using NUnit.Framework;
using UnityEngine;

namespace Yomawari.Tests
{
    public class YomawariDropdownTest
    {
        // 標準の Dropdown と同じ名前・同じフィールドを持つ部品（UI のアセンブリを参照せずに確かめる）
        class Dropdown : MonoBehaviour
        {
            GameObject m_Dropdown;
            public void Open(GameObject list) => m_Dropdown = list;
        }

        class CustomDropdown : Dropdown { }

        class Other : MonoBehaviour
        {
            GameObject m_Dropdown;
            public void Open(GameObject list) => m_Dropdown = list;
        }

        GameObject root;

        [SetUp]
        public void SetUp() => root = new GameObject("root");

        [TearDown]
        public void TearDown() => Object.DestroyImmediate(root);

        [Test]
        public void 開いているドロップダウンのリストを返す ()
        {
            var list = new GameObject("Dropdown List");
            list.transform.SetParent(root.transform);
            var d = root.AddComponent<CustomDropdown>();
            Assert.IsNull(YomawariDropdown.OpenList(d));
            d.Open(list);
            Assert.AreSame(list, YomawariDropdown.OpenList(d));
            list.SetActive(false);
            Assert.IsNull(YomawariDropdown.OpenList(d));
        }

        [Test]
        public void ドロップダウンでない部品はnull ()
        {
            var list = new GameObject("Dropdown List");
            list.transform.SetParent(root.transform);
            var o = root.AddComponent<Other>();
            o.Open(list);
            Assert.IsNull(YomawariDropdown.OpenList(o));
            Assert.IsNull(YomawariDropdown.OpenList(null));
        }
    }
}
