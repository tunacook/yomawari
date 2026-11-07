using Unity.Profiling;
using UnityEngine;

namespace Yomawari
{
    /**
     * <summary>
     * 1 秒ごとに heartbeat を出す。メインスレッドが止まる（ハング）と途絶えるので、ハーネスはそれで検出する。
     * パフォーマンスの判定のため、その 1 秒でいちばん長かったフレームの時間（frame_max、ms）と、Unity 標準の
     * ProfilerRecorder で読めるメモリ（mem_total: Total Used Memory、mem_gfx: Gfx Used Memory。読めなければ出さない）も付ける。
     * 解像度か画面モードが変わったときは resolution も出す。
     * YomawariLog が -yomawari のときだけ生成する
     * </summary>
     */
    public class YomawariHeartbeat : MonoBehaviour
    {
        const float Interval = 1f;

        float elapsed;
        float frameMax;
        int frames;
        ProfilerRecorder totalMem;
        ProfilerRecorder gfxMem;
        int lastWidth = -1;
        int lastHeight = -1;
        FullScreenMode lastMode;

        void OnEnable()
        {
            totalMem = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "Total Used Memory");
            gfxMem = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "Gfx Used Memory");
        }

        void OnDisable()
        {
            totalMem.Dispose();
            gfxMem.Dispose();
        }

        void Update()
        {
            EmitResolutionIfChanged();

            // timeScale の影響を受けないよう unscaledDeltaTime で数える
            var dt = Time.unscaledDeltaTime;
            elapsed += dt;
            if (dt > frameMax) frameMax = dt;
            frames++;
            if (elapsed < Interval) return;

            var fps = frames / elapsed;
            var fields = "\"fps\":" + YomawariFormat.Num(fps) +
                         ",\"frame_max\":" + YomawariFormat.Num(frameMax * 1000f) +
                         ",\"gc_mem\":" + System.GC.GetTotalMemory(false);
            if (totalMem.Valid) fields += ",\"mem_total\":" + totalMem.LastValue;
            if (gfxMem.Valid) fields += ",\"mem_gfx\":" + gfxMem.LastValue;
            YomawariLog.Emit("heartbeat", fields);
            elapsed = 0f;
            frameMax = 0f;
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
