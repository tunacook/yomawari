using UnityEngine;

namespace Yomawari
{
    /**
     * <summary>
     * 1 秒ごとに heartbeat を出す。メインスレッドが止まる（ハング）と途絶えるので、ハーネスはそれで検出する。
     * YomawariLog が -yomawari のときだけ生成する
     * </summary>
     */
    public class YomawariHeartbeat : MonoBehaviour
    {
        const float Interval = 1f;

        float elapsed;
        int frames;

        void Update()
        {
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
    }
}
