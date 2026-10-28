using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using Celeste;
using Celeste.Mod;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// The resident owner of a game/recording session. Reloadable assemblies may
/// supply logic, but cannot own the sockets, clock, input bindings or audio DSP.
/// </summary>
public static class BridgeRuntime {
    public const int ContractVersion = 1;
    public static bool SupportsSceneReloadModes => true;
    public static bool SupportsOriginSceneHold => true;
    public const string PayloadName = "CelesteAIBridge";
    private static readonly object Gate = new();
    private static readonly ConcurrentQueue<Request> Inbox = new();
    private sealed record Attachment(Guid Owner, string Version, Action<Action> Tick, Action<Request> Enqueue,
        Func<bool> CanReload, Func<string> Export, Action<string> Import, Action? Service);
    private sealed class Lease {
        public string Id = Guid.NewGuid().ToString("N");
        public string Stage = "prepared", Error = "", Snapshot = "", Path = "", ExpectedVersion = "";
        public string SceneIdentity = Guid.NewGuid().ToString("N");
        // The original held object remains the identity of a preserve-scene
        // lease across every retry; never rebase it to the latest attempt.
        public Scene? Scene;
        // Replacement mode waits for this attempt's scene to be replaced,
        // including when a previous failed attempt already rebuilt the room.
        public Scene? CommitScene;
        public bool ExpectSceneChange;
        public bool PreserveScene;
        public bool SceneModeInitialized;
        public long Frame, AttemptAt;
        public Guid PreviousOwner;
        public EverestModuleMetadata? Metadata;
        public bool Imported;
    }
    private static Attachment? attachment;
    private static Lease? lease;
    private static volatile bool holdingReload;
    private static bool finishedThisTick;
    private static string payloadVersion = "";
    private static readonly Stopwatch SinceHeldTick = Stopwatch.StartNew();
    private const double HeldTickMs = 1000.0 / 60;

    public static bool Active { get; set; }
    public static long Frame { get; set; }
    public static bool EngineHalted { get; set; } = true;
    public static bool FreeFrame { get; set; }
    public static bool FeedInputThisFrame { get; set; }
    public static InputFrame CurrentInput { get; set; } = InputFrame.Neutral;
    public static bool InputOwned { get; set; }
    public static bool HoldingReload => holdingReload;
    public static bool IsReloading => holdingReload;
    public static bool Ready => FreezeGate.Available;

    public static void Attach(Guid owner, int abi, string version, Action<Action> tick, Action<Request> enqueue,
        Func<bool> canReload, Func<string> exportState, Action<string> importState, Action? serviceRequests = null) {
        if (owner == Guid.Empty) throw new ArgumentException("A payload owner is required");
        if (abi != ContractVersion) throw new InvalidOperationException($"Runtime ABI mismatch: expected {ContractVersion}, got {abi}");
        lock (Gate) {
            if (attachment != null) throw new InvalidOperationException("A bridge payload is already attached");
            attachment = new(owner, version, tick, enqueue, canReload, exportState, importState, serviceRequests);
            payloadVersion = version;
            if (lease != null && holdingReload) lease.Imported = false;
        }
        Log.Info($"bridge logic attached: {version}, owner {owner}");
    }

    public static void Detach(Guid owner) {
        lock (Gate) {
            if (attachment?.Owner != owner) throw new InvalidOperationException("Bridge detach owner mismatch");
            attachment = null; // No delegate or DTO from the old collectible ALC survives this point.
            if (Active && !holdingReload) {
                holdingReload = true;
                lease = new Lease { Stage = "failed", Scene = Engine.Scene, Frame = Frame,
                    Error = "Bridge logic was unloaded outside a prepared reload; this hold cannot be retried or released safely, restart the game" };
            }
        }
    }

