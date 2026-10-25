using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.IO;
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
/// 未接管：游戏照常运行。接管后仅章节流程空闲时冻结；菜单空闲时正常运行，收到 step 时执行指定输入，
/// 任务完成后回报并恢复对应场景的空闲策略。加载场景自动走完。
/// </summary>
internal static class Bridge {
    public const string Version = "1.0.15";
    private const int MaxPlanFrames = 60 * 60 * 10;

    public static bool Active { get; private set; }
    public static bool Frozen => Active && SceneControl.FreezeWhenIdle(Engine.Scene) && job == null && !IsLoading();
    public static bool OwnsInput => SceneControl.OwnInput(Active, Engine.Scene, job != null);
    private static bool AdvanceWhenIdle => !SceneControl.FreezeWhenIdle(Engine.Scene) || IsLoading();
    public static bool FeedInputThisFrame { get; private set; }
    public static InputFrame CurrentInput { get; private set; } = InputFrame.Neutral;

    /// <summary>接管以来推进过的总帧数（含空闲菜单和加载帧）。</summary>
    public static long Frame { get; private set; }

    private static bool engineHalted = true;
    private static bool freeFrame;
    private static StepJob? job;
    private static readonly ConcurrentQueue<Request> Inbox = new();
    private static readonly List<RenderReply> PendingRenders = new();

    private sealed record RenderReply(long Id, Action<Utf8JsonWriter> Body, string Buffer);

    public static bool ShouldHaltEngineUpdate() => SceneControl.HaltEngine(Active, engineHalted, freeFrame, Engine.Scene);

    public static void Enqueue(Request request) => Inbox.Enqueue(request);

    // ---- 每个节拍的入口 ----

