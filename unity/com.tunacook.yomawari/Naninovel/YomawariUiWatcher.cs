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
     * - selected: EventSystem のフォーカスがあるボタン（menu, name, nth）。Enter で押されるのはこのボタン
     * - bgm_play / bgm_stop / voice / voice_stop: IAudioManager で実際に鳴っている BGM・ボイス（id はリソースのパス）
     * - input: 止められている Naninovel の入力（muted。IInputManager.IsMuted）と、本文送りが止められているか（advance_muted）
     * - 開いているドロップダウン（標準の Dropdown / TMP_Dropdown）のリスト: "&lt;ドロップダウンの name&gt;.List" のモーダルな menu。
     *   リストはドロップダウンの子に作られるので、元の画面の buttons には入れない
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
        // この回に見つけた、開いているドロップダウンのリスト（リスト → ドロップダウンの name）と、出しているリストの menu
        readonly Dictionary<GameObject, string> openLists = new();
        readonly Dictionary<string, string> listShown = new(); // menu の name → signature
        readonly List<string> listClosed = new();
        PointerEventData pointer;
        string choicesSignature = ""; // 空 = 選択肢なし。起動直後に空の choices を出さない
        string settingsSignature; // 最後に出した settings。null = まだ出していない
        string selectedSignature = ""; // 最後に出した selected。空 = フォーカスのあるボタンなし（起動直後は出さない）
        string inputSignature; // 最後に出した input。null = まだ出していない
        HashSet<string> bgmShown = new(); // 最後に鳴っていると出した BGM
        HashSet<string> bgmNow = new();
        string voiceShown; // 最後に鳴っていると出したボイス
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
            ScanInput();
            ScanSelected();
            ScanAudio();
        }

        // 実際に鳴っている BGM とボイスを見て、鳴り始めた・止まったときに出す。IAudioManager の OnPlayBgm などは再生・停止を
        // 頼まれたときに来るイベントで、ボイスが最後まで鳴って終わったときには来ないので、鳴っているかどうかを見に行く。
        // ハーネスは、BGM が重なったまま鳴り続ける（A-01）、タイトルに戻ってもボイスが鳴り続ける（A-02）を見る。
        // 0.2 秒より短いあいだ止まってまた鳴ったもの（同じ曲をすぐ鳴らし直したなど）は、続けて鳴っていたものとして扱う
        void ScanAudio()
        {
            if (!Engine.TryGetService<IAudioManager>(out var audio)) return;
            bgmNow.Clear();
            audio.CollectPlayedBgm(bgmNow);
            foreach (var path in bgmShown)
                if (!bgmNow.Contains(path)) YomawariLog.Emit("bgm_stop", "\"id\":" + YomawariFormat.Str(path));
            foreach (var path in bgmNow)
                if (!bgmShown.Contains(path)) YomawariLog.Emit("bgm_play", "\"id\":" + YomawariFormat.Str(path));
            (bgmShown, bgmNow) = (bgmNow, bgmShown);

            var voice = audio.GetPlayedVoice();
            if (voice == voiceShown) return;
            if (voiceShown != null) YomawariLog.Emit("voice_stop", "\"id\":" + YomawariFormat.Str(voiceShown));
            if (voice != null) YomawariLog.Emit("voice", "\"id\":" + YomawariFormat.Str(voice));
            voiceShown = voice;
        }

        // EventSystem のフォーカスがあるボタン（selected）を、変わったときだけ出す。Enter・Space で押されるのはこのボタンで、
        // ハーネスは、終了ボタンなどにフォーカスがあるあいだ Enter を押さない（run 2026-11-14_2214: 方向キーで右クリック
        // メニューの終了ボタンにフォーカスを移し、Enter でゲームを終了した）。ボタンは、それを含む表示中の menu のうち
        // いちばん内側のものの name と、menu の buttons と同じ数え方の番号（同じ名前のボタンの何番目か。0 から）で表す。
        // menu の外のもの・押せないものにフォーカスがあるときは、menu と name を null で出す
        void ScanSelected()
        {
            var es = EventSystem.current;
            var go = es != null ? es.currentSelectedGameObject : null;
            string menu = null, name = null;
            var nth = 0;
            if (go != null && go.TryGetComponent<Selectable>(out var sel) && Pressable(sel))
            {
                MonoBehaviour owner = null;
                var depth = -1;
                foreach (var pair in shown)
                {
                    if (pair.Value.Transition || pair.Key is not MonoBehaviour mb || mb == null) continue;
                    if (!sel.transform.IsChildOf(mb.transform)) continue;
                    var d = 0;
                    for (var t = mb.transform; t != null; t = t.parent) d++;
                    if (d <= depth) continue;
                    owner = mb;
                    depth = d;
                }
                if (owner != null)
                {
                    owner.GetComponentsInChildren(false, selectables);
                    foreach (var s in selectables)
                    {
                        if (s == sel) break;
                        if (s.name == sel.name && Pressable(s)) nth++;
                    }
                    menu = owner.name;
                    name = sel.name;
                }
            }
            var signature = menu == null ? "" : menu + "/" + name + "#" + nth;
            if (signature == selectedSignature) return;
            selectedSignature = signature;
            YomawariLog.Emit("selected", menu == null
                ? "\"menu\":null,\"name\":null"
                : "\"menu\":" + YomawariFormat.Str(menu) + ",\"name\":" + YomawariFormat.Str(name) + ",\"nth\":" + nth);
        }

        // menu の buttons に出すもの（Buttons と同じ条件）
        static bool Pressable(Selectable s)
        {
            return s.IsInteractable() && s.isActiveAndEnabled &&
                   YomawariRect.TryGetScreen((RectTransform)s.transform, out _, out _);
        }

        // ゲームの設定を、最初と変わったときだけ出す（settings）。Naninovel の標準の設定画面にある項目（言語・本文の出る速さ・
        // オートの待ち時間・音量・スキップの方式・フォント・文字の大きさ・ボイスの言語・画質・画面モード・解像度）の値で、
        // どれも Naninovel の標準のサービスか Unity の値なので、設定画面がゲーム独自の部品でも読める。ゲームがどの項目を
        // プレイヤーに変えさせるかは、ここでは分からない（ハーネスのプロファイルに書く。DESIGN.md §9「設定を変えに行く」）。
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
                       ",\"voice_volume\":" + YomawariFormat.Num(audio.VoiceVolume) +
                       ",\"voice_locale\":" + YomawariFormat.Str(audio.VoiceLocale) +
                       ",\"screen_mode\":" + YomawariFormat.Str(Screen.fullScreenMode.ToString()) +
                       ",\"resolution\":" + YomawariFormat.Str(Screen.width + "x" + Screen.height) +
                       ",\"quality\":" + QualitySettings.GetQualityLevel();
            if (Engine.TryGetService<IScriptPlayer>(out var player))
                body += ",\"skip_mode\":" + YomawariFormat.Str(player.SkipMode.ToString());
            if (Engine.TryGetService<IUIManager>(out var ui))
                body += ",\"font\":" + YomawariFormat.Str(ui.FontName) + ",\"font_size\":" + ui.FontSize;
            if (body == settingsSignature) return;
            settingsSignature = body;
            YomawariLog.Emit("settings", body);
        }

        // 見る入力: 本文送り・スキップ・オート・巻き戻し・メニュー・UI を隠す・バックログ（Naninovel の標準の入力）
        static readonly string[] WatchedInputs =
        {
            Inputs.Continue, Inputs.Skip, Inputs.ToggleSkip, Inputs.AutoPlay, Inputs.Rollback,
            Inputs.Pause, Inputs.ToggleUI, Inputs.ShowBacklog,
        };

        // 止められている入力を、最初と変わったときだけ出す（input）。@processInput の止め方も、AddMuter で一時的に止める
        // 方法（UI を隠すあいだなど）も、入力の処理自体が止まっている（Enabled が false）のも含めた、実際に効いている状態
        // （IInputManager.IsMuted）。入力の割り当てがない id も止められているものとして出る。
        // 本文送りの入力（Naninovel では Continue）が止められているかは advance_muted で出す。ハーネスは、そのあいだ
        // 押しても進まないのを進行不能（S-02）と数えない（入力の名前はアセットごとに違うので、ハーネスは muted を見ない）
        // スキップの入力（押しているあいだスキップする Skip と、押すたびに切り替える ToggleSkip）は、割り当てられた
        // キーボードのキー（skip_keys / toggle_skip_keys。Input System のキーの名前）と、止められているか（skip_muted）を出す。
        // ハーネスは、読んだことのある本文をスキップで送る（DESIGN.md §9「読んだ本文をスキップで送る」）
        void ScanInput()
        {
            if (!Engine.TryGetService<IInputManager>(out var input)) return;
            var muted = new StringBuilder("[");
            foreach (var id in WatchedInputs)
            {
                if (!input.IsMuted(id)) continue;
                if (muted.Length > 1) muted.Append(',');
                muted.Append(YomawariFormat.Str(id));
            }
            var body = "\"muted\":" + muted.Append(']') +
                       ",\"advance_muted\":" + (input.IsMuted(Inputs.Continue) ? "true" : "false") +
                       ",\"skip_muted\":" + (input.IsMuted(Inputs.Skip) && input.IsMuted(Inputs.ToggleSkip) ? "true" : "false") +
                       ",\"skip_keys\":" + KeyboardKeys(Inputs.Skip) +
                       ",\"toggle_skip_keys\":" + KeyboardKeys(Inputs.ToggleSkip);
            if (body == inputSignature) return;
            inputSignature = body;
            YomawariLog.Emit("input", body);
        }

        readonly Dictionary<string, string> keysOf = new Dictionary<string, string>();

        // Naninovel の入力（id）に割り当てられた、キーボードのキーの名前の配列（"[\"ctrl\"]"）。Naninovel が入力を作るのと
        // 同じ、入力の設定（InputConfiguration）の InputActions（なければ Naninovel の既定）と ActionMaps から読む。
        // 組み合わせ（Composite）と、キーボード以外の割り当ては出さない。Input System がなければ空
        string KeyboardKeys(string id)
        {
            if (keysOf.TryGetValue(id, out var cached)) return cached;
            var json = new StringBuilder("[");
#if ENABLE_INPUT_SYSTEM && YOMAWARI_INPUT_SYSTEM
            var config = Engine.GetConfiguration<InputConfiguration>();
            var asset = config.InputActions != null
                ? config.InputActions
                : Resources.Load<UnityEngine.InputSystem.InputActionAsset>("Naninovel/Input/DefaultControls");
            if (asset != null)
                foreach (var mapName in config.ActionMaps)
                {
                    var action = asset.FindActionMap(mapName)?.FindAction(id);
                    if (action == null) continue;
                    foreach (var binding in action.bindings)
                    {
                        const string keyboard = "<Keyboard>/";
                        var path = binding.effectivePath;
                        if (binding.isComposite || binding.isPartOfComposite || path == null ||
                            !path.StartsWith(keyboard)) continue;
                        if (json.Length > 1) json.Append(',');
                        json.Append(YomawariFormat.Str(path.Substring(keyboard.Length)));
                    }
                }
#endif
            var keys = json.Append(']').ToString();
            keysOf[id] = keys;
            return keys;
        }

        void ScanUIs(IUIManager manager)
        {
            uis.Clear();
            seen.Clear();
            openLists.Clear();
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
            ScanLists();
        }

        // 開いているドロップダウンのリストを、"<ドロップダウンの name>.List" のモーダルな menu として出す（DESIGN.md §9
        // 「ゲーム固有の仕様」①）。ハーネスは項目を選ぶか、リストの外を押して閉じる。閉じたら menu_close を出す
        void ScanLists()
        {
            foreach (var pair in openLists)
            {
                var name = pair.Value + ".List";
                var buttons = Buttons(pair.Key.transform, out var signature);
                var scroll = Scrolls(pair.Key.transform, ref signature);
                if (listShown.TryGetValue(name, out var prev) && prev == signature) continue;
                listShown[name] = signature;
                YomawariLog.Emit("menu",
                    "\"name\":" + YomawariFormat.Str(name) + ",\"modal\":true,\"buttons\":" + buttons + ",\"scroll\":" + scroll);
            }
            listClosed.Clear();
            foreach (var name in listShown.Keys)
            {
                var open = false;
                foreach (var d in openLists.Values) open |= d + ".List" == name;
                if (!open) listClosed.Add(name);
            }
            foreach (var name in listClosed)
            {
                listShown.Remove(name);
                YomawariLog.Emit("menu_close", "\"name\":" + YomawariFormat.Str(name));
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
        // root の中に開いているドロップダウンがあれば、そのリストを openLists に入れ、リストの項目はここに入れない（ScanLists で出す）
        string Buttons(Component root, out string signature)
        {
            var json = new StringBuilder("[");
            var sig = new StringBuilder();
            root.GetComponentsInChildren(false, selectables);
            foreach (var s in selectables)
            {
                var opened = YomawariDropdown.OpenList(s);
                if (opened != null && opened != root.gameObject) openLists[opened] = s.name;
            }
            foreach (var s in selectables)
            {
                if (!s.IsInteractable() || !s.isActiveAndEnabled) continue;
                if (InOpenList(s.transform, root.transform)) continue;
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

        // t が root の中で開いているドロップダウンのリストの中にあるか
        bool InOpenList(Transform t, Transform root)
        {
            foreach (var l in openLists.Keys)
                if (l != null && l.transform != root && l.transform.IsChildOf(root) && t.IsChildOf(l.transform)) return true;
            return false;
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
        string Scrolls(Component root, ref string signature)
        {
            const float Eps = 0.001f;
            var json = new StringBuilder("[");
            var sig = new StringBuilder(signature);
            root.GetComponentsInChildren(false, scrollRects);
            foreach (var sr in scrollRects)
            {
                if (!sr.isActiveAndEnabled || sr.content == null || InOpenList(sr.transform, root.transform)) continue;
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
