using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Celeste;
using Celeste.Mod;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 桥接的核心状态机，全部在游戏主线程上运行。
/// 未接管：游戏照常运行。接管后默认冻结在当前帧；收到 step 才按节拍逐帧推进，
/// 推进完再冻结并回报。加载场景不受冻结影响，会自动走完。
/// </summary>
internal static class Bridge {
    public const string Version = "0.8.1";
    private const int MaxPlanFrames = 60 * 60 * 10;

    public static bool Active { get; private set; }
    public static bool FeedInputThisFrame { get; private set; }
    public static InputFrame CurrentInput { get; private set; } = InputFrame.Neutral;

    /// <summary>接管以来推进过的总帧数（含加载帧）。</summary>
    public static long Frame { get; private set; }

    private static bool engineHalted = true;
    private static StepJob? job;
    private static readonly ConcurrentQueue<Request> Inbox = new();
    private static readonly List<RenderReply> PendingRenders = new();

    private sealed record RenderReply(long Id, Action<Utf8JsonWriter> Body, string Buffer);

    public static bool ShouldHaltEngineUpdate() => Active && engineHalted;

    public static void Enqueue(Request request) => Inbox.Enqueue(request);

    // ---- 每个节拍的入口 ----

    public static void Tick(Action runOriginal) {
        DrainInbox();
        FlushPendingAck();
        UpdateLoopMode();

        if (!Active) {
            runOriginal();
            return;
        }

        long before = Frame;
        if (lockstepEngaged) {
            LockstepTick(runOriginal);
        } else {
            NormalTick(runOriginal);
        }
        UpdateAudio(Frame != before);
    }

    // ---- 普通节拍：每个游戏循环推进 FramesPerTick 帧 ----

    private static void NormalTick(Action runOriginal) {
        if (job == null) {
            if (IsLoading()) {
                AdvanceFrame(runOriginal, InputFrame.Neutral);
            } else {
                Halt(runOriginal);
            }
            return;
        }

        int budget = Math.Clamp(job.FramesPerTick, 1, 600);
        for (int i = 0; i < budget && job != null; i++) {
            if (i > 0) {
                PollPlatformEventsSafe();
            }
            if (IsLoading()) {
                AdvanceFrame(runOriginal, InputFrame.Neutral);
                job.LoadingFrames++;
                continue;
            }
            AdvanceFrame(runOriginal, job.NextInput());
            job.AfterFrame(Frame);
            if (job.Finished) {
                FinishJob();
            }
        }
    }

    // ---- 锁步节拍：录制时由 OBS 的视频节拍驱动，每个令牌推进一帧 ----

    private static bool lockstepEngaged;
    private static long? pendingAck;
    private static readonly Stopwatch SinceAdvance = Stopwatch.StartNew();
    private static bool audioFrozen;

    /// <summary>录制校准用：在画面左下角画出帧号条码（只在校准时打开）。</summary>
    public static bool DrawFrameMarker;

    private static void LockstepTick(Action runOriginal) {
        bool wantsFrame = job != null || IsLoading();
        // 没有要推进的帧时也要及时回复令牌；等待令牌的同时限制空转的循环频率
        if (!ClockServer.TryTakeToken(wantsFrame ? 6 : 3, out long token)) {
            Halt(runOriginal);
            return;
        }
        if (!wantsFrame) {
            ClockServer.Send($"N {token}");
            Halt(runOriginal);
            return;
        }
        if (IsLoading()) {
            AdvanceFrame(runOriginal, InputFrame.Neutral);
            if (job != null) job.LoadingFrames++;
        } else {
            AdvanceFrame(runOriginal, job!.NextInput());
            job.AfterFrame(Frame);
            if (job.Finished) {
                FinishJob();
            }
        }
        pendingAck = token; // 这一帧画到屏幕上之后（EndDraw）再回复，OBS 下一次渲染就能采到它
    }

