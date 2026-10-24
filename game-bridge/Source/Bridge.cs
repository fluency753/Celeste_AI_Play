using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Threading;
using Celeste;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Celeste.Mod;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 桥接的核心状态机，全部在游戏主线程上运行。
/// 未接管：游戏照常运行。接管后默认冻结在当前帧；收到 step 才按节拍逐帧推进，
/// 推进完再冻结并回报。加载场景不受冻结影响，会自动走完。
/// </summary>
internal static class Bridge {
    public const string Version = "0.11.1";
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
        // 规划回放的最后一帧已经在上一个节拍画出来了：回复主控
        if (Playback.Done) {
            Playback.Finish();
        }

        long before = Frame;
        long replayBefore = Playback.StepCount;
        if (lockstepEngaged) {
            LockstepTick(runOriginal);
        } else {
            NormalTick(runOriginal);
        }
        UpdateAudio(Frame != before, Playback.StepCount != replayBefore);
    }

    // ---- 普通节拍：每个游戏循环推进 FramesPerTick 帧 ----

    private static void NormalTick(Action runOriginal) {
        if (job == null) {
            if (IsLoading()) {
                AdvanceFrame(runOriginal, InputFrame.Neutral);
            } else {
                Halt(runOriginal);
                // 规划回放：游戏不推进，这个节拍照常重画（回放镜头与路线在渲染钩子里画）
                if (Playback.Running && Engine.Scene is Level level) {
                    Playback.Step(level);
                }
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
    private static bool pendingAckReplay;
    private static readonly Stopwatch SinceAdvance = Stopwatch.StartNew();
    private enum AudioMode { Playing, Frozen, MusicOnly }
    private static AudioMode audioMode = AudioMode.Playing;
    private static bool sfxPausedByUs;
    // 规划回放时只放音乐：音效、界面音效与音乐里的短促音效（stings）暂停
    private static readonly string[] SfxBuses = { "bus:/gameplay_sfx", "bus:/ui_sfx", "bus:/music/stings" };

    /// <summary>录制校准用：在画面左下角画出帧号条码（只在校准时打开）。</summary>
    public static bool DrawFrameMarker;

    private static void LockstepTick(Action runOriginal) {
        bool wantsFrame = job != null || IsLoading() || Playback.Running;
        // 没有要推进的帧时也要及时回复令牌；等待令牌的同时限制空转的循环频率
        if (!ClockServer.TryTakeToken(wantsFrame ? 6 : 3, out long token)) {
            HaltWithoutDrawing(runOriginal);
            return;
        }
        if (!wantsFrame) {
            ClockServer.Send($"N {token}");
            HaltWithoutDrawing(runOriginal);
            return;
        }
        // OBS 的游戏采集钩子最多每 1/120 秒拷贝一次画面，离上一次呈现太近的那次呈现不会被拷贝：
        // 停住时不重画（见 HaltWithoutDrawing），推进前再确认离上一次呈现已超过这个间隔
        WaitSincePresent(MinPresentGapMs);
        if (job == null && Playback.Running && !IsLoading()) {
            // 规划回放的一帧：不推进游戏，只重画；回复里章节计时记为 -1，录像对账时与游玩帧分开
            Halt(runOriginal);
            if (Engine.Scene is Level level) Playback.Step(level);
            pendingAck = token;
            pendingAckReplay = true;
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
    public static void AfterPresent() {
        if (pendingAck != null) {
            WaitForGpu();
        }
        FlushPendingAck();
        SincePresent.Restart();
    }

    private static RenderTarget2D? syncTarget;
    private static readonly Color[] SyncPixel = new Color[1];
    private static bool gpuSyncFailed;

    /// <summary>
    /// 等显卡把这一帧（以及 OBS 游戏采集钩子在 Present 里做的拷贝）真正做完再回复，否则 OBS 偶尔采到上一帧
    /// （实测约 2%）。做法是读回一个 1×1 渲染目标：读回要等同一设备上之前的命令全部执行完。
    /// 不能读后备缓冲：FNA3D 读后备缓冲时每次新建一块整屏大小的暂存纹理且不释放（每帧泄漏约 8 MB）；
    /// 渲染目标的暂存纹理会被缓存复用，没有泄漏。
    /// </summary>
    private static void WaitForGpu() {
        if (gpuSyncFailed) return;
        try {
            syncTarget ??= new RenderTarget2D(Engine.Instance.GraphicsDevice, 1, 1);
            syncTarget.GetData(SyncPixel);
        } catch (Exception ex) {
            gpuSyncFailed = true;
            Log.Warn($"GPU sync for lockstep failed, OBS may capture the previous frame: {ex.Message}");
        }
    }

    private const double MinPresentGapMs = 9.0;
    private static readonly Stopwatch SincePresent = Stopwatch.StartNew();

    private static void WaitSincePresent(double ms) {
        // Thread.Sleep 的精度不够（可能睡 15 毫秒），这里只差几毫秒，直接空转等待
        while (SincePresent.Elapsed.TotalMilliseconds < ms) {
            Thread.SpinWait(200);
        }
    }

    /// <summary>锁步时停住的节拍不重画：画面保持上一帧，也不占用 OBS 采集钩子的拷贝间隔。有截图请求时照常画。</summary>
    private static void HaltWithoutDrawing(Action runOriginal) {
        Halt(runOriginal);
        if (PendingRenders.Count == 0) {
            Engine.Instance.SuppressDraw();
        }
    }

    private static void FlushPendingAck() {
        if (pendingAck is not { } token) return;
        pendingAck = null;
        long ticks = pendingAckReplay ? -1 : Engine.Scene is Level level ? level.Session.Time : 0;
        pendingAckReplay = false;
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

    /// <summary>游戏冻结时声音也冻结：超过 60 毫秒没有推进就暂停 FMOD 主总线，推进时立即恢复。
    /// 规划回放的帧里音乐照常播放（音效暂停），录像里的音乐与之后真实执行时的音乐连在一起。</summary>
    private static void UpdateAudio(bool advanced, bool replayed) {
        if (advanced) {
            SinceAdvance.Restart();
            SetAudio(AudioMode.Playing);
        } else if (replayed) {
            SinceAdvance.Restart();
            SetAudio(AudioMode.MusicOnly);
        } else if (audioMode != AudioMode.Frozen && SinceAdvance.ElapsedMilliseconds > 60) {
            SetAudio(AudioMode.Frozen);
        }
    }

    private static void SetAudio(AudioMode mode) {
        if (mode == audioMode) return;
        try {
            switch (mode) {
                case AudioMode.Playing:
                    // 先在主总线还停着时恢复音效总线，再放开主总线，免得音效先冒出来
                    if (sfxPausedByUs) {
                        foreach (string bus in SfxBuses) Audio.BusPaused(bus, false);
                        sfxPausedByUs = false;
                    }
                    Audio.BusPaused("bus:/", false);
                    break;
                case AudioMode.MusicOnly:
                    if (!sfxPausedByUs) {
                        foreach (string bus in SfxBuses) Audio.BusPaused(bus, true);
                        sfxPausedByUs = true;
                    }
                    Audio.BusPaused("bus:/", false);
                    break;
                case AudioMode.Frozen:
                    Audio.BusPaused("bus:/", true);
                    break;
            }
        } catch (Exception ex) {
            Log.Warn($"audio mode {mode} failed: {ex.Message}");
        }
        audioMode = mode;
    }

    /// <summary>这一帧画完要截图回复（观测）：规划回放这一帧不换镜头、不画线。</summary>
    public static bool CapturePending => PendingRenders.Count > 0;

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
        Playback.Reset();
        SetAudio(AudioMode.Playing);
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

            case "playback":
                // 规划回放：游戏不推进，按主控给的路线重画若干帧，画完再回复（见 Playback）
                if (!Active || job != null || Engine.Scene is not Level pbLevel) {
                    IpcServer.Send(Reply.Error(req.Id, "playback needs an active bridge, a level and no running step"));
                    break;
                }
                if (pbLevel.Transitioning) {
                    // 换房间的转场还没结束（不应出现：只在 Madeline 可操作时回放）：这次不画
                    IpcServer.Send(Reply.Ok(req.Id, w => { w.WriteNumber("frames", 0); w.WriteString("skipped", "transition"); }));
                    break;
                }
                if (Playback.Start(req.Id, a, pbLevel) is { } pbError) {
                    IpcServer.Send(Reply.Error(req.Id, pbError));
                }
                break;

            case "playback_clear":
                Playback.EndOverlay();
                IpcServer.Send(Reply.Ok(req.Id));
                break;

            case "cancel":
                if (job != null) {
                    job.Cancel();
                    FinishJob();
                }
                IpcServer.Send(Reply.Ok(req.Id));
                break;

            case "sim_assets":
                // 预置模拟器的资源表（贴图尺寸与精灵动画；只在本机使用，见 SimState.WriteAssets）
                byte[] assets = Reply.Object(SimState.WriteAssets);
                IpcServer.Send(Reply.Ok(req.Id, w => {
                    w.WritePropertyName("assets");
                    w.WriteRawValue(assets, skipInputValidation: true);
                }));
                break;
            case "sim_state":
                // 预置模拟器的起始状态：当前帧的完整场景（见 SimState）
                if (Engine.Scene is not Level simLevel) {
                    IpcServer.Send(Reply.Error(req.Id, "not in a level"));
                    break;
                }
                byte[] state = Reply.Object(w => SimState.Write(w, simLevel, GetInt(a, "margin", 4)));
                IpcServer.Send(Reply.Ok(req.Id, w => {
                    w.WritePropertyName("state");
                    w.WriteRawValue(state, skipInputValidation: true);
                }));
                break;

            case "teleport":
                // 仅供模拟器校准：在 test 存档里把 Madeline 放到指定位置附近的空位（不与实心物体重叠）
                Teleport(req.Id, a);
                break;

            case "load_room":
                // 仅供模拟器校准：在 test 存档里直接载入同一章节的某个房间（玩家重生在房间的出生点）
                LoadRoom(req.Id, a);
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

    private static void LoadRoom(long id, JsonElement a) {
        if (!Active) {
            IpcServer.Send(Reply.Error(id, "bridge not active; send activate first"));
            return;
        }
        if (SaveData.Instance?.Name != "test") {
            IpcServer.Send(Reply.Error(id, "load_room is only allowed on the 'test' save"));
            return;
        }
        if (Engine.Scene is not Level level || job != null) {
            IpcServer.Send(Reply.Error(id, "load_room needs a level scene and no running step"));
            return;
        }
        string room = GetString(a, "room", "");
        AreaKey area = level.Session.Area;
        // 可指定章节（编号，A 面）：校准时直接进入尚未解锁的章节
        if (a.TryGetProperty("chapter", out JsonElement ch) && ch.ValueKind == JsonValueKind.Number) {
            area = new AreaKey(ch.GetInt32(), AreaMode.Normal);
        }
        MapData? map = AreaData.Get(area)?.Mode[(int) area.Mode]?.MapData;
        if (map == null || map.Get(room) == null) {
            IpcServer.Send(Reply.Error(id, $"no room '{room}' in chapter {area.ID}"));
            return;
        }
        var session = new Session(area) {
            Level = room,
            FirstLevel = false,
            StartedFromBeginning = false,
        };
        session.Inventory = area.Equals(level.Session.Area) ? level.Session.Inventory : AreaData.Get(area).Mode[(int) area.Mode].Inventory;
        // 校准用：可以预先设定会话标记（例如 2A 的追逐要求先经过房间 3）与能力（梦境冲刺、冲刺次数）
        if (a.TryGetProperty("levelFlags", out JsonElement lf) && lf.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement f in lf.EnumerateArray()) session.LevelFlags.Add(f.GetString() ?? "");
        }
        if (a.TryGetProperty("flags", out JsonElement fl) && fl.ValueKind == JsonValueKind.Array) {
            foreach (JsonElement f in fl.EnumerateArray()) session.SetFlag(f.GetString() ?? "");
        }
        if (a.TryGetProperty("dreamDash", out JsonElement dd) && dd.ValueKind is JsonValueKind.True or JsonValueKind.False) {
            session.Inventory.DreamDash = dd.GetBoolean();
        }
        if (a.TryGetProperty("dashes", out JsonElement da) && da.ValueKind == JsonValueKind.Number) {
            session.Inventory.Dashes = da.GetInt32();
        }
        Vector2? spawn = null;
        if (a.TryGetProperty("spawn", out JsonElement sp) && sp.ValueKind == JsonValueKind.Array && sp.GetArrayLength() == 2) {
            spawn = new Vector2(sp[0].GetSingle(), sp[1].GetSingle());
        }
        Engine.Scene = new LevelLoader(session, spawn);
        IpcServer.Send(Reply.Ok(id, w => w.WriteString("room", room)));
    }

    private static void Teleport(long id, JsonElement a) {
        if (!Active || job != null) {
            IpcServer.Send(Reply.Error(id, "teleport needs an active bridge and no running step"));
            return;
        }
        if (SaveData.Instance?.Name != "test") {
            IpcServer.Send(Reply.Error(id, "teleport is only allowed on the 'test' save"));
            return;
        }
        if (Engine.Scene is not Level level || level.Tracker.GetEntity<Player>() is not { } player) {
            IpcServer.Send(Reply.Error(id, "no player"));
            return;
        }
        if (!a.TryGetProperty("x", out JsonElement xe) || !a.TryGetProperty("y", out JsonElement ye)) {
            IpcServer.Send(Reply.Error(id, "missing x/y"));
            return;
        }
        var target = new Vector2(MathF.Round(xe.GetSingle()), MathF.Round(ye.GetSingle()));
        Vector2 original = player.Position;
        Vector2? found = null;
        // 由近到远找一个不与实心物体重叠、且在房间内的位置
        for (int r = 0; r <= 48 && found == null; r++) {
            for (int dy = -r; dy <= r && found == null; dy++) {
                for (int dx = -r; dx <= r && found == null; dx++) {
                    if (Math.Max(Math.Abs(dx), Math.Abs(dy)) != r) continue;
                    Vector2 p = target + new Vector2(dx, dy);
                    player.Position = p;
                    if (!player.CollideCheck<Solid>() && level.Bounds.Contains((int) p.X, (int) p.Y - 6)) found = p;
                }
            }
        }
        if (found == null) {
            player.Position = original;
            IpcServer.Send(Reply.Error(id, "no free spot near target"));
            return;
        }
        player.Position = found.Value;
        player.ZeroRemainderX();
        player.ZeroRemainderY();
        player.Speed = Vector2.Zero;
        player.Dashes = player.MaxDashes;
        if (player.StateMachine.State != Player.StNormal) player.StateMachine.State = Player.StNormal;
        IpcServer.Send(Reply.Ok(id, w => {
            w.WriteNumber("x", found.Value.X);
            w.WriteNumber("y", found.Value.Y);
        }));
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
        if (job != null || Playback.Running) {
            IpcServer.Send(Reply.Error(id, "busy: a step or a playback is already running"));
            return;
        }
        if (!a.TryGetProperty("inputs", out JsonElement inputs)) {
            IpcServer.Send(Reply.Error(id, "missing 'inputs'"));
            return;
        }

        Playback.EndOverlay(); // 真实执行开始：规划回放的历史线清掉，镜头还给游戏
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
