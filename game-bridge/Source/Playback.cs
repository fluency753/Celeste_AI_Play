using System;
using System.Collections.Generic;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 规划回放（主控的 playback 命令，见 orchestrator/playback.py）：把智能体在模拟器里推演过的路线画进游戏。
/// 智能体每规划完一轮，主控就发来这一轮的一段回放；真实执行之前再发一段"最终路线"。回放期间游戏完全不推进
/// （帧闸门照常拦住 Engine.Update），桥接只是每个节拍重画一次画面：
/// - 一轮的路线按帧号逐渐延伸（每个画面帧推进 speed 帧游戏时间），延伸中一律淡绿色细线（线多时整体再调淡，见 dim）；
///   一条线走到头的那一帧定下结果：死亡的从死亡点往起点一路变成淡红，这一轮最快的一条从终点往起点一路变成实心金色，
///   变色每个画面帧传导 recolor 帧（默认 4 倍速）；其余的（出口、按键用完）保持淡绿。只画线，不画角色；
/// - 一轮放完，这一轮的线在最后 fade 帧里变成淡灰，留在画面上（历史层），直到真实执行开始才清掉；
/// - 镜头临时借用：渲染前把关卡镜头换成回放镜头（跟着金色路线的前端平滑移动、受房间边界限制），渲染后原样换回，
///   连同 Level.cameraPreShake；渲染期间随机数换成单独的发生器（Calc.PushRandom），游戏状态一点不变。
///   两轮之间（主控等智能体规划下一轮时）画面停在回放镜头上，历史线照常画；
/// - 执行前的最后一段（final）：不画新的线，镜头用 return 帧平滑回到 Madeline（镜头本来就在那里时直接结束）；
///   真实执行开始时清掉全部线条；
/// - 观测截图的那一帧不换镜头、不画回放，智能体看到的永远是游戏本来的画面；
/// - 刚进章节时圆圈转场（Level.Wipe）还没放完：回放帧先把转场放完（只是画面效果：转场只影响能不能暂停），
///   黑屏消失之后才开始画线；
/// - 回放帧里音乐照常播放、音效暂停（见 Bridge.UpdateAudio）。
/// 录制时每个回放帧照常回复 OBS 插件，章节计时记为 -1，声音保持冻结。
/// </summary>
internal static class Playback {
    private sealed class Track {
        public float[] F = Array.Empty<float>();       // 每个点的帧号（共线的点已合并）
        public Vector2[] P = Array.Empty<Vector2>();
        public int Kind;                               // 0 其他、1 死亡、2 出口、3 最快
        public int Delay;                              // 第几个画面帧开始出现
        public int Cursor;
        public Color Faded;                            // 这一轮放完之后的颜色（淡灰）
        public float Last => F.Length > 0 ? F[^1] : 0f;
        public float FinishAt => Delay + Last / speed;  // 走到头的画面帧
    }

    private static readonly Color Gold = new(0xF5, 0xB8, 0x41);
    private static readonly Color Green = new(0x4C, 0xC3, 0x8A);
    private static readonly Color Red = new(0xE0, 0x47, 0x5B);
    private static readonly Color Gray = new(0xC8, 0xCC, 0xD6);
    private const float GreenAlpha = 0.28f, RedAlpha = 0.32f, GrayAlpha = 0.09f;
    private static readonly Vector2 Lift = new(0f, -5f);   // 线画在身体中部，不贴着脚下

    private static long requestId;
    private static bool running;
    private static bool overlay;                           // 回放镜头与历史线留在画面上（直到真实执行开始）
    private static bool final;                             // 执行前的最后一段：只把镜头送回 Madeline
    private static readonly List<Track> Tracks = new();
    private static readonly List<Track> History = new();
    private static float speed = 2f;
    private static float dim = 1f;                         // 淡色线条的整体透明度系数（线多时主控调低，免得叠成一片）
    private static float recolor = 4f;                     // 定下结果后的变色传导速度（每个画面帧传导几帧路线）
    private static int frames, returnFrames, fade = 15, frame;
    private static Vector2[] focus = Array.Empty<Vector2>();
    private static int focusEvery = 4;
    private static Vector2 cam, realCam, returnFrom;
    private static bool camValid, returnStarted;
    private static Vector2 savedCam, savedPreShake;
    private static bool swapped;

