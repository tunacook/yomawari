using System.Globalization;
using System.Threading;
using NUnit.Framework;

namespace Yomawari.Tests
{
    public class YomawariFormatTest
    {
        [Test]
        public void 起動引数にyomawariがあるときだけ有効 ()
        {
            Assert.IsTrue(YomawariFormat.IsEnabled(new[] { "Rachel", "-yomawari", "-logFile", "p.log" }));
            Assert.IsFalse(YomawariFormat.IsEnabled(new[] { "Rachel", "-logFile", "p.log" }));
            Assert.IsFalse(YomawariFormat.IsEnabled(new[] { "Rachel", "-yomawari2" }));
            Assert.IsFalse(YomawariFormat.IsEnabled(null));
        }

        [Test]
        public void 文字列をJSONのリテラルにする ()
        {
            Assert.AreEqual("\"レイチェル\"", YomawariFormat.Str("レイチェル"));
            Assert.AreEqual("\"a\\\"b\\\\c\"", YomawariFormat.Str("a\"b\\c"));
            Assert.AreEqual("\"1\\n2\\r3\\t4\"", YomawariFormat.Str("1\n2\r3\t4"));
            Assert.AreEqual("\"\\u0001\"", YomawariFormat.Str("\u0001"));
            Assert.AreEqual("null", YomawariFormat.Str(null));
        }

        [Test]
        public void 一行のイベントを組み立てる ()
        {
            Assert.AreEqual(
                "[YOMAWARI] {\"seq\":3,\"t\":12.346,\"f\":700,\"ev\":\"heartbeat\",\"fps\":59.5}",
                YomawariFormat.Line(3, 12.3456, 700, "heartbeat", "\"fps\":" + YomawariFormat.Num(59.5)));
            Assert.AreEqual(
                "[YOMAWARI] {\"seq\":0,\"t\":0.000,\"f\":0,\"ev\":\"boot\"}",
                YomawariFormat.Line(0, 0, 0, "boot", null));
        }

        [Test]
        public void 改行を含むメッセージでも一行になる ()
        {
            var line = YomawariFormat.Line(1, 1, 1, "log_error",
                "\"message\":" + YomawariFormat.Str("NullReferenceException\nat Foo()"));
            StringAssert.DoesNotContain("\n", line);
        }

        [Test]
        public void 小数点はロケールに関係なくピリオド ()
        {
            var saved = Thread.CurrentThread.CurrentCulture;
            try
            {
                Thread.CurrentThread.CurrentCulture = new CultureInfo("de-DE");
                StringAssert.Contains("\"t\":1.500", YomawariFormat.Line(0, 1.5, 0, "x", null));
                Assert.AreEqual("59.5", YomawariFormat.Num(59.5));
            }
            finally
            {
                Thread.CurrentThread.CurrentCulture = saved;
            }
        }
    }
}