    /// <summary>游戏把这一帧呈现到屏幕之后调用（FNA Game.EndDraw）。</summary>
    public static void AfterPresent() => FlushPendingAck();

    private static void FlushPendingAck() {
        if (pendingAck is not { } token) return;
        pendingAck = null;
        long ticks = Engine.Scene is Level level ? level.Session.Time : 0;
        ClockServer.Send($"F {token} {Frame} {ticks}");
    }

    /// <summary>锁步时改成不限速的游戏循环（关垂直同步），由令牌决定推进；结束后恢复原设置。</summary>
    private static void UpdateLoopMode() {
        bool want = Active && ClockServer.Connected && ClockServer.LockstepRequested;
        if (want == lockstepEngaged) return;
        lockstepEngaged = want;
        try {
            Engine.Instance.IsFixedTimeStep = !want;
            Engine.Graphics.SynchronizeWithVerticalRetrace = !want && global::Celeste.Settings.Instance.VSync;
            Engine.Graphics.ApplyChanges();
        } catch (Exception ex) {
            Log.Warn($"switching game loop mode failed: {ex.Message}");
        }
        Log.Info($"lockstep {(want ? "engaged" : "released")}");
    }

    /// <summary>游戏冻结时声音也冻结：超过 60 毫秒没有推进就暂停 FMOD 主总线，推进时立即恢复。</summary>
    private static void UpdateAudio(bool advanced) {
        if (advanced) {
            SinceAdvance.Restart();
            if (audioFrozen) SetAudioFrozen(false);
        } else if (!audioFrozen && SinceAdvance.ElapsedMilliseconds > 60) {
            SetAudioFrozen(true);
        }
    }

    private static void SetAudioFrozen(bool frozen) {
        audioFrozen = frozen;
        try {
            Audio.BusPaused("bus:/", frozen);
        } catch (Exception ex) {
            Log.Warn($"audio freeze failed: {ex.Message}");
        }
    }

    /// <summary>绘制完成后调用：处理需要截图的回复。</summary>
    public static void AfterRender() {
        if (PendingRenders.Count == 0) {
            return;
        }
        foreach (RenderReply pending in PendingRenders) {
            string? image = FrameGrabber.CapturePngBase64(pending.Buffer);
            IpcServer.Send(Reply.Ok(pending.Id, w => {
                pending.Body(w);
                if (image != null) {
                    w.WriteString("imageFormat", "png-base64-320x180");
                    w.WriteString("image", image);
                } else {
                    w.WriteNull("image");
                }
            }));
        }
        PendingRenders.Clear();
    }

    public static void Deactivate() {
        if (job != null) {
            job.Cancel();
            FinishJob();
        }
        if (!Active) {
            return;
        }
        InputInjector.Uninstall();
        if (audioFrozen) SetAudioFrozen(false);
        DrawFrameMarker = false;
        Active = false;
        engineHalted = true;
        FeedInputThisFrame = false;
        CurrentInput = InputFrame.Neutral;
        Log.Info("bridge deactivated");
    }

    // ---- 帧推进 ----

    private static void Halt(Action runOriginal) {
        engineHalted = true;
        FeedInputThisFrame = false;
        runOriginal(); // Engine.Update 会在 MInput.Update 之后直接返回
    }

    private static void AdvanceFrame(Action runOriginal, InputFrame input) {
        engineHalted = false;
        FeedInputThisFrame = true;
        CurrentInput = input;
        try {
            runOriginal();
        } finally {
            engineHalted = true;
            FeedInputThisFrame = false;
            Frame++;
        }
    }

    private static void FinishJob() {
        StepJob finished = job!;
        job = null;
        byte[] result = Reply.Object(finished.WriteResult);
        byte[] obs = Reply.Object(w => StateExtractor.WriteSnapshot(w, finished.Snapshot));
        void Body(Utf8JsonWriter w) {
            w.WritePropertyName("result");
            w.WriteRawValue(result, skipInputValidation: true);
            w.WritePropertyName("obs");
            w.WriteRawValue(obs, skipInputValidation: true);
        }
        if (finished.WantImage) {
            PendingRenders.Add(new RenderReply(finished.RequestId, Body, finished.Buffer));
        } else {
            IpcServer.Send(Reply.Ok(finished.RequestId, Body));
        }
    }