    public static bool Running => running;
    public static long StepCount;                          // 画过的回放帧数（桥接据此在回放时放音乐）
    private static int wipeTicks;
    public static bool Done => running && frame >= frames + returnFrames;

    // ---- 命令 ----

    public static string? Start(long id, JsonElement a, Level level) {
        if (running) return "a playback is already running";
        Tracks.Clear();
        final = GetBool(a, "final", false);
        speed = Math.Clamp(GetFloat(a, "speed", 2f), 0.1f, 60f);
        frames = Math.Clamp(GetInt(a, "frames", 60), final ? 0 : 1, 60 * 120);
        returnFrames = Math.Clamp(GetInt(a, "return", 0), 0, 600);
        fade = Math.Clamp(GetInt(a, "fade", 15), 0, frames);
        dim = Math.Clamp(GetFloat(a, "dim", 1f), 0.05f, 1f);
        recolor = Math.Clamp(GetFloat(a, "recolor", 4f), 0.1f, 240f);
        focusEvery = Math.Max(1, GetInt(a, "focus_every", 4));
        focus = ReadPoints(a, "focus");
        Color faded = Gray * (GrayAlpha * dim);
        if (a.TryGetProperty("tracks", out JsonElement ts) && ts.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement t in ts.EnumerateArray()) {
                Track track = ReadTrack(t);
                if (track.P.Length == 0) continue;
                track.Kind = GetInt(t, "k", 0);
                track.Delay = GetInt(t, "d", 0);
                track.Faded = faded;
                Tracks.Add(track);
            }
        }
        realCam = level.Camera.Position;
        if (GetString(a, "camera", "keep") == "real" || !camValid) cam = realCam;
        if (final && Vector2.Distance(cam, realCam) < 0.5f) {
            frames = 0;            // 镜头本来就在 Madeline 这里：不用回，直接结束
            returnFrames = 0;
        }
        camValid = true;
        overlay = true;
        returnStarted = false;
        frame = 0;
        requestId = id;
        running = true;
        return null;
    }

    /// <summary>回放的最后一帧已经画出来之后（下一个节拍开头）回复主控。这一轮的线转进历史层。</summary>
    public static void Finish() {
        if (!running) return;
        running = false;
        History.AddRange(Tracks);
        Tracks.Clear();
        if (returnFrames > 0) cam = realCam;
        int drawn = frame;
        IpcServer.Send(Reply.Ok(requestId, w => w.WriteNumber("frames", drawn)));
    }

    /// <summary>真实执行开始：清掉历史线，镜头还给游戏（金色参考线留到这次执行结束）。</summary>
    public static void EndOverlay() {
        if (running) return;
        overlay = false;
        camValid = false;
        History.Clear();
    }

    public static void Reset() {
        running = false;
        Tracks.Clear();
        EndOverlay();
    }

    // ---- 每个回放帧 ----

    public static void Step(Level level) {
        if (!running) return;
        StepCount++;
        if (level.Wipe is { WipeIn: true }) {
            // 进章节的圆圈转场还没放完：先放完它（用单独的随机数），黑屏消失之后才开始这一段的时间线。
            // 进关卡的转场（SpotlightWipe 或章节自己的转场）没有完成回调，也没有协程等它，只影响能不能暂停
            Calc.PushRandom(104729 + wipeTicks++);
            try {
                level.Wipe.Update(level);
            } finally {
                Calc.PopRandom();
            }
            return;
        }
        frame++;
        if (frame <= frames) {
            Vector2 target = ClampCamera(level, FocusAt(frame) - new Vector2(160f, 90f));
            cam += (target - cam) * 0.12f;
        } else {
            if (!returnStarted) {
                returnFrom = cam;
                returnStarted = true;
            }
            float t = Math.Min(1f, (frame - frames) / (float) Math.Max(1, returnFrames));
            cam = Vector2.Lerp(returnFrom, realCam, Ease.CubeInOut(t));
        }
    }

    private static Vector2 FocusAt(int f) {
        if (focus.Length == 0) return cam + new Vector2(160f, 90f);
        float u = f / (float) focusEvery;
        int i = Math.Min(focus.Length - 1, (int) u);
        int j = Math.Min(focus.Length - 1, i + 1);
        return Vector2.Lerp(focus[i], focus[j], u - (int) u);
    }

    private static Vector2 ClampCamera(Level level, Vector2 p) {
        Rectangle b = level.Bounds;
        float x = Math.Clamp(p.X, b.Left, Math.Max(b.Left, b.Right - 320));
        float y = Math.Clamp(p.Y, b.Top, Math.Max(b.Top, b.Bottom - 180));
        return new Vector2(x, y);
    }

    /// <summary>路线在第 l 帧（游戏时间）的位置与每帧位移；顺带推进游标。</summary>
    private static (Vector2, Vector2) Head(Track t, float l) {
        if (t.Cursor >= t.F.Length) t.Cursor = 0;
        if (t.F[t.Cursor] > l) t.Cursor = 0;
        while (t.Cursor + 1 < t.F.Length && t.F[t.Cursor + 1] <= l) t.Cursor++;
        int i = t.Cursor;
        if (i + 1 >= t.P.Length) {
            Vector2 v = i > 0 ? (t.P[i] - t.P[i - 1]) / Math.Max(1f, t.F[i] - t.F[i - 1]) : Vector2.Zero;
            return (t.P[i], v);
        }
        float span = Math.Max(1e-3f, t.F[i + 1] - t.F[i]);
        float u = Math.Clamp((l - t.F[i]) / span, 0f, 1f);
        return (Vector2.Lerp(t.P[i], t.P[i + 1], u), (t.P[i + 1] - t.P[i]) / span);
    }

    // ---- 渲染 ----

    private static bool Showing => (running || overlay) && camValid && !Bridge.CapturePending;

    /// <summary>Celeste.RenderCore 之前：换上回放镜头与单独的随机数（回放进行中与两轮之间；观测截图的那一帧除外）。</summary>
    public static void BeforeRender() {
        if (!Showing || Engine.Scene is not Level level) return;
        savedCam = level.Camera.Position;
        savedPreShake = level.cameraPreShake;
        level.Camera.Position = cam;
        Calc.PushRandom(7919 + frame);
        swapped = true;
    }

    /// <summary>Celeste.RenderCore 之后：原样换回镜头与随机数。</summary>
    public static void AfterRender() {
        if (!swapped) return;
        swapped = false;
        Calc.PopRandom();
        if (Engine.Scene is Level level) {
            level.Camera.Position = savedCam;
            level.cameraPreShake = savedPreShake;
        }
    }

    /// <summary>GameplayRenderer 画完实体之后：用同一个镜头矩阵画历史线与这一轮的路线。</summary>
    public static void DrawWorld(Camera camera) {
        if (Bridge.CapturePending) return;     // 观测截图：智能体看到的是游戏本来的画面
        if (running && Engine.Scene is Level { Wipe.WipeIn: true }) return;   // 圆圈转场还没放完时不画
        bool drawOverlay = overlay && camValid;
        if (!running && !drawOverlay) return;
        Draw.SpriteBatch.Begin(SpriteSortMode.Deferred, BlendState.AlphaBlend, SamplerState.PointClamp,
            DepthStencilState.None, RasterizerState.CullNone, null, camera.Matrix);
        try {
            if (drawOverlay) {
                foreach (Track t in History) DrawRange(t, 0f, t.Last, t.Faded);
            }
            if (running) {
                DrawTracks();
            }
        } finally {
            Draw.SpriteBatch.End();
        }
    }

    private static void DrawTracks() {
        // 这一轮的最后 fade 帧：所有线转成淡灰（之后留在历史层），标记与幽灵淡出
        float toGray = fade > 0 && frame > frames - fade ? Math.Clamp((frame - (frames - fade)) / (float) fade, 0f, 1f) : 0f;
        float keep = 1f - toGray;
        // 先画淡色的，金色的一条最后画（压在上面）
        for (int pass = 0; pass < 2; pass++) {
            foreach (Track t in Tracks) {
                bool best = t.Kind == 3;
                if ((pass == 0) == best) continue;
                float l = (frame - t.Delay) * speed;
                if (l <= 0f) continue;
                float shown = Math.Min(l, t.Last);
                Color green = Color.Lerp(Green * (GreenAlpha * dim), t.Faded, toGray);
                // 走到头之后：死亡变红、金色变金，从终点往起点每个画面帧传导 recolor 帧
                float since = frame - t.FinishAt;
                float back = since > 0f && (best || t.Kind == 1) ? since * recolor : 0f;
                if (back <= 0f) {
                    DrawRange(t, 0f, shown, green);
                } else {
                    float split = Math.Max(0f, t.Last - back);
                    DrawRange(t, 0f, split, green);
                    Color done = best ? Gold : Red * (RedAlpha * dim);
                    DrawRange(t, split, t.Last, Color.Lerp(done, t.Faded, toGray));
                }
                if (keep <= 0.01f) continue;
                (Vector2 head, _) = Head(t, shown);
                if (l < t.Last) {
                    Draw.Point(head + Lift, Color.White * (0.7f * keep));
                } else if (t.Kind == 1) {
                    Vector2 e = head + Lift;
                    Color x = Red * (0.85f * keep);
                    Draw.Line(e + new Vector2(-2f, -2f), e + new Vector2(2f, 2f), x);
                    Draw.Line(e + new Vector2(-2f, 2f), e + new Vector2(2f, -2f), x);
                } else if (t.Kind == 2) {
                    Vector2 e = head + Lift;
                    Draw.Rect(e.X - 1f, e.Y - 1f, 2f, 2f, Green * (0.8f * keep));
                }
            }
        }
    }

    /// <summary>画路线在帧号 [from, to] 之间的一段（两端按帧号插值）。</summary>
    private static void DrawRange(Track t, float from, float to, Color color) {
        if (t.P.Length < 2 || to <= from) return;
        for (int i = 1; i < t.P.Length; i++) {
            float f0 = t.F[i - 1], f1 = t.F[i];
            if (f1 <= from) continue;
            if (f0 >= to) break;
            float span = Math.Max(1e-3f, f1 - f0);
            Vector2 p0 = f0 >= from ? t.P[i - 1] : Vector2.Lerp(t.P[i - 1], t.P[i], (from - f0) / span);
            Vector2 p1 = f1 <= to ? t.P[i] : Vector2.Lerp(t.P[i - 1], t.P[i], (to - f0) / span);
            Draw.Line(p0 + Lift, p1 + Lift, color);
        }
    }

    // ---- 解析 ----

    private static Track ReadTrack(JsonElement t, string name = "p") {
        var track = new Track();
        if (!t.TryGetProperty(name, out JsonElement p) || p.ValueKind != JsonValueKind.Array) return track;
        int n = p.GetArrayLength() / 3;
        track.F = new float[n];
        track.P = new Vector2[n];
        int i = 0, k = 0;
        float f = 0f, x = 0f;
        foreach (JsonElement v in p.EnumerateArray()) {
            float value = v.GetSingle();
            switch (k++ % 3) {
                case 0: f = value; break;
                case 1: x = value; break;
                default:
                    if (i < n) {
                        track.F[i] = f;
                        track.P[i] = new Vector2(x, value);
                        i++;
                    }
                    break;
            }
        }
        return track;
    }

    private static Vector2[] ReadPoints(JsonElement a, string name) {
        if (!a.TryGetProperty(name, out JsonElement p) || p.ValueKind != JsonValueKind.Array) return Array.Empty<Vector2>();
        var list = new List<Vector2>();
        float x = 0f;
        int k = 0;
        foreach (JsonElement v in p.EnumerateArray()) {
            if (k++ % 2 == 0) x = v.GetSingle();
            else list.Add(new Vector2(x, v.GetSingle()));
        }
        return list.ToArray();
    }

    private static int GetInt(JsonElement a, string name, int fallback) =>
        a.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.Number ? (int) Math.Round(v.GetDouble()) : fallback;

    private static float GetFloat(JsonElement a, string name, float fallback) =>
        a.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.Number ? v.GetSingle() : fallback;

    private static bool GetBool(JsonElement a, string name, bool fallback) =>
        a.TryGetProperty(name, out JsonElement v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False ? v.GetBoolean() : fallback;

    private static string GetString(JsonElement a, string name, string fallback) =>
        a.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? fallback : fallback;
}