    public static void Tick(Action runOriginal) {
        ModHud.ReportStatus(Active, lockstepEngaged, Frame, ClockServer.Connected, RecordingAudio.Enabled, Frozen, OwnsInput);
        DrainInbox();
        SyncInputOwnership();
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
            if (AdvanceWhenIdle) {
                AdvanceFrame(runOriginal, InputFrame.Neutral, feedInput: OwnsInput);
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
    private static bool continuousMusic;
    private static AudioMode IdleAudio => continuousMusic ? AudioMode.MusicOnly : AudioMode.Frozen;
    /// <summary>停住后第几个节拍暂停声音（见 LockstepTick）；idleTokens 是自上一帧以来没有推进的节拍数。</summary>
    private const int FreezeAfterIdleTokens = 2;
    private static int idleTokens;
    // 规划回放时只放音乐：音效、界面音效与音乐里的短促音效（stings）暂停
    private static readonly string[] SfxBuses = { "bus:/gameplay_sfx", "bus:/ui_sfx", "bus:/music/stings" };

    /// <summary>录制校准用：在画面左下角画出帧号条码（只在校准时打开）。</summary>
    public static bool DrawFrameMarker;

    private static void LockstepTick(Action runOriginal) {
        bool wantsFrame = job != null || AdvanceWhenIdle || Playback.Running;
        // 没有要推进的帧时也要及时回复令牌；等待令牌的同时限制空转的循环频率
        if (!ClockServer.TryTakeToken(wantsFrame ? 6 : 3, out long token)) {
            HaltWithoutDrawing(runOriginal);
            return;
        }
        if (!wantsFrame) {
            ClockServer.Send($"N {token}");
            // 游戏停住后的第 FreezeAfterIdleTokens 个节拍暂停声音（放在这个节拍的 Audio.Update 之前）。
            // 第一个节拍就暂停时，暂停正好卡在上一帧那 1/60 秒的末尾，FMOD 按混音块生效有十几毫秒的抖动，
            // 约四分之一的接缝里上一帧末尾被截成静音（实测 3–19 毫秒，听得出断口）；晚一个节拍留出余量，
            // 代价是每个接缝跳过约一帧长度的音乐（接缝两边有 2 毫秒淡出淡入，听不出来）。
            // 更早的做法是等 60 毫秒才暂停，每个接缝丢约 50 毫秒
            if (++idleTokens >= FreezeAfterIdleTokens) SetAudio(IdleAudio);
            HaltWithoutDrawing(runOriginal);
            return;
        }
        idleTokens = 0;
        // OBS 的游戏采集钩子最多每 1/120 秒拷贝一次画面，离上一次呈现太近的那次呈现不会被拷贝：
        // 停住时不重画（见 HaltWithoutDrawing），推进前再确认离上一次呈现已超过这个间隔
        WaitSincePresent(MinPresentGapMs);
        if (job == null && Playback.Running && !IsLoading()) {
            // 规划回放的一帧：不推进游戏，只重画；回复里章节计时记为 -1，录像对账时与游玩帧分开。
            // 声音在这一帧的 Audio.Update 之前恢复（只放音乐），从这一帧开头就有声音
            SetAudio(AudioMode.MusicOnly);
            Halt(runOriginal);
            if (Engine.Scene is Level level) Playback.Step(level);
            // 录像声音：完整版音轨跟着这一帧推进（只放音乐），纯享版音轨不推进（见 RecordingAudio）
            RecordingAudio.AdvanceReplay();
            pendingAck = token;
            pendingAckReplay = true;
            return;
        }
        // 推进之前恢复声音：恢复的指令在这一帧的 Audio.Update 里生效，这一段从第一帧开头就有声音
        //（之前在推进之后才恢复，要到第二帧才有声音，每段开头多一帧静音）
        SetAudio(AudioMode.Playing);
        if (IsLoading()) {
            AdvanceFrame(runOriginal, InputFrame.Neutral);
            if (job != null) job.LoadingFrames++;
        } else if (job != null) {
            AdvanceFrame(runOriginal, job.NextInput());
            job.AfterFrame(Frame);
            if (job.Finished) {
                FinishJob();
            }
        } else {
            AdvanceFrame(runOriginal, InputFrame.Neutral, feedInput: OwnsInput);
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
        if (PendingRenders.Count > 0) return;
        // 游戏内 HUD 有新内容时（思考语句、计时）隔一会儿重画一次：离上一次呈现至少 50 毫秒，
        // 不会顶掉 OBS 还没采到的上一个游玩帧；这次呈现没有时钟令牌，插件不录它
        if (ModHud.RedrawDue && SincePresent.Elapsed.TotalMilliseconds >= 50) {
            ModHud.NoteIdleDraw();
            return;
        }
        Engine.Instance.SuppressDraw();
    }

    private static void FlushPendingAck() {
        if (pendingAck is not { } token) return;
        RecordingAudio.Publish(token, pendingAckReplay);
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
    /// 录制（锁步）时更精确：停住后的第二个节拍暂停、推进前恢复（见 LockstepTick），这里只是兜底。
    /// 规划回放的帧里音乐照常播放（音效暂停）。录像声音另有两条独立音轨（RecordingAudio）：完整版音轨在回放帧里
    /// 同样只放音乐、随帧推进；纯享版音轨只随真实游玩帧推进，去掉回放之后音乐依然连贯。</summary>
    private static void UpdateAudio(bool advanced, bool replayed) {
        if (advanced) {
            SinceAdvance.Restart();
            SetAudio(AudioMode.Playing);
        } else if (replayed) {
            SinceAdvance.Restart();
            SetAudio(AudioMode.MusicOnly);
        } else if (audioMode != IdleAudio && SinceAdvance.ElapsedMilliseconds > 60) {
            SetAudio(IdleAudio);
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
        RecordingAudio.Stop();
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

    private static void SyncInputOwnership() {
        if (OwnsInput) InputInjector.Install();
        else InputInjector.Uninstall();
    }

    private static void AdvanceFrame(Action runOriginal, InputFrame input, bool feedInput = true) {
        SyncInputOwnership();
        engineHalted = false;
        freeFrame = !OwnsInput;
        FeedInputThisFrame = feedInput;
        CurrentInput = input;
        try {
            runOriginal();
            RecordingAudio.Advance();
        } finally {
            engineHalted = true;
            freeFrame = false;
            FeedInputThisFrame = false;
            Frame++;
            SyncInputOwnership();
        }
    }

    private static void FinishJob() {
        StepJob finished = job!;
        job = null;
        SyncInputOwnership();
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
                    w.WriteBoolean("frozen", Frozen);
                    w.WriteBoolean("input_owned", OwnsInput);
                    w.WriteString("freeze_scope", "chapter");
                    w.WriteNumber("frame", Frame);
                    w.WriteString("scene", Engine.Scene?.GetType().Name ?? "none");
                    w.WriteBoolean("continuous_music", continuousMusic);
                    w.WriteString("audio_mode", audioMode.ToString());
                    w.WriteBoolean("record_audio_nrt", true);
                    w.WriteBoolean("mod_hud", true);
                    w.WriteNumber("record_audio_tracks", RecordingAudio.TrackCount);
                    w.WriteBoolean("create_save", true);
                    w.WriteBoolean("checkpoints", true);
                    w.WriteBoolean("load_room_modes", true);
                    w.WriteBoolean("stop_rooms", true);
                    w.WriteBoolean("dialog_choices", true);
                    w.WriteBoolean("story_text", true);
                    w.WriteBoolean("memorial_text", true);
                    w.WriteBoolean("story_input", true);
                    w.WriteBoolean("record_audio_active", RecordingAudio.Enabled);
                    w.WriteNumber("record_audio_frames", RecordingAudio.RenderedFrames);
                    w.WriteNumber("record_audio_replay_frames", RecordingAudio.ReplayFrames);
                    w.WritePropertyName("record_audio_diagnostics");
                    JsonSerializer.Serialize(w, RecordingAudio.Diagnostics);
                    if (Audio.CurrentMusicEventInstance is { } music && music.getTimelinePosition(out int musicPosition) == FMOD.RESULT.OK) {
                        w.WriteNumber("live_music_ms", musicPosition);
                        if (RecordingAudio.PositionFor(music) is { } recordingPosition)
                            w.WriteNumber("record_music_ms", recordingPosition);
                        if (RecordingAudio.FullPositionFor(music) is { } fullPosition)
                            w.WriteNumber("record_music_full_ms", fullPosition);
                    }
                    if (RecordingAudio.Error != null) w.WriteString("record_audio_error", RecordingAudio.Error);
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
                continuousMusic = GetBool(a, "continuous_music", continuousMusic);
                string recordAction = GetString(a, "action", "");
                if (recordAction == "start" && GetString(a, "audio_source", "obs_mix") == "game_nrt") {
                    using JsonDocument plugin = JsonDocument.Parse(ClockServer.PluginStatus ?? "{}");
                    if (!plugin.RootElement.TryGetProperty("audio_nrt", out JsonElement capability) || capability.ValueKind != JsonValueKind.True)
                        throw new InvalidOperationException("Update the OBS lockstep plugin for independent recording audio");
                    if (!plugin.RootElement.TryGetProperty("audio_tracks", out JsonElement tracks) || tracks.ValueKind != JsonValueKind.Number ||
                        tracks.GetInt32() != RecordingAudio.TrackCount)
                        throw new InvalidOperationException("Update the OBS lockstep plugin for two-track recording audio");
                    RecordingAudio.Start(Audio.System, Path.Combine(Engine.ContentDirectory, "FMOD", "Desktop"), BridgeModule.Settings.ClockPort);
                }
                else if (recordAction == "stop") RecordingAudio.Stop();
                ClockServer.Send("R " + a.GetRawText());
                IpcServer.Send(Reply.Ok(req.Id, w => w.WriteBoolean("marker", DrawFrameMarker)));
                break;

            case "activate":
                continuousMusic = GetBool(a, "continuous_music", continuousMusic);
                if (!Active) {
                    Active = true;
                    SyncInputOwnership();
                    engineHalted = true;
                    Log.Info("bridge activated");
                }
                SendObservation(req.Id, a, defaultImage: false);
                break;

            case "deactivate":
                Deactivate();
                IpcServer.Send(Reply.Ok(req.Id));
                break;

            case "create_save":
                if (job != null || Playback.Running) throw new InvalidOperationException("game action already running");
                string requestedName = GetString(a, "name", "").Trim();
                bool dryRun = GetBool(a, "dry_run", false);
                int createdSlot = SaveCreation.Start(requestedName, dryRun);
                IpcServer.Send(Reply.Ok(req.Id, w => {
                    w.WriteNumber("slot", createdSlot);
                    w.WriteString("name", requestedName);
                    w.WriteBoolean("started", !dryRun);
                    w.WriteBoolean("dry_run", dryRun);
                    w.WriteBoolean("persisted", File.Exists(UserIO.GetSaveFilePath(SaveData.GetFilename(createdSlot))));
                }));
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

            case "areas":
                // 官方章节（A 面）与各自的检查点：按段通关用（段 k = 检查点 k 到下一个检查点或章节结尾，0 是章节开头）
                IpcServer.Send(Reply.Ok(req.Id, WriteAreas));
                break;

            case "enter_checkpoint":
                // 按段通关：进入某个官方章节（A 面）的某个检查点，与在选章面板里选它相同；没到过的检查点也能进
                EnterCheckpoint(req.Id, a);
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

    private static bool IsOfficial(AreaData? area) => area != null && (area.ToKey().SID ?? "").StartsWith("Celeste/")
        && area.Mode is { Length: > 0 } && area.Mode[0] != null;

    private static string CleanName(string? key, Language? language = null) =>
        string.IsNullOrEmpty(key) ? "" : Dialog.Has(key, language) ? Dialog.Clean(key, language) : key;

    private static void WriteAreas(Utf8JsonWriter w) {
        Dialog.Languages.TryGetValue("english", out Language? english);
        Dialog.Languages.TryGetValue("schinese", out Language? chinese);
        void Names(string? key) {
            w.WriteString("name", CleanName(key));
            if (english != null) w.WriteString("name_en", CleanName(key, english));
            if (chinese != null) w.WriteString("name_zh", CleanName(key, chinese));
        }
        w.WriteStartArray("areas");
        foreach (AreaData area in AreaData.Areas) {
            if (!IsOfficial(area)) continue;
            ModeProperties mode = area.Mode[0];
            w.WriteStartObject();
            w.WriteNumber("id", area.ID);
            w.WriteString("sid", area.ToKey().SID);
            Names(area.Name);
            w.WriteStartArray("checkpoints");
            w.WriteStartObject();                       // 0：章节开头
            w.WriteNumber("index", 0);
            w.WriteString("level", mode.MapData?.StartLevel()?.Name ?? "");
            Names("overworld_start");
            w.WriteEndObject();
            CheckpointData[] checkpoints = mode.Checkpoints ?? Array.Empty<CheckpointData>();
            for (int i = 0; i < checkpoints.Length; i++) {
                w.WriteStartObject();
                w.WriteNumber("index", i + 1);
                w.WriteString("level", checkpoints[i].Level);
                Names(checkpoints[i].Name);
                w.WriteEndObject();
            }
            w.WriteEndArray();
            w.WriteEndObject();
        }
        w.WriteEndArray();
    }

    private static void EnterCheckpoint(long id, JsonElement a) {
        if (!Active) {
            IpcServer.Send(Reply.Error(id, "bridge not active; send activate first"));
            return;
        }
        if (job != null || Playback.Running) {
            IpcServer.Send(Reply.Error(id, "enter_checkpoint needs no running step or playback"));
            return;
        }
        if (SaveData.Instance == null || Engine.Scene is not (Level or Overworld)) {
            IpcServer.Send(Reply.Error(id, $"enter_checkpoint needs a loaded save in a level or on the map (scene={Engine.Scene?.GetType().Name ?? "none"})"));
            return;
        }
        int chapter = GetInt(a, "chapter", -1);
        int index = GetInt(a, "checkpoint", 0);
        AreaData? area = chapter >= 0 && chapter < AreaData.Areas.Count ? AreaData.Areas[chapter] : null;
        if (!IsOfficial(area)) {
            IpcServer.Send(Reply.Error(id, $"no official chapter {chapter}"));
            return;
        }
        CheckpointData[] checkpoints = area!.Mode[0].Checkpoints ?? Array.Empty<CheckpointData>();
        if (index < 0 || index > checkpoints.Length) {
            IpcServer.Send(Reply.Error(id, $"chapter {chapter} has checkpoints 0..{checkpoints.Length}"));
            return;
        }
        var key = new AreaKey(chapter, AreaMode.Normal);
        // 与选章面板相同：新会话从检查点开始（检查点的能力、标记由 Session 按检查点设置），经 LevelEnter 进入
        var session = new Session(key, index == 0 ? null : checkpoints[index - 1].Level);
        SaveData.Instance.LastArea = key;
        LevelEnter.Go(session, false);
        IpcServer.Send(Reply.Ok(id, w => {
            w.WriteNumber("chapter", chapter);
            w.WriteNumber("checkpoint", index);
            w.WriteString("room", session.Level);
        }));
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
        if (Engine.Scene is not Level level || job != null || Playback.Running) {
            IpcServer.Send(Reply.Error(id, "load_room needs a level scene and no running step"));
            return;
        }
        string room = GetString(a, "room", "");
        // 旧请求指定 chapter 时仍默认 A；显式 mode 可选 0=A、1=B、2=C。
        AreaKey area;
        try {
            area = RoomAreaSelection.Select(level.Session.Area,
                a.TryGetProperty("chapter", out JsonElement ch) ? ch.GetInt32() : null,
                a.TryGetProperty("mode", out JsonElement md) ? md.GetInt32() : null);
        } catch (ArgumentException e) {
            IpcServer.Send(Reply.Error(id, e.Message));
            return;
        }
        MapData? map = area.ID < AreaData.Areas.Count ? AreaData.Get(area)?.Mode[(int) area.Mode]?.MapData : null;
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
        IpcServer.Send(Reply.Ok(id, w => {
            w.WriteString("room", room);
            w.WriteNumber("chapter", area.ID);
            w.WriteNumber("mode", (int)area.Mode);
        }));
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
                    w.WriteBoolean("frozen", Frozen);
                    w.WriteBoolean("input_owned", OwnsInput);
                    w.WriteString("freeze_scope", "chapter");
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
        // 按段通关：进入这些房间（这一截终点检查点的房间）时停下，停止原因记为 room；其他换房间不受影响
        List<string> stopRooms = a.TryGetProperty("stop_rooms", out JsonElement sr) && sr.ValueKind == JsonValueKind.Array
            ? sr.EnumerateArray().Select(e => e.GetString() ?? "").Where(x => x.Length > 0).ToList()
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
            progressEvery: GetInt(a, "progress", 0),
            stopRooms: stopRooms);

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
