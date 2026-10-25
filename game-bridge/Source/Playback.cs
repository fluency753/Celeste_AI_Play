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
/// - 一轮的路线按帧号逐渐延伸（每个画面帧推进 speed 帧游戏时间），普通路线绿色，最佳路线亮薄荷色加深色描边；
///   一条线走到头的那一帧定下结果：死亡的从死亡点往起点一路变红，这一轮最快的一条从终点往起点一路变成实心金色，
///   变色每个画面帧传导 recolor 帧；其余的（出口、按键用完）保持绿色。只画线和前端标记，不画角色；
/// - 一轮放完保留彩色；开始新一轮时旧路线转为淡灰并衰减，过淡的线清掉；真实执行开始清空全部历史；
/// - 镜头临时借用：渲染前把关卡镜头换成回放镜头（跟着金色路线的前端平滑移动、受房间边界限制），渲染后原样换回，
///   连同 Level.cameraPreShake；渲染期间随机数换成单独的发生器（Calc.PushRandom），游戏状态一点不变。
///   两轮之间（主控等智能体规划下一轮时）画面停在回放镜头上，历史线照常画；
/// - 执行前的最后一段（final）：不画新的线，镜头用 return 帧平滑回到 Madeline（镜头本来就在那里时直接结束）；
///   真实执行开始时清掉全部线条；
/// - 观测截图的那一帧不换镜头、不画回放，智能体看到的永远是游戏本来的画面；
/// - 刚进章节时圆圈转场（Level.Wipe）还没放完：回放帧先把转场放完（只是画面效果：转场只影响能不能暂停），
///   黑屏消失之后才开始画线；
/// - 回放帧里音乐照常播放、音效暂停（见 Bridge.UpdateAudio）。
/// 录制时每个回放帧照常回复 OBS 插件，章节计时记为 -1。
///
/// 画得快：一轮常有两三千条线，逐条逐段每帧重画一遍要 20 ms 以上，跟不上 60 帧（录像里回放时的音乐会被跳掉一截）。
/// 所以不再变化的线画进缓存层（世界坐标的渲染目标，覆盖整个房间，渲染开始前更新）：
/// - 历史层：前几轮的淡灰线，每轮只合进来一次；
/// - 本轮层：其他与出口路线走过的每一段（一段只画一次），死亡路线变完红之后整条画进来；
/// - 衰减层：路线密集时与本轮层交替，只在绘制新线段时衰减旧线段；最佳路线单独绘制，保持完整清晰。
/// 每帧只重画还在延伸的那一小段、还没变完色的死亡路线和金色路线。每段线按预先算好的方向与长度画，不再每帧算三角函数。
/// 房间大到建不了缓存层（超过 4096 像素）时退回逐条重画。
/// </summary>
internal static class Playback {
    private sealed class Track {
        public float[] F = Array.Empty<float>();       // 每个点的帧号（共线的点已合并）
        public Vector2[] P = Array.Empty<Vector2>();
        public float[] Ang = Array.Empty<float>();     // 第 i 段（P[i-1] → P[i]）的方向
        public float[] Len = Array.Empty<float>();     // 第 i 段的长度
        public int Baked;                              // 已经画进本轮层的点（其他与出口路线：第 1～Baked 段）
        public bool Settled;                           // 死亡路线变完红之后整条画进了本轮层
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
    private const float GreenAlpha = 0.65f, RedAlpha = 0.65f, GrayAlpha = 0.045f;
    private const float ActiveWidth = 1.5f, BestWidth = 2f, HistoryWidth = 0.75f;
    private static PlaybackDensity density = new(0);
    private static PlaybackTrailClock trailClock = new(new());
    private static readonly Color GrowingBest = new(0xA0, 0xFF, 0xE0);
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
    private static int frames, returnFrames, frame;
    private static Vector2[] focus = Array.Empty<Vector2>();
    private static int focusEvery = 4;
    private static Vector2 cam, realCam, returnFrom;
    private static bool camValid, returnStarted;
    private static Vector2 savedCam, savedPreShake;
    private static bool swapped;

