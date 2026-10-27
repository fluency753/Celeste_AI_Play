using System;
using System.Collections.Concurrent;
using System.IO;
using System.Linq;
using System.Text.Json;
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
    public const string PayloadName = "CelesteAIBridge";
    private static readonly object Gate = new();
    private static readonly ConcurrentQueue<Request> Inbox = new();
    private sealed record Attachment(Guid Owner, string Version, Action<Action> Tick, Action<Request> Enqueue,
        Func<bool> CanReload, Func<string> Export, Action<string> Import, Action? Service);
    private sealed class Lease {
        public string Id = Guid.NewGuid().ToString("N");
        public string Stage = "prepared", Error = "", Snapshot = "", Path = "", ExpectedVersion = "";
        public string SceneIdentity = Guid.NewGuid().ToString("N");
        public Scene? Scene;
        public Session? Session;
        public string? Sid, Room;
        public int? Slot;
        public long SessionTime, Frame, AttemptAt;
        public Guid PreviousOwner;
        public EverestModuleMetadata? Metadata;
        public bool Imported;
    }
    private static Attachment? attachment;
    private static Lease? lease;
    private static volatile bool holdingReload;
    private static bool finishedThisTick;
    private static string payloadVersion = "";

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
                lease = new Lease { Stage = "failed", Error = "Bridge detached without a prepared reload; game remains held", Scene = Engine.Scene, Frame = Frame };
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
            if (holdingReload && request.Cmd is not ("reload_prepare" or "reload_commit" or "reload_finish") &&
                    (lease?.Stage != "ready" || request.Cmd is not ("hello" or "observe" or "areas"))) {
                IpcServer.Send(Reply.Error(request.Id, "Bridge maintenance hold; no game input was executed"));
                return;
            }
        }
        Inbox.Enqueue(request);
    }

    internal static void Tick(Action runOriginal, Action runFixedOriginal) {
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
            return;
        }
        if (current == null) { runOriginal(); return; }
        current.Tick(runFixedOriginal);
    }

    private static bool MaintenanceScene(Scene? scene) => scene is AssetReloadHelper or LevelLoader or LevelExit or OverworldLoader or GameLoader;
    public static bool ShouldHaltEngineUpdate() {
        if (holdingReload) return !MaintenanceScene(Engine.Scene);
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
        if (Engine.Scene is not Level readyLevel || readyLevel.Transitioning) {
            IpcServer.Send(Reply.Ok(request.Id, w => { w.WriteString("stage", "busy"); w.WriteString("error", "Reload requires a loaded Level outside a room transition; menus and story scenes are not supported"); }));
            return;
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
        if (Engine.Scene is Level level) {
            prepared.Session = level.Session;
            prepared.Sid = level.Session.Area.SID;
            prepared.Room = level.Session.Level;
            prepared.SessionTime = level.Session.Time;
            prepared.Slot = SaveData.Instance?.FileSlot;
        }
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
        lock (Gate) {
            current.ExpectedVersion = expected;
            current.PreviousOwner = attachment?.Owner ?? Guid.Empty;
            current.Stage = "reloading";
            current.Error = "";
            current.Imported = false;
            current.AttemptAt = Environment.TickCount64;
        }
        try {
            if (FindPayload() != null && current.Metadata.AssemblyContext != null) {
                // The ordinary Everest path intentionally reloads the room.
                // The resident owner keeps recording and clocks across it.
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
        if (MaintenanceScene(Engine.Scene)) return;
        if (next == null) {
            if (current.Scene is Level && !ReferenceEquals(Engine.Scene, current.Scene)) Fail(current, "Everest completed room reload without compatible bridge logic");
            return;
        }
        if (next.Owner == current.PreviousOwner) return;
        if (current.Scene is Level && ReferenceEquals(Engine.Scene, current.Scene)) return; // Wait for ordinary ReloadLevel.
        try {
            CheckScene(current);
            if (!string.IsNullOrEmpty(current.ExpectedVersion) && next.Version != current.ExpectedVersion)
                throw new InvalidOperationException($"Bridge version mismatch: expected {current.ExpectedVersion}, got {next.Version}");
            if (!current.Imported) { next.Import(current.Snapshot); current.Imported = true; }
            CheckScene(current);
            if (next.Service == null) throw new InvalidOperationException("Bridge payload cannot service read-only requests while held");
            lock (Gate) current.Stage = "ready";
        } catch (Exception ex) { Fail(current, ex.Message); }
    }

    private static void CheckScene(Lease current) {
        if (Frame != current.Frame) throw new InvalidOperationException("Bridge frame counter changed during reload");
        if (current.Session == null) return;
        if (Engine.Scene is not Level level || !ReferenceEquals(level.Session, current.Session) ||
                level.Session.Area.SID != current.Sid || level.Session.Level != current.Room ||
                level.Session.Time != current.SessionTime || SaveData.Instance?.FileSlot != current.Slot)
            throw new InvalidOperationException("Reloaded room does not match the held Session, map, room, save slot or chapter time");
    }

    private static void Finish(Request request) {
        Lease current = ValidateLease(request.Args);
        if (current.Stage == "finished") { SendStatus(request.Id); return; }
        if (current.Stage != "ready") throw new InvalidOperationException("Bridge reload is not ready");
        CheckScene(current);
        lock (Gate) {
            current.Stage = "finished";
            current.Scene = null;
            current.Session = null;
            current.Metadata = null;
            current.Snapshot = "";
            holdingReload = false;
            finishedThisTick = true;
        }
        SendStatus(request.Id);
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
            }));
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