    /// <summary>Network threads may answer status, but game operations run on the main thread.</summary>
    public static void Enqueue(Request request) {
        if (request.Cmd == "reload_status") {
            try { ValidateLease(request.Args, optional: true); SendStatus(request.Id); }
            catch (Exception ex) { IpcServer.Send(Reply.Error(request.Id, ex.Message)); }
            return;
        }
        lock (Gate) {
            if (request.Cmd == "hello" && holdingReload && lease?.Stage != "ready") {
                // Read-only: a restarted controller can see why the game is held.
                SendHeldHello(request.Id);
                return;
            }
            if (holdingReload && request.Cmd is not ("reload_prepare" or "reload_commit" or "reload_finish" or "reload_abort") &&
                    (lease?.Stage != "ready" || request.Cmd is not ("hello" or "observe" or "areas"))) {
                IpcServer.Send(Reply.Error(request.Id, "Bridge maintenance hold; no game input was executed"));
                return;
            }
        }
        Inbox.Enqueue(request);
    }

    internal static void Tick(Action runOriginal, Action runFixedOriginal) {
        // Everest's Mod reload re-initializes inputs on a worker thread. Re-apply
        // the injected bindings on the main thread once fresh maintenance
        // objects finish, including when the original held object is a loader.
        if (!holdingReload || !RunningMaintenanceScene(Engine.Scene)) InputInjector.ServicePending();
        DrainRequests();
        if (finishedThisTick) {
            finishedThisTick = false;
            EngineHalted = true;
            FeedInputThisFrame = false;
            runOriginal();
            return;
        }
        UpdateReadiness();
        Attachment? current;
        lock (Gate) current = attachment;
        if (holdingReload) {
            RuntimeStatus.ReportHold(payloadVersion);
            ClockServer.AcknowledgeIdle();
            EngineHalted = true;
            FreeFrame = false;
            FeedInputThisFrame = false;
            CurrentInput = InputFrame.Neutral;
            if (lease?.Stage == "ready") current?.Service?.Invoke();
            // Everest owns its maintenance UI/loaders. Never call payload Tick
            // or increment the bridge/audio counters while maintenance is held.
            runOriginal();
            PaceHeldTick();
            return;
        }
        if (current == null) { runOriginal(); return; }
        current.Tick(runFixedOriginal);
    }

    /// <summary>
    /// Lockstep turns off FNA's fixed time step, so a held game would otherwise
    /// update and draw as fast as possible. Held ticks produce no movie frames
    /// (the clock reader answers tokens with N), so pacing them only saves CPU/GPU.
    /// </summary>
    private static void PaceHeldTick() {
        if (Engine.Instance == null || Engine.Instance.IsFixedTimeStep) {
            SinceHeldTick.Restart();
            return;
        }
        double left = HeldTickMs - SinceHeldTick.Elapsed.TotalMilliseconds;
        if (left >= 1) Thread.Sleep((int)left);
        SinceHeldTick.Restart();
    }

    private static bool MaintenanceScene(Scene? scene) => scene is AssetReloadHelper or LevelLoader or LevelExit or OverworldLoader or GameLoader;
    // A loader can be the caller's preserved scene. Only maintenance objects
    // introduced after prepare may advance; the original object stays frozen.
    private static bool RunningMaintenanceScene(Scene? scene) => MaintenanceScene(scene) && !ReferenceEquals(scene, lease?.Scene);
    public static bool ShouldHaltEngineUpdate() {
        if (holdingReload) return !RunningMaintenanceScene(Engine.Scene);
        return Active && (EngineHalted || FreeFrame && Engine.Scene is Level or AreaComplete or IntroVignette or LevelEnter);
    }