    // 缓存层（见文件头）
    private const int LayerMargin = 16, MaxLayerSize = 4096;
    private static RenderTarget2D? historyRt, roundRt, decayRt;
    private static int roundLayerFrame;
    private static Rectangle area;                         // 缓存层覆盖的世界范围（房间范围外加一圈）
    private static bool layersReady;                       // 这一帧的缓存层已经更新好（渲染开始前更新，渲染完复位）
    private static bool layersBroken;                      // 建渲染目标失败过：以后一直逐条重画
    private static int historyDrawn;                       // History 里已经画进历史层的条数
    private static bool historyClear = true, roundClear = true;
    // 渲染用时（回放帧整帧渲染，含游戏本身），回复主控时报告
    private static readonly System.Diagnostics.Stopwatch RenderWatch = new();
    private static double renderTotalMs, renderMaxMs;
    private static int renderFrames;
    private static bool timing;

    public static bool Running => running;
    public static long StepCount;                          // 画过的回放帧数（桥接据此在回放时放音乐）
    private static int wipeTicks;
    public static bool Done => running && frame >= frames + returnFrames;

    // ---- 命令 ----

    public static string? Start(long id, JsonElement a, Level level) {
        if (running) return "a playback is already running";
        var incoming = new List<Track>();
        if (a.TryGetProperty("tracks", out JsonElement ts) && ts.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement t in ts.EnumerateArray()) {
                Track track = ReadTrack(t);
                if (track.P.Length == 0) continue;
                track.Kind = GetInt(t, "k", 0);
                track.Delay = GetInt(t, "d", 0);
                incoming.Add(track);
            }
        }
        if (!GetBool(a, "final", false) && incoming.Count == 0) return "playback needs at least one nonempty track";
        final = GetBool(a, "final", false);
        speed = Math.Clamp(GetFloat(a, "speed", 2f), 0.1f, 60f);
        frames = Math.Clamp(GetInt(a, "frames", 60), final ? 0 : 1, 60 * 120);
        returnFrames = Math.Clamp(GetInt(a, "return", 0), 0, 600);
        dim = Math.Clamp(GetFloat(a, "dim", 1f), 0.05f, 1f);
        recolor = Math.Clamp(GetFloat(a, "recolor", 4f), 0.1f, 240f);
        focusEvery = Math.Max(1, GetInt(a, "focus_every", 4));
        focus = ReadPoints(a, "focus");
        PlaybackSpinners.Clear();
        if (!final) History.AddRange(Tracks);
        Tracks.Clear();
        Tracks.AddRange(incoming);
        var spans = new List<(float Start, float End)>();
        foreach (Track track in Tracks)
            for (int i = 1; i < track.P.Length; i++)
                if (track.Len[i] > 0)
                    spans.Add((track.Delay + track.F[i-1]/speed,track.Delay + track.F[i]/speed));
        trailClock = new PlaybackTrailClock(spans);
        if (!final) {
            density = new PlaybackDensity(Tracks.Count);
            dim = Math.Min(dim, density.Dim);
            foreach (Track track in Tracks) track.Faded = Gray * (GrayAlpha * dim);
            foreach (Track track in History) track.Faded *= density.HistoryRetention;
            History.RemoveAll(track => track.Faded.A <= 1);
            historyDrawn = 0;
            historyClear = true;
        }
        roundLayerFrame = 0;
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
        roundClear = true;
        renderTotalMs = renderMaxMs = 0;
        renderFrames = 0;
        running = true;
        return null;
    }

    /// <summary>回放结束后保留最后一帧彩色路线，下一轮开始时才移入历史层。</summary>
    public static void Finish() {
        if (!running) return;
        running = false;
        if (returnFrames > 0) cam = realCam;
        int drawn = frame;
        double avg = renderFrames > 0 ? renderTotalMs / renderFrames : 0, max = renderMaxMs;
        IpcServer.Send(Reply.Ok(requestId, w => {
            w.WriteNumber("frames", drawn);
            w.WriteNumber("render_avg_ms", Math.Round(avg, 2));
            w.WriteNumber("render_max_ms", Math.Round(max, 2));
            w.WriteNumber("dim", dim);
            w.WriteNumber("fade_frames", 0);
            w.WriteNumber("spinner_visual_peak", PlaybackSpinners.PeakCount);
            if (float.IsFinite(density.HalfLife)) w.WriteNumber("trail_half_life_frames", density.HalfLife);
        }));
    }

