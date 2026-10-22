using UnityEngine;

namespace Yomawari
{
    /**
     * <summary>
     * 1 秒ごとに heartbeat を出す。メインスレッドが止まる（ハング）と途絶えるので、ハーネスはそれで検出する。
     * 解像度か画面モードが変わったときは resolution も出す。
     * YomawariLog が -yomawari のときだけ生成する
     * </summary>
     */
    public class YomawariHeartbeat : MonoBehaviour
    {
        const float Interval = 1f;

        float elapsed;
        int frames;
        int lastWidth = -1;
        int lastHeight = -1;
        FullScreenMode lastMode;

        void Update()
        {
            EmitResolutionIfChanged();

            // timeScale の影響を受けないよう unscaledDeltaTime で数える
            elapsed += Time.unscaledDeltaTime;
            frames++;
            if (elapsed < Interval) return;

            var fps = frames / elapsed;
            YomawariLog.Emit("heartbeat",
                "\"fps\":" + YomawariFormat.Num(fps) +
                ",\"gc_mem\":" + System.GC.GetTotalMemory(false));
            elapsed = 0f;
            frames = 0;
        }

        // 描画領域のピクセル数と画面モードを、最初と変わったときだけ出す。
        // ハーネスはウィンドウの外枠（タイトルバー込み）とこの値から、描画領域の位置と Retina の倍率を求める
        void EmitResolutionIfChanged()
        {
            int w = Screen.width, h = Screen.height;
            var mode = Screen.fullScreenMode;
            if (w == lastWidth && h == lastHeight && mode == lastMode) return;
            lastWidth = w;
            lastHeight = h;
            lastMode = mode;
            YomawariLog.Emit("resolution",
                "\"w\":" + YomawariFormat.Num(w) +
                ",\"h\":" + YomawariFormat.Num(h) +
                ",\"mode\":" + YomawariFormat.Str(mode.ToString()));
        }
    }
}
