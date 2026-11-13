using System.Collections.Generic;
using System.Text;
using Naninovel;
using Naninovel.UI;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace Yomawari
{
    /**
     * <summary>
     * 画面に出ている Naninovel の UI を 0.2 秒ごとに見て、変化があったときだけ出す。
     * - menu / menu_close: IUIManager が管理する UI の表示・非表示と、その中の押せるボタン（name, rect, type, role, hidden, covered）
     *   と、スクロール領域（scroll）。DESIGN.md §3、§9「ゲーム固有の仕様」①。ゲーム側に Yomawari 用の部品は求めない（§1）
     * - transition: ISceneTransitionUI / ILoadingUI の表示（start）と非表示（end）
     * - choices: 表示中の選択肢ボタン（id, text, rect, locked）
     * ボタンの位置はホバーの拡大などで少し動くので、変化の判定は 1/20 単位に丸めて行う（出す値は丸めない）
     * </summary>
     */
    public class YomawariUiWatcher : MonoBehaviour
    {
        const float Interval = 0.2f;
        const float SignatureGrid = 20f;

        class Shown
        {
            public string Name;
            public bool Transition;
            public string Signature;
        }

        static readonly List<string> choiceIds = new();
        // 選び終えた選択肢の id。選んだあと消えていくまでのボタンを、選択肢として出し直さない（ScanChoices。消えきったら忘れる）
        static readonly HashSet<string> handledIds = new();

        readonly List<IManagedUI> uis = new();
        readonly HashSet<IManagedUI> seen = new();
        readonly Dictionary<IManagedUI, Shown> shown = new();
        readonly List<IManagedUI> closed = new();
        readonly List<Selectable> selectables = new();
        readonly List<ScrollRect> scrollRects = new();
        readonly List<RaycastResult> hits = new();
        PointerEventData pointer;
        string choicesSignature = ""; // 空 = 選択肢なし。起動直後に空の choices を出さない
        string settingsSignature; // 最後に出した settings。null = まだ出していない
        float elapsed;

        // choices_done の index（直近に出した choices の中での順番）。見つからなければ -1
        public static int IndexOfChoice(string id)
        {
            return choiceIds.IndexOf(id);
        }

        // 選択肢が選ばれた（choices_done）
        public static void Handled(string id)
        {
            if (id != null) handledIds.Add(id);
        }

        void Update()
        {
            elapsed += Time.unscaledDeltaTime;
            if (elapsed < Interval) return;
            elapsed = 0f;
            if (!Engine.Initialized || !Engine.TryGetService<IUIManager>(out var manager)) return;
            ScanUIs(manager);
            ScanChoices();
            ScanSettings();
        }

        // ゲームの設定（言語・本文の出る速さ・音量）を、最初と変わったときだけ出す（settings）。どれも Naninovel の
        // 標準のサービスの値なので、設定画面がゲーム独自の部品でも読める。ウィンドウの大きさは resolution で出す。
        // 値は小数第 3 位まで（YomawariFormat.Num）で比べるので、スライダーの細かな揺れでは出し直さない
        void ScanSettings()
        {
            if (!Engine.TryGetService<ILocalizationManager>(out var locale) ||
                !Engine.TryGetService<ITextPrinterManager>(out var printers) ||
                !Engine.TryGetService<IAudioManager>(out var audio)) return;
            // 使える言語の一覧も出す（プロファイルの言語がゲームにあるかを、ハーネスが確かめる）
            var locales = new StringBuilder("[");
            foreach (var tag in locale.AvailableLocales)
            {
                if (locales.Length > 1) locales.Append(',');
                locales.Append(YomawariFormat.Str(tag));
            }
            locales.Append(']');
            var body = "\"locale\":" + YomawariFormat.Str(locale.SelectedLocale) +
                       ",\"locales\":" + locales +
                       ",\"reveal_speed\":" + YomawariFormat.Num(printers.BaseRevealSpeed) +
                       ",\"auto_delay\":" + YomawariFormat.Num(printers.BaseAutoDelay) +
                       ",\"master_volume\":" + YomawariFormat.Num(audio.MasterVolume) +
                       ",\"bgm_volume\":" + YomawariFormat.Num(audio.BgmVolume) +
                       ",\"sfx_volume\":" + YomawariFormat.Num(audio.SfxVolume) +
                       ",\"voice_volume\":" + YomawariFormat.Num(audio.VoiceVolume);
            if (body == settingsSignature) return;
            settingsSignature = body;
            YomawariLog.Emit("settings", body);
        }

        void ScanUIs(IUIManager manager)
        {
            uis.Clear();
            seen.Clear();
            manager.CollectUIs(uis);
            foreach (var ui in uis)
            {
                if (ui is not MonoBehaviour mb || mb == null) continue;
                seen.Add(ui);
                var visible = ui.Visible && mb.isActiveAndEnabled;
                if (!visible)
                {
                    if (shown.Remove(ui, out var s)) EmitClose(s);
                    continue;
                }

                var transition = ui is ISceneTransitionUI || ui is ILoadingUI;
                if (transition)
                {
                    if (shown.ContainsKey(ui)) continue;
                    shown[ui] = new Shown { Name = mb.name, Transition = true };
                    YomawariLog.Emit("transition", "\"phase\":\"start\",\"name\":" + YomawariFormat.Str(mb.name));
                    continue;
                }

                var buttons = Buttons(mb, out var signature);
                var scroll = Scrolls(mb, ref signature);
                if (shown.TryGetValue(ui, out var prev) && prev.Signature == signature) continue;
                shown[ui] = new Shown { Name = mb.name, Signature = signature };
                YomawariLog.Emit("menu",
                    "\"name\":" + YomawariFormat.Str(mb.name) +
                    ",\"modal\":" + (manager.IsActiveModal(ui) ? "true" : "false") +
                    ",\"buttons\":" + buttons +
                    ",\"scroll\":" + scroll);
            }

            // 破棄された UI は CollectUIs から消えるので、ここで閉じたことにする
            closed.Clear();
            foreach (var ui in shown.Keys)
                if (!seen.Contains(ui)) closed.Add(ui);
            foreach (var ui in closed)
            {
                EmitClose(shown[ui]);
                shown.Remove(ui);
            }
        }

        static void EmitClose(Shown s)
        {
            if (s.Transition) YomawariLog.Emit("transition", "\"phase\":\"end\",\"name\":" + YomawariFormat.Str(s.Name));
            else YomawariLog.Emit("menu_close", "\"name\":" + YomawariFormat.Str(s.Name));
        }

        // 押せるボタン（Selectable で interactable なもの）の [{name, rect, type, role, hidden, covered}]。
        // ボタンの文字は出さない。ハーネスが画面から読む（DESIGN.md §9「画面の文字を読む」。UI の部品の作りに頼らないため）
        // covered は、中心をクリックしたときに EventSystem のレイキャストがほかの UI に先に当たるもの（上に重なった画面など）。
        // rect は祖先の RectMask2D / Mask と画面で切り取った見えている部分。中心が見えていないボタンは hidden にして、
        // 矩形は切り取らずに出す（ハーネスは同じ名前のボタンを出てきた順で数えるので、スクロールしても番号が変わらないように）
        string Buttons(MonoBehaviour root, out string signature)
        {
            var json = new StringBuilder("[");
            var sig = new StringBuilder();
            root.GetComponentsInChildren(false, selectables);
            foreach (var s in selectables)
            {
                if (!s.IsInteractable() || !s.isActiveAndEnabled) continue;
                var rt = (RectTransform)s.transform;
                if (!YomawariRect.TryGetScreen(rt, out var min, out var max)) continue;
                var hidden = !Clip(rt, ref min, ref max);
                var covered = !hidden && Covered(s, (min + max) / 2);
                var role = Role(s);
                if (json.Length > 1) json.Append(',');
                json.Append("{\"name\":").Append(YomawariFormat.Str(s.name))
                    .Append(",\"rect\":").Append(YomawariFormat.Rect(min.x, min.y, max.x, max.y, Screen.width, Screen.height))
                    .Append(",\"type\":").Append(YomawariFormat.Str(TypeOf(s)));
                if (role != null) json.Append(",\"role\":").Append(YomawariFormat.Str(role));
                if (hidden) json.Append(",\"hidden\":true");
                if (covered) json.Append(",\"covered\":true");
                json.Append('}');
                AppendSignature(sig, s.name + "|" + (hidden ? "~" : "") + (covered ? "^" : ""), min, max);
            }
            signature = sig.ToString();
            return json.Append(']').ToString();
        }

        // 画面座標 center をクリックしたとき、いちばん手前に当たるのが s（かその子）でなければ true。
        // EventSystem がない・何にも当たらないときは分からないので false
        bool Covered(Selectable s, Vector2 center)
        {
            var es = EventSystem.current;
            if (es == null) return false;
            pointer ??= new PointerEventData(es);
            pointer.position = center;
            hits.Clear();
            es.RaycastAll(pointer, hits);
            if (hits.Count == 0) return false;
            var top = hits[0].gameObject;
            return top != null && !top.transform.IsChildOf(s.transform);
        }

        // 中心が見えていれば、矩形を祖先の RectMask2D / Mask と画面で切り取って true
        static bool Clip(RectTransform rt, ref Vector2 min, ref Vector2 max)
        {
            var center = (min + max) / 2;
            Vector2 cmin = Vector2.zero, cmax = new Vector2(Screen.width, Screen.height);
            for (var t = rt.parent; t != null; t = t.parent)
            {
                var masked = t.TryGetComponent<RectMask2D>(out var m2) && m2.isActiveAndEnabled ||
                             t.TryGetComponent<Mask>(out var m) && m.isActiveAndEnabled;
                if (!masked || !YomawariRect.TryGetScreen((RectTransform)t, out var mmin, out var mmax)) continue;
                cmin = Vector2.Max(cmin, mmin);
                cmax = Vector2.Min(cmax, mmax);
            }
            if (center.x < cmin.x || center.x > cmax.x || center.y < cmin.y || center.y > cmax.y) return false;
            min = Vector2.Max(min, cmin);
            max = Vector2.Min(max, cmax);
            return true;
        }

        // DESIGN.md §3 の type。TextMeshPro の部品は、TextMeshPro を参照しなくて済むように型の名前で見る
        static string TypeOf(Selectable s)
        {
            switch (s)
            {
                case Button: return "button";
                case Toggle: return "toggle";
                case Dropdown: return "dropdown";
                case Slider: return "slider";
                case InputField: return "input";
            }
            for (var t = s.GetType(); t != null && t != typeof(Selectable); t = t.BaseType)
            {
                if (t.Name == "TMP_Dropdown") return "dropdown";
                if (t.Name == "TMP_InputField") return "input";
            }
            return "other";
        }

        // Naninovel の標準の部品から分かる役割（§9「ゲーム固有の仕様」①）。分からなければ null（ハーネスが推定する）
        static string Role(Selectable s)
        {
            if (s.TryGetComponent<TitleExitButton>(out _)) return "exit";
            if (s.TryGetComponent<ControlPanelTitleButton>(out _)) return "to_title";
            if (s.TryGetComponent<TitleNewGameButton>(out _)) return "new_game";
            if (s.TryGetComponent<GameSettingsLanguageDropdown>(out _) ||
                s.TryGetComponent<GameSettingsResolutionDropdown>(out _) ||
                s.TryGetComponent<GameSettingsScreenModeDropdown>(out _) ||
                s.TryGetComponent<GameSettingsGraphicsDropdown>(out _)) return "system_setting";
            return null;
        }

        // スクロール領域ごとの {rect, up, down, left, right}（その方向にまだ項目があるか）
        string Scrolls(MonoBehaviour root, ref string signature)
        {
            const float Eps = 0.001f;
            var json = new StringBuilder("[");
            var sig = new StringBuilder(signature);
            root.GetComponentsInChildren(false, scrollRects);
            foreach (var sr in scrollRects)
            {
                if (!sr.isActiveAndEnabled || sr.content == null) continue;
                var view = sr.viewport != null ? sr.viewport : (RectTransform)sr.transform;
                if (!YomawariRect.TryGetScreen(view, out var min, out var max)) continue;
                var content = sr.content.rect;
                var viewRect = view.rect;
                var tallV = sr.vertical && content.height > viewRect.height + 0.5f;
                var wideH = sr.horizontal && content.width > viewRect.width + 0.5f;
                var v = sr.verticalNormalizedPosition;   // 1 が上端
                var h = sr.horizontalNormalizedPosition; // 0 が左端
                bool up = tallV && v < 1 - Eps, down = tallV && v > Eps;
                bool left = wideH && h > Eps, right = wideH && h < 1 - Eps;
                if (json.Length > 1) json.Append(',');
                json.Append("{\"rect\":").Append(YomawariFormat.Rect(min.x, min.y, max.x, max.y, Screen.width, Screen.height))
                    .Append(",\"up\":").Append(up ? "true" : "false")
                    .Append(",\"down\":").Append(down ? "true" : "false")
                    .Append(",\"left\":").Append(left ? "true" : "false")
                    .Append(",\"right\":").Append(right ? "true" : "false")
                    .Append('}');
                sig.Append(up ? 'U' : '-').Append(down ? 'D' : '-').Append(left ? 'L' : '-').Append(right ? 'R' : '-').Append(';');
            }
            signature = sig.ToString();
            return json.Append(']').ToString();
        }

        void ScanChoices()
        {
            var buttons = FindObjectsByType<ChoiceHandlerButton>(FindObjectsSortMode.None);
            // index を画面の並びに合わせる
            System.Array.Sort(buttons, (a, b) => a.transform.GetSiblingIndex().CompareTo(b.transform.GetSiblingIndex()));
            var json = new StringBuilder("[");
            var sig = new StringBuilder();
            var ids = new List<string>();
            foreach (var b in buttons)
            {
                if (!b.isActiveAndEnabled) continue;
                var rt = (RectTransform)b.transform;
                if (!YomawariRect.TryGetScreen(rt, out var min, out var max)) continue;
                var c = b.Choice;
                // 消えていく途中でスクリプトが解放されると、文字を解決できずに Error が飛ぶ。
                // 捕まえないと Update の未処理例外になり、ゲームの不具合（L-01）として報告されるので、消えたボタンとして扱う
                string text;
                try { text = c.Summary; }
                catch (Error) { continue; }
                if (json.Length > 1) json.Append(',');
                json.Append("{\"id\":").Append(YomawariFormat.Str(c.Id))
                    .Append(",\"text\":").Append(YomawariFormat.Str(text))
                    .Append(",\"rect\":").Append(YomawariFormat.Rect(min.x, min.y, max.x, max.y, Screen.width, Screen.height))
                    .Append(",\"locked\":").Append(c.Locked ? "true" : "false")
                    .Append('}');
                AppendSignature(sig, c.Id, min, max);
                ids.Add(c.Id);
            }

            // 選び終えた選択肢と同じ組は、選ばなかったほうも含めて消えていく途中なので出さない。
            // 消えきったら忘れる（ロールバック・ロードで同じ選択肢が出直したときは出す）
            if (ids.Count == 0) handledIds.Clear();
            else if (ids.Exists(handledIds.Contains))
            {
                json.Length = 1;
                sig.Length = 0;
                ids.Clear();
            }

            var signature = sig.ToString();
            if (signature == choicesSignature) return;
            choicesSignature = signature;
            // 選ばずに消えた（ロールバック・ロード・スクリプトで消した）ときも伝わるように、消えたら items を空で出す
            if (ids.Count > 0)
            {
                choiceIds.Clear();
                choiceIds.AddRange(ids);
            }
            YomawariLog.Emit("choices", "\"items\":" + json.Append(']'));
        }

        static void AppendSignature(StringBuilder sig, string key, Vector2 min, Vector2 max)
        {
            float w = Screen.width, h = Screen.height;
            sig.Append(key).Append('@')
                .Append(Mathf.RoundToInt(min.x / w * SignatureGrid)).Append(',')
                .Append(Mathf.RoundToInt(min.y / h * SignatureGrid)).Append(',')
                .Append(Mathf.RoundToInt(max.x / w * SignatureGrid)).Append(',')
                .Append(Mathf.RoundToInt(max.y / h * SignatureGrid)).Append(';');
        }
    }
}