    /// <summary>真实执行开始：清掉历史线，镜头还给游戏（金色参考线留到这次执行结束）。</summary>
    public static void EndOverlay() {
        if (running) return;
        overlay = false;
        camValid = false;
        PlaybackSpinners.Clear();
        History.Clear();
        Tracks.Clear();
        historyDrawn = 0;
        historyClear = true;
    }

    public static void Reset() {
        running = false;
        Tracks.Clear();
        EndOverlay();
        DisposeLayers();
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
    internal static bool ShowSpinnerVisuals => Showing && !(running && Engine.Scene is Level { Wipe.WipeIn: true });

    /// <summary>Celeste.RenderCore 之前：换上回放镜头与单独的随机数（回放进行中与两轮之间；观测截图的那一帧除外）。</summary>
    public static void BeforeRender() {
        if (!Showing || Engine.Scene is not Level level) return;
        timing = running;
        if (timing) RenderWatch.Restart();
        layersReady = UpdateLayers(level);
        savedCam = level.Camera.Position;
        savedPreShake = level.cameraPreShake;
        level.Camera.Position = cam;
        Calc.PushRandom(7919 + frame);
        swapped = true;
    }

    /// <summary>Celeste.RenderCore 之后：原样换回镜头与随机数。</summary>
    public static void AfterRender() {
        layersReady = false;
        if (timing) {
            timing = false;
            double ms = RenderWatch.Elapsed.TotalMilliseconds;
            renderTotalMs += ms;
            renderFrames++;
            if (ms > renderMaxMs) renderMaxMs = ms;
        }
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
                if (layersReady && historyDrawn == History.Count) {
                    if (History.Count > 0) Draw.SpriteBatch.Draw(historyRt, new Vector2(area.X, area.Y), Color.White);
                } else {
                    foreach (Track t in History) DrawRange(t, 0f, t.Last, t.Faded, width: HistoryWidth);
                }
            }
            if (Tracks.Count > 0) {
                DrawTracks();
            }
        } finally {
            Draw.SpriteBatch.End();
        }
    }

    private static void DrawTracks() {
        // 轮末及空闲时保留最后的颜色，只有下一轮开始才把本轮转成历史线。
        bool layers = layersReady;
        if (layers) {
            // 缓存保留最后一帧；空闲期间不重复衰减。
            Draw.SpriteBatch.Draw(roundRt, new Vector2(area.X, area.Y), Color.White);
        }
        // 先画淡色的，金色的一条最后画（压在上面）
        for (int pass = 0; pass < 2; pass++) {
            foreach (Track t in Tracks) {
                bool best = t.Kind == 3;
                if ((pass == 0) == best) continue;
                float l = (frame - t.Delay) * speed;
                if (l <= 0f) continue;
                float shown = Math.Min(l, t.Last);
                bool ageTrail = !best && (t.Kind is 0 or 2 || (frame - t.FinishAt) * recolor >= t.Last);
                Color green = best ? GrowingBest : Green * (GreenAlpha * dim);
                float width = best ? BestWidth : ActiveWidth;
                if (layers && t.Kind is 0 or 2) {
                    // 画进本轮层的段之后：还在延伸的那一小段
                    DrawRange(t, t.F[t.Baked], shown, green, t.Baked + 1);
                } else if (!(layers && t.Settled)) {
                    // 走到头之后：死亡变红、金色变金，从终点往起点每个画面帧传导 recolor 帧
                    float since = frame - t.FinishAt;
                    float back = since > 0f && (best || t.Kind == 1) ? since * recolor : 0f;
                    if (back <= 0f) {
                        DrawRange(t, 0f, shown, green, width: width, outline: best ? 1f : 0f, ageTrail: ageTrail);
                    } else {
                        float split = Math.Max(0f, t.Last - back);
                        DrawRange(t, 0f, split, green, width: width, outline: best ? 1f : 0f, ageTrail: ageTrail);
                        Color done = best ? Gold : Red * (RedAlpha * dim);
                        DrawRange(t, split, t.Last, done, width: width, outline: best ? 1f : 0f, ageTrail: ageTrail);
                    }
                }
                (Vector2 head, _) = Head(t, shown);
                if (l < t.Last) {
                    Vector2 e = head + Lift;
                    if (best) Draw.Rect(e.X - 2f, e.Y - 2f, 4f, 4f, Color.Black * 0.8f);
                    Draw.Rect(e.X - 1f, e.Y - 1f, 2f, 2f, Color.White * (best ? 1f : dim));
                } else if (t.Kind == 1) {
                    Vector2 e = head + Lift;
                    Color x = Red * (0.85f * dim * density.Visibility(trailClock.Age(t.FinishAt,frame)));
                    Draw.Line(e + new Vector2(-2f, -2f), e + new Vector2(2f, 2f), x);
                    Draw.Line(e + new Vector2(-2f, 2f), e + new Vector2(2f, -2f), x);
                } else if (t.Kind == 2) {
                    Vector2 e = head + Lift;
                    Draw.Rect(e.X - 1f, e.Y - 1f, 2f, 2f, Green * (0.8f * dim * density.Visibility(trailClock.Age(t.FinishAt,frame))));
                }
            }
        }
    }

    /// <summary>画路线在帧号 [from, to] 之间的一段（两端按帧号插值），从第 start 段开始找。</summary>
    private static void DrawRange(Track t, float from, float to, Color color, int start = 1,
                                  float width = ActiveWidth, float outline = 0f, bool ageTrail = false) {
        if (t.P.Length < 2 || to <= from) return;
        for (int i = Math.Max(1, start); i < t.P.Length; i++) {
            float f0 = t.F[i - 1], f1 = t.F[i];
            if (f1 <= from) continue;
            if (f0 >= to) break;
            float len = t.Len[i];
            if (len <= 0f) continue;
            float span = Math.Max(1e-3f, f1 - f0);
            float u0 = f0 >= from ? 0f : (from - f0) / span;
            float u1 = f1 <= to ? 1f : (to - f0) / span;
            if (u1 <= u0) continue;
            Vector2 p0 = u0 <= 0f ? t.P[i - 1] : Vector2.Lerp(t.P[i - 1], t.P[i], u0);
            if (outline > 0f)
                Draw.LineAngle(p0 + Lift, t.Ang[i], len * (u1 - u0), Color.Black * (0.8f * outline), width + 1.5f);
            float paintedAt = t.Kind == 1 ? t.FinishAt + t.Last / recolor : t.Delay + Math.Min(f1, to) / speed;
            float visibility = ageTrail ? density.Visibility(trailClock.Age(paintedAt,frame)) : 1f;
            Color ink = color * visibility;
            Draw.LineAngle(p0 + Lift, t.Ang[i], len * (u1 - u0), ink, width);
        }
    }

    // ---- 缓存层 ----

    /// <summary>渲染开始前（还没有绑定任何渲染目标）更新缓存层；返回这一帧能不能用缓存层。</summary>
    private static bool UpdateLayers(Level level) {
        if (!EnsureLayers(level)) return false;
        GraphicsDevice device = Engine.Instance.GraphicsDevice;
        bool bound = false;
        try {
            // 新一轮开始后才把上一轮以淡灰历史线重画；空闲时缓存保持不变。
            bool historyWork = historyClear || historyDrawn < History.Count;
            if (historyWork) {
                BeginLayer(device, historyRt!, historyClear);
                bound = true;
                historyClear = false;
                for (; historyDrawn < History.Count; historyDrawn++) {
                    Track t = History[historyDrawn];
                    DrawRange(t, 0f, t.Last, t.Faded, width: HistoryWidth);
                }
                Draw.SpriteBatch.End();
            }
            if (!running && !roundClear) return true;
            // 衰减缓存而不是逐帧重画全部路线；同一回放帧重复渲染不重复衰减。
            if (!roundClear && frame > roundLayerFrame && float.IsFinite(density.HalfLife)) {
                BeginLayer(device, decayRt!, true);
                bound = true;
                Draw.SpriteBatch.Draw(roundRt, new Vector2(area.X, area.Y), Color.White * density.Visibility(trailClock.Age(roundLayerFrame,frame)));
                Draw.SpriteBatch.End();
                (roundRt, decayRt) = (decayRt, roundRt);
            }
            // 本轮层：其他与出口路线走过的每一段画一次；死亡路线变完红后整条画一次
            BeginLayer(device, roundRt!, roundClear);
            bound = true;
            if (roundClear) {
                roundClear = false;
                foreach (Track t in Tracks) {
                    t.Baked = 0;
                    t.Settled = false;
                }
            }
            Color green = Green * (GreenAlpha * dim), red = Red * (RedAlpha * dim);
            foreach (Track t in Tracks) {
                float l = (frame - t.Delay) * speed;
                if (l <= 0f || t.P.Length < 2) continue;
                if (t.Kind is 0 or 2) {
                    int from = t.Baked;
                    while (t.Baked + 1 < t.P.Length && t.F[t.Baked + 1] <= l) t.Baked++;
                    if (t.Baked > from) DrawRange(t, t.F[from], t.F[t.Baked], green, from + 1, ageTrail: true);
                } else if (t.Kind == 1 && !t.Settled && (frame - t.FinishAt) * recolor >= t.Last) {
                    DrawRange(t, 0f, t.Last, red, ageTrail: true);
                    t.Settled = true;
                }
            }
            Draw.SpriteBatch.End();
            roundLayerFrame = frame;
            return true;
        } catch (Exception e) {
            Log.Info($"playback layers failed, falling back to direct drawing: {e.Message}");
            layersBroken = true;
            DisposeLayers();
            return false;
        } finally {
            if (bound) device.SetRenderTarget(null);
        }
    }

    private static void BeginLayer(GraphicsDevice device, RenderTarget2D target, bool clear) {
        device.SetRenderTarget(target);
        if (clear) device.Clear(Color.Transparent);
        Draw.SpriteBatch.Begin(SpriteSortMode.Deferred, BlendState.AlphaBlend, SamplerState.PointClamp,
            DepthStencilState.None, RasterizerState.CullNone, null, Matrix.CreateTranslation(-area.X, -area.Y, 0f));
    }

    /// <summary>缓存层覆盖当前房间；房间变了就重建（历史线与本轮已画的部分重新画进去）。</summary>
    private static bool EnsureLayers(Level level) {
        if (layersBroken) return false;
        Rectangle want = level.Bounds;
        want.Inflate(LayerMargin, LayerMargin);
        if (want.Width <= 0 || want.Height <= 0 || want.Width > MaxLayerSize || want.Height > MaxLayerSize) {
            DisposeLayers();
            return false;
        }
        if (historyRt is { IsDisposed: false } && roundRt is { IsDisposed: false }
            && decayRt is { IsDisposed: false } && want == area)
            return true;
        DisposeLayers();
        area = want;
        try {
            GraphicsDevice device = Engine.Instance.GraphicsDevice;
            historyRt = NewLayer(device);
            roundRt = NewLayer(device);
            decayRt = NewLayer(device);
        } catch (Exception e) {
            Log.Info($"playback layers unavailable, drawing directly: {e.Message}");
            layersBroken = true;
            DisposeLayers();
            return false;
        }
        historyDrawn = 0;
        historyClear = true;
        roundClear = true;
        return true;
    }

    private static RenderTarget2D NewLayer(GraphicsDevice device) =>
        new(device, area.Width, area.Height, false, SurfaceFormat.Color, DepthFormat.None, 0, RenderTargetUsage.PreserveContents);

    private static void DisposeLayers() {
        historyRt?.Dispose();
        roundRt?.Dispose();
        decayRt?.Dispose();
        historyRt = roundRt = decayRt = null;
        historyDrawn = 0;
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
        track.Ang = new float[n];
        track.Len = new float[n];
        for (int j = 1; j < n; j++) {
            Vector2 d = track.P[j] - track.P[j - 1];
            track.Len[j] = d.Length();
            track.Ang[j] = (float) Math.Atan2(d.Y, d.X);
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