    private static bool IsLoading() => Engine.Scene switch {
        Level level => level.Paused && UserIO.Saving,
        LevelLoader or LevelExit or GameLoader or OverworldLoader => true,
        _ => false,
    };

    // 一个节拍推进多帧时，手动处理窗口事件，避免窗口"未响应"（同 CelesteTAS）
    private static void PollPlatformEventsSafe() {
        try {
            PollPlatformEvents();
        } catch (Exception) {
            // FNA 版本不同导致签名不匹配时忽略
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void PollPlatformEvents() {
        Engine game = Engine.Instance;
        Microsoft.Xna.Framework.FNAPlatform.PollEvents(game, ref game.currentAdapter, game.textInputControlDown, ref game.textInputSuppress);
    }

    // ---- 指令处理 ----

    private static void DrainInbox() {
        while (Inbox.TryDequeue(out Request? request)) {
            try {
                Handle(request);
            } catch (Exception ex) {
                IpcServer.Send(Reply.Error(request.Id, $"{ex.GetType().Name}: {ex.Message}"));
            }
        }
    }

    private static void Handle(Request req) {
        JsonElement a = req.Args;
        switch (req.Cmd) {
            case "hello":
                IpcServer.Send(Reply.Ok(req.Id, w => {
                    w.WriteString("bridge", Version);
                    w.WriteString("everest", Everest.VersionString);
                    w.WriteBoolean("active", Active);
                    w.WriteNumber("frame", Frame);
                    w.WriteString("scene", Engine.Scene?.GetType().Name ?? "none");
                    w.WriteStartObject("clock");
                    w.WriteBoolean("connected", ClockServer.Connected);
                    w.WriteBoolean("lockstep", lockstepEngaged);
                    w.WritePropertyName("plugin");
                    if (ClockServer.PluginStatus is { } status && IsJson(status)) w.WriteRawValue(status, skipInputValidation: true);
                    else w.WriteNullValue();
                    w.WriteEndObject();
                }));
                break;

            case "record":
                // 转给 OBS 插件：{"action": "start" | "stop", "dir", "ffmpeg", "video_args", "audio_offset_ms"}
                if (!ClockServer.Connected) {
                    IpcServer.Send(Reply.Error(req.Id, "OBS plugin not connected to the clock port"));
                    break;
                }
                DrawFrameMarker = GetBool(a, "marker", DrawFrameMarker);
                ClockServer.Send("R " + a.GetRawText());
                IpcServer.Send(Reply.Ok(req.Id, w => w.WriteBoolean("marker", DrawFrameMarker)));
                break;

            case "activate":
                if (!Active) {
                    InputInjector.Install();
                    Active = true;
                    engineHalted = true;
                    Log.Info("bridge activated");
                }
                SendObservation(req.Id, a, defaultImage: false);
                break;

            case "deactivate":
                Deactivate();
                IpcServer.Send(Reply.Ok(req.Id));
                break;

            case "observe":
                SendObservation(req.Id, a, defaultImage: true);
                break;

            case "step":
                StartStep(req.Id, a);
                break;

            case "cancel":
                if (job != null) {
                    job.Cancel();
                    FinishJob();
                }
                IpcServer.Send(Reply.Ok(req.Id));
                break;

            case "skip_cutscene":
                // 仅供调试：直接调用跳过，不经过暂停菜单。主控与菜单宏改用暂停菜单跳过（与玩家操作一致）。
                bool skipped = false;
                if (Engine.Scene is Level level && level.InCutscene && !level.SkippingCutscene) {
                    level.SkipCutscene();
                    skipped = true;
                } else if (Engine.Scene is IntroVignette vignette && !vignette.exiting) {
                    // 序章开场的独立场景：等同于在它的暂停菜单里选"跳过"
                    vignette.StartGame();
                    skipped = true;
                }
                IpcServer.Send(Reply.Ok(req.Id, w => w.WriteBoolean("skipped", skipped)));
                break;

            default:
                IpcServer.Send(Reply.Error(req.Id, $"unknown cmd '{req.Cmd}'"));
                break;
        }
    }

    private static void SendObservation(long id, JsonElement a, bool defaultImage) {
        SnapshotOptions opt = ReadSnapshotOptions(a);
        bool image = GetBool(a, "image", defaultImage);
        string buffer = GetString(a, "buffer", "level");
        byte[] obs = Reply.Object(w => StateExtractor.WriteSnapshot(w, opt));
        void Body(Utf8JsonWriter w) {
            w.WriteBoolean("active", Active);
            w.WritePropertyName("obs");
            w.WriteRawValue(obs, skipInputValidation: true);
        }
        if (image) {
            PendingRenders.Add(new RenderReply(id, Body, buffer));
        } else {
            IpcServer.Send(Reply.Ok(id, Body));
        }
    }

    private static void StartStep(long id, JsonElement a) {
        if (!Active) {
            IpcServer.Send(Reply.Error(id, "bridge not active; send activate first"));
            return;
        }
        if (job != null) {
            IpcServer.Send(Reply.Error(id, "busy: a step is already running"));
            return;
        }
        if (!a.TryGetProperty("inputs", out JsonElement inputs)) {
            IpcServer.Send(Reply.Error(id, "missing 'inputs'"));
            return;
        }

        List<InputFrame> plan = StepJob.ParsePlan(inputs, MaxPlanFrames);
        List<StepJob.ExpectRow?>? expect = StepJob.ParseExpect(a, MaxPlanFrames);
        float tolerance = a.TryGetProperty("tolerance", out JsonElement tol) && tol.ValueKind == JsonValueKind.Number
            ? Math.Max(0f, tol.GetSingle()) : 1f;
        List<string> stopOn = a.TryGetProperty("stop_on", out JsonElement s) && s.ValueKind == JsonValueKind.Array
            ? s.EnumerateArray().Select(e => e.GetString() ?? "").Where(x => x.Length > 0).ToList()
            : new List<string>();

        job = new StepJob(
            id, plan, stopOn,
            framesPerTick: GetInt(a, "speed", 1),
            wantImage: GetBool(a, "image", true),
            buffer: GetString(a, "buffer", "level"),
            snapshot: ReadSnapshotOptions(a),
            recordTrajectory: GetBool(a, "trajectory", true),
            recordEntities: GetBool(a, "entityTrajectory", false),
            expect: expect,
            tolerance: tolerance,
            progressEvery: GetInt(a, "progress", 0));

        if (job.Finished) {
            FinishJob(); // 空计划：直接回报当前帧
        }
    }

    private static SnapshotOptions ReadSnapshotOptions(JsonElement a) => new() {
        Entities = GetBool(a, "entities", true),
        Tiles = GetBool(a, "tiles", true),
        Margin = GetInt(a, "margin", 16),
        Room = GetString(a, "scope", "view") == "room",
        Fields = GetBool(a, "fields", false),
    };

    private static bool IsJson(string text) {
        try {
            using JsonDocument _ = JsonDocument.Parse(text);
            return true;
        } catch (JsonException) {
            return false;
        }
    }

    private static bool GetBool(JsonElement a, string name, bool fallback) =>
        a.ValueKind == JsonValueKind.Object && a.TryGetProperty(name, out JsonElement v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? v.GetBoolean() : fallback;

    private static int GetInt(JsonElement a, string name, int fallback) =>
        a.ValueKind == JsonValueKind.Object && a.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt32() : fallback;

    private static string GetString(JsonElement a, string name, string fallback) =>
        a.ValueKind == JsonValueKind.Object && a.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? fallback : fallback;
}
