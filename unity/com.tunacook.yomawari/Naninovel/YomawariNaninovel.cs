using System.Collections.Generic;
using System.Reflection;
using Naninovel;
using Naninovel.Commands;
using UnityEngine;
using UnityEngine.Scripting;

// ゲームのコードはこのアセンブリを参照しないので、指定しないと IL2CPP のストリッピングでアセンブリごと消える
[assembly: AlwaysLinkAssembly]

namespace Yomawari
{
    /**
     * <summary>
     * Naninovel のサービスのイベントから進行を出す: scene / line / line_done / choices_done / save / load / command。
     * 画面に出ている UI（menu / menu_close / choices / transition）は YomawariUiWatcher が見る。
     * Naninovel のエンジンが作り直されても（タイトルへ戻る等）、初期化のたびに購読し直す
     * </summary>
     */
    public static class YomawariNaninovel
    {
        static string lastScript;
        static string lastLineId;
        // コマンドの型 → (名前（@ のあとに書く別名）, 名前のない引数（対象）のフィールド)
        static readonly Dictionary<System.Type, (string name, FieldInfo target)> commandInfo = new();

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad), Preserve]
        static void Init()
        {
            lastScript = null;
            lastLineId = null;
            if (!YomawariLog.Enabled) return;
            Engine.OnInitializationFinished -= Attach;
            Engine.OnInitializationFinished += Attach;
            if (Engine.Initialized) Attach();

            var go = new GameObject(nameof(YomawariUiWatcher)) { hideFlags = HideFlags.HideAndDontSave };
            Object.DontDestroyOnLoad(go);
            go.AddComponent<YomawariUiWatcher>();
        }

        static void Attach()
        {
            var player = Engine.GetServiceOrErr<IScriptPlayer>();
            player.OnExecute += track => OnExecute(player, track);

            var printers = Engine.GetServiceOrErr<ITextPrinterManager>();
            printers.OnPrintStarted += args => OnPrintStarted(player, args);
            printers.OnPrintFinished += _ => YomawariLog.Emit("line_done", "\"line_id\":" + YomawariFormat.Str(lastLineId));

            var choices = Engine.GetServiceOrErr<IChoiceHandlerManager>();
            choices.OnActorAdded += id => Watch(choices.GetActor(id));
            var handlers = new List<IChoiceHandlerActor>();
            choices.CollectActors(handlers);
            foreach (var h in handlers) Watch(h);

            var state = Engine.GetServiceOrErr<IStateManager>();
            state.OnGameSaveStarted += a => EmitSaveLoad("save", a, "start");
            state.OnGameSaveFinished += a => EmitSaveLoad("save", a, "end");
            state.OnGameLoadStarted += a => EmitSaveLoad("load", a, "start");
            state.OnGameLoadFinished += a => EmitSaveLoad("load", a, "end");
        }

        // メイントラックのスクリプトが変わったら scene を出す。本文以外のコマンドは command で出す
        static void OnExecute(IScriptPlayer player, IScriptTrack track)
        {
            if (track != player.MainTrack) return;
            var path = track.PlaybackSpot.ScriptPath;
            if (!string.IsNullOrEmpty(path) && path != lastScript)
            {
                lastScript = path;
                YomawariLog.Emit("scene", "\"name\":" + YomawariFormat.Str(path));
            }
            EmitCommand(track);
        }

        // 演出（キャラ・背景・エフェクト・音など）のコマンドを出す。ハーネスは、実行の前後で FPS・CPU・GPU・メモリが
        // 悪くなっていないかを見る（DESIGN.md §10「パフォーマンス」）。本文（print）は line で出しているので出さない
        static void EmitCommand(IScriptTrack track)
        {
            var command = track.PlayedCommand;
            if (command == null || command is PrintText) return;
            var (name, field) = InfoOf(command.GetType());
            string target = null;
            if (field?.GetValue(command) is ICommandParameter p && p.HasValue && !p.DynamicValue)
                target = p.ToString();
            var spot = track.PlaybackSpot;
            YomawariLog.Emit("command",
                "\"name\":" + YomawariFormat.Str(name) +
                ",\"target\":" + YomawariFormat.Str(target) +
                ",\"line_id\":" + YomawariFormat.Str(spot.Valid ? spot.ScriptPath + ":" + spot.LineNumber + "." + spot.InlineIndex : null));
        }

        static (string, FieldInfo) InfoOf(System.Type type)
        {
            if (commandInfo.TryGetValue(type, out var info)) return info;
            var alias = type.GetCustomAttribute<Command.AliasAttribute>()?.Alias;
            var name = string.IsNullOrEmpty(alias) ? char.ToLowerInvariant(type.Name[0]) + type.Name.Substring(1) : alias;
            FieldInfo target = null;
            foreach (var f in CommandParameter.ExtractFields(type))
            {
                if (f.GetCustomAttribute<Command.AliasAttribute>()?.Alias != Command.NamelessParameterAlias) continue;
                target = f;
                break;
            }
            info = (name, target);
            commandInfo[type] = info;
            return info;
        }

        static void OnPrintStarted(IScriptPlayer player, PrintMessageArgs args)
        {
            var spot = player.MainTrack.PlaybackSpot;
            lastLineId = spot.Valid ? spot.ScriptPath + ":" + spot.LineNumber + "." + spot.InlineIndex : null;
            YomawariLog.Emit("line",
                "\"label\":" + YomawariFormat.Str(spot.Valid ? spot.ScriptPath : null) +
                ",\"line_id\":" + YomawariFormat.Str(lastLineId) +
                ",\"speaker\":" + YomawariFormat.Str(args.Message.Author?.Id) +
                ",\"text\":" + YomawariFormat.Str(args.Message.Text) +
                ",\"printer\":" + YomawariFormat.Str(args.Printer?.Id) +
                ",\"append\":" + (args.Append ? "true" : "false"));
        }

        static void Watch(IChoiceHandlerActor handler)
        {
            if (handler == null) return;
            handler.OnChoiceHandled += c =>
            {
                YomawariUiWatcher.Handled(c.Id);
                YomawariLog.Emit("choices_done",
                    "\"id\":" + YomawariFormat.Str(c.Id) +
                    ",\"index\":" + YomawariUiWatcher.IndexOfChoice(c.Id));
            };
        }

        static void EmitSaveLoad(string ev, GameSaveLoadArgs a, string phase)
        {
            YomawariLog.Emit(ev,
                "\"slot\":" + YomawariFormat.Str(a.SlotId) +
                ",\"type\":" + YomawariFormat.Str(a.Type.ToString()) +
                ",\"phase\":" + YomawariFormat.Str(phase));
        }
    }
}
