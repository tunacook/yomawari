using System.Collections.Generic;
using System.Text;
using Naninovel;
using Naninovel.UI;
using UnityEngine;
using UnityEngine.UI;

namespace Yomawari
{
    /**
     * <summary>
     * 画面に出ている Naninovel の UI を 0.2 秒ごとに見て、変化があったときだけ出す。
     * - menu / menu_close: IUIManager が管理する UI の表示・非表示と、その中の押せるボタン（name, rect）
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

        readonly List<IManagedUI> uis = new();
        readonly HashSet<IManagedUI> seen = new();
        readonly Dictionary<IManagedUI, Shown> shown = new();
        readonly List<IManagedUI> closed = new();
        readonly List<Selectable> selectables = new();
        string choicesSignature;
        float elapsed;

        // choices_done の index（直近に出した choices の中での順番）。見つからなければ -1
        public static int IndexOfChoice(string id)
        {
            return choiceIds.IndexOf(id);
        }

        void Update()
        {
            elapsed += Time.unscaledDeltaTime;
            if (elapsed < Interval) return;
            elapsed = 0f;
            if (!Engine.Initialized || !Engine.TryGetService<IUIManager>(out var manager)) return;
            ScanUIs(manager);
            ScanChoices();
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
                if (shown.TryGetValue(ui, out var prev) && prev.Signature == signature) continue;
                shown[ui] = new Shown { Name = mb.name, Signature = signature };
                YomawariLog.Emit("menu",
                    "\"name\":" + YomawariFormat.Str(mb.name) +
                    ",\"modal\":" + (manager.IsActiveModal(ui) ? "true" : "false") +
                    ",\"buttons\":" + buttons);
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

        // 押せるボタン（Selectable で interactable なもの）の [{name, rect}]
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
                if (json.Length > 1) json.Append(',');
                json.Append("{\"name\":").Append(YomawariFormat.Str(s.name))
                    .Append(",\"rect\":").Append(YomawariFormat.Rect(min.x, min.y, max.x, max.y, Screen.width, Screen.height))
                    .Append('}');
                AppendSignature(sig, s.name, min, max);
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
                if (json.Length > 1) json.Append(',');
                json.Append("{\"id\":").Append(YomawariFormat.Str(c.Id))
                    .Append(",\"text\":").Append(YomawariFormat.Str(c.Summary))
                    .Append(",\"rect\":").Append(YomawariFormat.Rect(min.x, min.y, max.x, max.y, Screen.width, Screen.height))
                    .Append(",\"locked\":").Append(c.Locked ? "true" : "false")
                    .Append('}');
                AppendSignature(sig, c.Id, min, max);
                ids.Add(c.Id);
            }

            var signature = sig.ToString();
            if (signature == choicesSignature) return;
            choicesSignature = signature;
            if (ids.Count == 0) return; // 消えたことは choices_done で伝わる
            choiceIds.Clear();
            choiceIds.AddRange(ids);
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