    private static void DrainRequests() {
        bool forwarded = false;
        while (Inbox.TryDequeue(out Request? request)) {
            try {
                switch (request.Cmd) {
                    case "reload_prepare": Prepare(request, forwarded); break;
                    case "reload_commit": Commit(request); break;
                    case "reload_finish": Finish(request); break;
                    case "reload_abort": Abort(request); break;
                    default:
                        if (request.Cmd == "activate" && !FreezeGate.Available)
                            throw new InvalidOperationException("Resident freeze gate is not ready; activation refused and no input was executed");
                        Attachment? current;
                        lock (Gate) current = attachment;
                        if (current == null) throw new InvalidOperationException("Bridge logic is unavailable; no game input was executed");
                        if (holdingReload && (lease?.Stage != "ready" || request.Cmd is not ("hello" or "observe" or "areas")))
                            throw new InvalidOperationException("Bridge maintenance hold; no game input was executed");
                        current.Enqueue(request);
                        forwarded = true;
                        break;
                }
            } catch (Exception ex) { IpcServer.Send(Reply.Error(request.Id, ex.Message)); }
        }
    }

    private static void Prepare(Request request, bool forwarded) {
        if (!FreezeGate.Available)
            throw new InvalidOperationException("Resident freeze gate is not ready; reload preparation refused");
        Attachment? current;
        lock (Gate) {
            if (holdingReload) { SendStatus(request.Id); return; }
            current = attachment;
        }
        if (current == null || !Active || forwarded || !current.CanReload()) {
            IpcServer.Send(Reply.Ok(request.Id, w => { w.WriteString("stage", "busy"); w.WriteString("error", "Wait for the current step, render acknowledgement and playback to finish"); }));
            return;
        }
        EverestModuleMetadata metadata = FindPayload() ?? throw new InvalidOperationException("Registered bridge metadata was not found");
        CheckReverseDependencies(metadata);
        if (string.IsNullOrEmpty(metadata.PathDirectory) || !string.IsNullOrEmpty(metadata.PathArchive))
            throw new InvalidOperationException("Reload deployment requires an unpacked bridge Mod directory");
        string path = System.IO.Path.GetFullPath(System.IO.Path.Combine(metadata.PathDirectory, metadata.DLL));
        string root = System.IO.Path.GetFullPath(metadata.PathDirectory).TrimEnd(System.IO.Path.DirectorySeparatorChar) + System.IO.Path.DirectorySeparatorChar;
        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Bridge DLL escapes its Mod directory");
        var prepared = new Lease { Scene = Engine.Scene, Metadata = metadata, Snapshot = current.Export(),
            Path = path, PreviousOwner = current.Owner, Frame = Frame };
        lock (Gate) { lease = prepared; holdingReload = true; }
        EngineHalted = true;
        FeedInputThisFrame = false;
        CurrentInput = InputFrame.Neutral;
        ClockServer.AcknowledgeIdle();
        SendStatus(request.Id);
    }

    private static EverestModuleMetadata? FindPayload() => Everest.Modules.FirstOrDefault(m => m.Metadata.Name == PayloadName)?.Metadata;
    private static void CheckReverseDependencies(EverestModuleMetadata metadata) {
        foreach (EverestModule module in Everest.Modules) {
            if (module.Metadata.Name == metadata.Name) continue;
            bool declared = module.Metadata.Dependencies.Any(d => d.Name == metadata.Name);
            bool actual = module.Metadata.AssemblyContext?.GetActiveDependencyContextsSnapshot()
                .Any(c => c.ModuleMeta.Name == metadata.Name) == true;
            if (declared || actual) throw new InvalidOperationException("Bridge reload would also reload dependent Mod " + module.Metadata.Name);
        }
    }

