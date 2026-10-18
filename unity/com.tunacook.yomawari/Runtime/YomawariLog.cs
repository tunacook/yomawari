using UnityEngine;

namespace Yomawari
{
    /**
     * <summary>
     * Yomawari（夜間自動プレイのハーネス）にゲームの状態を伝える。起動引数 -yomawari があるときだけ
     * Player.log に [YOMAWARI] 行を出す。ハーネス側からゲームを操作する口は作らない（OS の入力だけ）。
     * 出すもの: boot（起動時）、heartbeat（1 秒ごと、YomawariHeartbeat）、log_error（Error / Exception / Assert）
     * </summary>
     */
    public static class YomawariLog
    {
        public static readonly bool Enabled = YomawariFormat.IsEnabled(System.Environment.GetCommandLineArgs());
        static int seq;

        public static void Emit(string ev, string body = null)
        {
            if (!Enabled) return;
            // Log にスタックを付ける設定でも [YOMAWARI] 行には付けない（heartbeat ごとに Player.log が膨らむため）
            Debug.LogFormat(LogType.Log, LogOption.NoStacktrace, null, "{0}",
                YomawariFormat.Line(seq++, Time.realtimeSinceStartupAsDouble, Time.frameCount, ev, body));
        }

        // できるだけ早く購読し、起動直後の例外も拾う
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        static void Init()
        {
            seq = 0;
            if (!Enabled) return;
            Application.logMessageReceived -= OnLog;
            Application.logMessageReceived += OnLog;
            Emit("boot",
                "\"version\":" + YomawariFormat.Str(Application.version) +
                ",\"build_type\":" + YomawariFormat.Str(Debug.isDebugBuild ? "development" : "release") +
                ",\"build_guid\":" + YomawariFormat.Str(Application.buildGUID) +
                ",\"unity\":" + YomawariFormat.Str(Application.unityVersion));
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        static void StartHeartbeat()
        {
            if (!Enabled) return;
            var go = new GameObject(nameof(YomawariHeartbeat)) { hideFlags = HideFlags.HideAndDontSave };
            Object.DontDestroyOnLoad(go);
            go.AddComponent<YomawariHeartbeat>();
        }

        // Error / Exception / Assert だけを出す。Emit 自身の Debug.Log は LogType.Log なので再帰しない。
        // logMessageReceived はメインスレッドのログだけを受け取る（別スレッドの例外は対象外）
        static void OnLog(string message, string stackTrace, LogType type)
        {
            if (type == LogType.Log || type == LogType.Warning) return;
            Emit("log_error",
                "\"type\":" + YomawariFormat.Str(type.ToString()) +
                ",\"message\":" + YomawariFormat.Str(message) +
                ",\"stack\":" + YomawariFormat.Str(stackTrace));
        }
    }
}