    private static void Commit(Request request) {
        Lease current = ValidateLease(request.Args);
        if (current.Stage is not ("prepared" or "failed" or "ready")) throw new InvalidOperationException("Reload is not prepared or retryable");
        if (current.Metadata == null) throw new InvalidOperationException("Reload metadata is unavailable");
        CheckReverseDependencies(current.Metadata);
        string expected = request.Args.TryGetProperty("expected_version", out var v) ? v.GetString() ?? "" : "";
        bool expectSceneChange = !current.SceneModeInitialized || !current.PreserveScene;
        if (request.Args.TryGetProperty("expect_scene_change", out var sceneMode)) {
            if (sceneMode.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new InvalidOperationException("expect_scene_change must be boolean");
            expectSceneChange = sceneMode.GetBoolean();
        }
        if (current.SceneModeInitialized && current.PreserveScene == expectSceneChange)
            throw new InvalidOperationException("Scene completion mode cannot change within a lease");
        bool ordinary = FindPayload() != null && current.Metadata.AssemblyContext != null;
        lock (Gate) {
            current.ExpectedVersion = expected;
            current.PreviousOwner = attachment?.Owner ?? Guid.Empty;
            current.Stage = "reloading";
            current.Error = "";
            current.Imported = false;
            current.AttemptAt = Environment.TickCount64;
            current.CommitScene = Engine.Scene;
            current.ExpectSceneChange = ordinary && expectSceneChange;
            current.PreserveScene = !expectSceneChange;
            current.SceneModeInitialized = true;
        }
        try {
            if (ordinary) {
                // Follow Everest's ordinary lifecycle. The caller declares
                // whether completion replaces or preserves the scene object.
                Everest.Loader.ReloadMod(current.Metadata);
            } else {
                // A failed ordinary reload can leave a context without a
                // registered module. Clean only that orphan before normal load.
                AssetReloadHelper.Do("Retry Celeste AI bridge", () => {
                    try {
                        current.Metadata.AssemblyContext?.Dispose();
                        current.Metadata.AssemblyContext = null;
                        current.Metadata.InvalidateHash();
                        if (!Everest.Loader.LoadMod(current.Metadata)) Fail(current, "Bridge reload retry failed to load");
                    } catch (Exception ex) { Fail(current, ex.Message); }
                });
            }
        } catch (Exception ex) { Fail(current, ex.Message); }
        SendStatus(request.Id);
    }

    private static Lease ValidateLease(JsonElement args, bool optional = false) {
        string? id = args.TryGetProperty("lease", out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
        lock (Gate) {
            if (lease == null) {
                if (optional && id == null) return null!;
                throw new InvalidOperationException("No prepared bridge reload exists");
            }
            if (id != lease.Id && !(optional && id == null)) throw new InvalidOperationException("Reload lease mismatch");
            return lease;
        }
    }

    private static void UpdateReadiness() {
        Lease? current;
        Attachment? next;
        lock (Gate) { current = lease; next = attachment; }
        if (!holdingReload || current?.Stage != "reloading") return;
        if (Environment.TickCount64 - current.AttemptAt > 120000) { Fail(current, "Bridge reload did not become ready within 120 seconds"); return; }
        if (RunningMaintenanceScene(Engine.Scene)) return;
        bool awaitingScene = current.ExpectSceneChange && ReferenceEquals(Engine.Scene, current.CommitScene);
        if (next == null) {
            if (current.ExpectSceneChange && !awaitingScene)
                Fail(current, "Everest completed room reload without compatible bridge logic");
            return;
        }
        if (next.Owner == current.PreviousOwner) return;
        // Wait for the ordinary ReloadLevel of this commit attempt; a retry after
        // an earlier room reload must not become ready before its own rebuild.
        if (awaitingScene) return;
        try {
            CheckHeldFrame(current);
            if (!string.IsNullOrEmpty(current.ExpectedVersion) && next.Version != current.ExpectedVersion)
                throw new InvalidOperationException($"Bridge version mismatch: expected {current.ExpectedVersion}, got {next.Version}");
            if (!current.Imported) { next.Import(current.Snapshot); current.Imported = true; }
            CheckHeldFrame(current);
            if (next.Service == null) throw new InvalidOperationException("Bridge payload cannot service read-only requests while held");
            lock (Gate) current.Stage = "ready";
        } catch (Exception ex) { Fail(current, ex.Message); }
    }

    private static void CheckHeldFrame(Lease current) {
        if (Frame != current.Frame) throw new InvalidOperationException("Bridge frame counter changed during reload");
        if (current.PreserveScene && !ReferenceEquals(Engine.Scene, current.Scene))
            throw new InvalidOperationException("The scene object changed during a preserve-scene reload");
    }

    private static void Finish(Request request) {
        Lease current = ValidateLease(request.Args);
        if (current.Stage == "finished") { SendStatus(request.Id); return; }
        if (current.Stage != "ready") throw new InvalidOperationException("Bridge reload is not ready");
        CheckHeldFrame(current);
        InputInjector.ServicePending();
        string[] mismatches = InputInjector.BindingMismatches();
        if (mismatches.Length > 0) Log.Warn("finishing reload while input bindings differ: " + string.Join(", ", mismatches));
        lock (Gate) {
            current.Stage = "finished";
            current.Scene = null;
            current.CommitScene = null;
            current.Metadata = null;
            current.Snapshot = "";
            holdingReload = false;
            finishedThisTick = true;
        }
        SendStatus(request.Id);
    }

    /// <summary>
    /// Releases a hold only while it is provably the pre-prepare game: nothing
    /// was committed, the original bridge logic is still attached, and neither
    /// the scene nor the frame counter changed. Everything else needs a retry
    /// with a compatible bridge or a game restart.
    /// </summary>
    private static void Abort(Request request) {
        Lease current = ValidateLease(request.Args);
        if (current.Stage == "aborted") { SendStatus(request.Id); return; }
        if (AbortBlocker(current) is { } reason)
            throw new InvalidOperationException("Reload hold cannot be released safely: " + reason);
        lock (Gate) {
            current.Stage = "aborted";
            current.Error = "";
            current.Scene = null;
            current.CommitScene = null;
            current.Metadata = null;
            current.Snapshot = "";
            holdingReload = false;
            finishedThisTick = true;
        }
        Log.Info("bridge reload hold released before commit; the original bridge logic continues");
        SendStatus(request.Id);
    }

    private static string? AbortBlocker(Lease current) {
        lock (Gate) {
            if (!holdingReload || !ReferenceEquals(lease, current)) return "no reload hold is active for this lease";
            if (current.Stage != "prepared")
                return $"stage is {current.Stage}; only a prepared reload that was never committed can be released";
            if (attachment == null || attachment.Owner != current.PreviousOwner) return "the original bridge logic is no longer attached";
        }
        if (!ReferenceEquals(Engine.Scene, current.Scene)) return "the scene changed during the hold";
        if (Frame != current.Frame) return "the bridge frame counter changed during the hold";
        return null;
    }

    private static void Fail(Lease current, string message) {
        lock (Gate) { current.Stage = "failed"; current.Error = message; holdingReload = true; }
        Log.Error("bridge reload held after failure: " + message);
    }

    private static void SendStatus(long id) {
        lock (Gate) {
            IpcServer.Send(Reply.Ok(id, w => {
                w.WriteString("stage", lease?.Stage ?? "idle");
                w.WriteString("lease", lease?.Id);
                w.WriteString("payload_path", lease?.Path);
                w.WriteString("payload_version", attachment?.Version);
                w.WriteString("scene_identity", lease?.SceneIdentity);
                w.WriteString("error", lease?.Error ?? "");
                w.WriteBoolean("holding", holdingReload);
                w.WriteBoolean("frozen", holdingReload);
                w.WriteNumber("frame", Frame);
                w.WriteBoolean("record_audio_active", RecordingAudio.Enabled);
                w.WriteNumber("record_audio_frames", RecordingAudio.RenderedFrames);
                w.WriteNumber("record_audio_replay_frames", RecordingAudio.ReplayFrames);
                WriteHoldAdvice(w);
            }));
        }
    }

    /// <summary>Caller holds Gate.</summary>
    private static void WriteHoldAdvice(System.Text.Json.Utf8JsonWriter w) {
        w.WriteBoolean("reload_scene_modes", SupportsSceneReloadModes);
        w.WriteBoolean("reload_origin_scene_hold", SupportsOriginSceneHold);
        w.WriteBoolean("abortable", lease != null && AbortBlocker(lease) == null);
        // A hold whose payload metadata is unknown can neither be retried nor released.
        w.WriteBoolean("restart_required", holdingReload && lease?.Stage == "failed" && lease.Metadata == null);
        EverestModuleMetadata? runtime = RuntimeModule.Instance?.Metadata;
        w.WriteString("runtime_version", runtime?.VersionString);
        string? runtimePath = null;
        try {
            if (!string.IsNullOrEmpty(runtime?.PathDirectory) && !string.IsNullOrEmpty(runtime.DLL) && string.IsNullOrEmpty(runtime.PathArchive))
                runtimePath = System.IO.Path.GetFullPath(System.IO.Path.Combine(runtime.PathDirectory, runtime.DLL));
        } catch (Exception) { /* Report no path rather than failing the status reply. */ }
        w.WriteString("runtime_path", runtimePath);
    }

    /// <summary>
    /// hello while held before readiness: only resident facts, no payload
    /// capabilities, so callers cannot mistake it for a usable bridge.
    /// Caller holds Gate.
    /// </summary>
    private static void SendHeldHello(long id) {
        IpcServer.Send(Reply.Ok(id, w => {
            w.WriteNull("bridge");
            w.WriteBoolean("reload_hold", true);
            w.WriteBoolean("active", Active);
            w.WriteBoolean("frozen", true);
            w.WriteBoolean("input_owned", InputOwned);
            w.WriteNumber("frame", Frame);
            w.WriteString("scene", Engine.Scene?.GetType().Name ?? "none");
            w.WriteNumber("bridge_reload", FreezeGate.Available ? 1 : 0);
            w.WriteNumber("runtime_contract", ContractVersion);
            w.WriteBoolean("record_audio_nrt", true);
            w.WriteNumber("record_audio_tracks", RecordingAudio.TrackCount);
            w.WriteBoolean("record_audio_active", RecordingAudio.Enabled);
            w.WriteNumber("record_audio_frames", RecordingAudio.RenderedFrames);
            w.WriteNumber("record_audio_replay_frames", RecordingAudio.ReplayFrames);
            if (RecordingAudio.Error != null) w.WriteString("record_audio_error", RecordingAudio.Error);
            w.WriteStartObject("clock");
            w.WriteBoolean("connected", ClockServer.Connected);
            w.WriteBoolean("lockstep", Active && ClockServer.Connected && ClockServer.LockstepRequested);
            w.WritePropertyName("plugin");
            if (ClockServer.PluginStatus is { } status && IsJson(status)) w.WriteRawValue(status, skipInputValidation: true);
            else w.WriteNullValue();
            w.WriteEndObject();
            w.WriteStartObject("reload");
            w.WriteString("stage", lease?.Stage ?? "idle");
            w.WriteString("lease", lease?.Id);
            w.WriteString("payload_path", lease?.Path);
            w.WriteString("payload_version", attachment?.Version);
            w.WriteString("error", lease?.Error ?? "");
            w.WriteBoolean("holding", holdingReload);
            WriteHoldAdvice(w);
            w.WriteEndObject();
        }));
    }

    private static bool IsJson(string text) {
        try {
            using JsonDocument document = JsonDocument.Parse(text);
            return true;
        } catch (JsonException) {
            return false;
        }
    }

    public static void Shutdown() {
        lock (Gate) { attachment = null; lease = null; holdingReload = false; }
        Active = false;
        InputOwned = false;
        FeedInputThisFrame = false;
        CurrentInput = InputFrame.Neutral;
        while (Inbox.TryDequeue(out _)) { }
    }
}
