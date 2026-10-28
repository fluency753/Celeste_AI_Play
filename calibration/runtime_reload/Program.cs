using System.Net;
using System.IO.MemoryMappedFiles;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using Celeste;
using Celeste.Mod;
using CelesteAI.Bridge;
using Bridge = CelesteAI.Bridge.Bridge;
using Monocle;
using MonoMod.RuntimeDetour;
using MonoMod.Cil;

string game = args[0];
var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
AssemblyLoadContext.Default.Resolving += (_, name) => {
    string path = Path.Combine(game, name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.GetFullPath(path)) : null;
};
NativeLibrary.SetDllImportResolver(typeof(FMOD.System).Assembly, (name, _, _) =>
    NativeLibrary.Load(Path.Combine(game, "everest-lib", "lib64-win-x64", name.Contains("studio") ? "fmodstudio64.dll" : "fmod64.dll")));
void Check(bool value, string message) { if (!value) throw new Exception(message); }
void Fmod(FMOD.RESULT value) { if (value != FMOD.RESULT.OK) throw new Exception(value.ToString()); }
int Port() { using var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start(); return ((IPEndPoint)listener.LocalEndpoint).Port; }
int ipcPort = Port(), clockPort = Port(), audioPort = 100000 + Environment.ProcessId;
var engine = (Engine)RuntimeHelpers.GetUninitializedObject(typeof(Engine));
typeof(Engine).GetProperty("Instance", flags)!.SetValue(null, engine);
void Scene(Scene value) => typeof(Engine).GetField("scene", flags)!.SetValue(engine, value);
AreaData.Areas = new() { new AreaData { ID = 0, SID = "test/reload" } };
SaveData.Instance = (SaveData)RuntimeHelpers.GetUninitializedObject(typeof(SaveData)); SaveData.Instance.FileSlot = 10;
var session = (Session)RuntimeHelpers.GetUninitializedObject(typeof(Session)); session.Area = new AreaKey(0); session.Level = "start";
Level NewLevel() { var level = (Level)RuntimeHelpers.GetUninitializedObject(typeof(Level)); level.Session = session; return level; }
var originalScene = NewLevel(); Scene(originalScene);
Everest._Modules.Clear();
var metadata = new EverestModuleMetadata { Name = BridgeRuntime.PayloadName, DLL = "bin/CelesteAIBridge.dll",
    PathDirectory = Path.Combine(Path.GetTempPath(), "celeste-reload-fixture") };
metadata.AssemblyContext = (EverestModuleAssemblyContext)RuntimeHelpers.GetUninitializedObject(typeof(EverestModuleAssemblyContext));
Everest._Modules.Add(new FixtureModule { Metadata = metadata });
Fmod(FMOD.Studio.System.create(out var live)); Fmod(live.getLowLevelSystem(out var low));
Fmod(low.setOutput(FMOD.OUTPUTTYPE.NOSOUND_NRT)); Fmod(low.setSoftwareFormat(48000, FMOD.SPEAKERMODE.STEREO, 0));
Fmod(low.setDSPBufferSize(800, 2));
Fmod(live.initialize(256, FMOD.Studio.INITFLAGS.SYNCHRONOUS_UPDATE, FMOD.INITFLAGS.NORMAL, IntPtr.Zero));
foreach (string file in new[] { "Master Bank.bank", "Master Bank.strings.bank", "music.bank", "sfx.bank", "ui.bank" })
    Fmod(live.loadBankFile(Path.Combine(game, "Content", "FMOD", "Desktop", file), FMOD.Studio.LOAD_BANK_FLAGS.NORMAL, out _));
Fmod(live.getEvent("event:/music/lvl0/intro", out var musicDescription));
Fmod(musicDescription.createInstance(out var music)); Fmod(music.start());
for (int i = 0; i < 360; i++) Fmod(live.update());
RecordingAudio.Load();
RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), audioPort);
IpcServer.Start(ipcPort); ClockServer.Start(clockPort);
Guid owner = Guid.Empty;
int imports = 0, logicTicks = 0, originalTicks = 0;
bool failReload = false;
bool wrongSlot = false;
bool deferScene = false;
WeakReference? oldContext = null;
object AudioMixer(string name) => typeof(RecordingAudio).GetField(name, flags)!.GetValue(null)!;
object full = AudioMixer("full"), play = AudioMixer("play");
void Logic(Action original) {
    logicTicks++;
    if (!ClockServer.TryTakeToken(0, out long token)) return;
    Fmod(live.update()); RecordingAudio.Advance(); RecordingAudio.Publish(token, false);
    BridgeRuntime.Frame++;
    ClockServer.Send($"F {token} {BridgeRuntime.Frame} 0");
}
void Attach() {
    owner = Guid.NewGuid();
    oldContext = ReloadFixture.Attach(owner, Logic, request => IpcServer.Send(Reply.Ok(request.Id)),
        text => { Bridge.ImportReloadState(text); imports++; });
}
Attach(); BridgeRuntime.Active = true;
Scene? reloadSceneOverride = null;
using var reload = new Hook(typeof(Everest.Loader).GetMethod("ReloadMod", flags)!, (Action<EverestModuleMetadata>)(_ => {
    if (owner != Guid.Empty) { BridgeRuntime.Detach(owner); owner = Guid.Empty; }
    if (failReload) throw new InvalidOperationException("Injected loader failure");
    Attach();
    if (reloadSceneOverride != null) Scene(reloadSceneOverride);
    else if (!deferScene) Scene(NewLevel());
    if (wrongSlot) SaveData.Instance.FileSlot = 11;
}));
void Pump() => BridgeRuntime.Tick(() => originalTicks++, () => originalTicks++);
using var ipc = new TcpClient(); ipc.Connect(IPAddress.Loopback, ipcPort);
using var stream = ipc.GetStream(); using var reader = new StreamReader(stream, Encoding.UTF8, leaveOpen: true);
using var writer = new StreamWriter(stream, new UTF8Encoding(false), leaveOpen: true) { AutoFlush = true };
using var clock = new TcpClient(); clock.Connect(IPAddress.Loopback, clockPort);
using var clockReader = new StreamReader(clock.GetStream(), Encoding.UTF8, leaveOpen: true);
using var clockWriter = new StreamWriter(clock.GetStream(), new UTF8Encoding(false), leaveOpen: true) { AutoFlush = true };
long requestId = 0;
long nextToken = 1;
double[] receivedEnergy = new double[2];
using var audioMapping = MemoryMappedFile.OpenExisting($@"Local\CelesteAIRecordAudio-{audioPort}");
using var audioView = audioMapping.CreateViewAccessor();
JsonElement Call(string command, string? lease = null, bool? expectSceneChange = null) {
    var request = new Dictionary<string, object?> { ["id"] = ++requestId, ["cmd"] = command };
    if (lease != null) request["lease"] = lease;
    if (expectSceneChange.HasValue) request["expect_scene_change"] = expectSceneChange.Value;
    writer.WriteLine(JsonSerializer.Serialize(request));
    long until = Environment.TickCount64 + 5000;
    while (ipc.Available == 0 && Environment.TickCount64 < until) { Pump(); Thread.Sleep(1); }
    Check(ipc.Available > 0, "IPC reply timeout: " + command);
    using var document = JsonDocument.Parse(reader.ReadLine()!);
    return document.RootElement.Clone();
}
string ClockToken(long token, bool held) {
    clockWriter.WriteLine("T " + token);
    long until = Environment.TickCount64 + 5000;
    while (clock.Available == 0 && Environment.TickCount64 < until) { Pump(); Thread.Sleep(1); }
    Check(clock.Available > 0, "Clock reply timeout");
    string response = clockReader.ReadLine()!;
    Check(response.StartsWith((held ? "N " : "F ") + token), "Clock token mismatch: " + response);
    if (!held) {
        const int slotSize = 64 + 2 * 800 * 2 * 4;
        long offset = 64L + token % 1024 * slotSize;
        Check(audioView.ReadInt64(offset) == token, "Actual PCM receiver lost its clock-token identity");
        foreach (int track in new[] { 0, 1 }) {
            var bytes = new byte[6400]; audioView.ReadArray(offset + 64 + track * 6400, bytes, 0, bytes.Length);
            var samples = new float[1600]; Buffer.BlockCopy(bytes, 0, samples, 0, bytes.Length);
            receivedEnergy[track] += samples.Sum(x => (double)x*x);
        }
    }
    return response;
}
try {
    Check(!BridgeRuntime.Ready, "Native fixture unexpectedly began with an installed freeze gate");
    Check(!Call("activate").GetProperty("ok").GetBoolean(), "Core accepted activate before freeze gate readiness");
    Check(!Call("reload_prepare").GetProperty("ok").GetBoolean(), "Core accepted prepare before freeze gate readiness");
    using var engineGate = new ILHook(typeof(Engine).GetMethod("Update", BindingFlags.Instance | BindingFlags.NonPublic)!,
        typeof(FreezeGate).GetMethod("EngineGate", flags)!.CreateDelegate<ILContext.Manipulator>());
    Check(BridgeRuntime.Ready, "Production Engine IL did not enable the resident freeze gate");
    // The Level deliberately has no player and is in a cutscene: these are not
    // resident-resource concerns and must not require bypassing any core rule.
    originalScene.InCutscene = true;
    JsonElement unrestricted = Call("reload_prepare");
    Check(unrestricted.GetProperty("stage").GetString() == "prepared" && BridgeRuntime.HoldingReload,
        "Resident core imposed a gameplay rule: " + unrestricted);
    Check(Call("reload_abort", unrestricted.GetProperty("lease").GetString()).GetProperty("stage").GetString() == "aborted",
        "Prepared cutscene hold could not abort");
    originalScene.InCutscene = false;
    string identitySnapshot = Bridge.ExportReloadState();
    foreach (string identityField in new[] { "slot", "room", "time" }) {
        string oldRoom = session.Level; long oldTime = session.Time; int oldSlot = SaveData.Instance.FileSlot;
        if (identityField == "slot") SaveData.Instance.FileSlot++;
        if (identityField == "room") session.Level = "wrong-room";
        if (identityField == "time") session.Time++;
        bool rejected = false;
        try { Bridge.ImportReloadState(identitySnapshot); } catch (InvalidOperationException) { rejected = true; }
        Check(rejected, "Real bridge import accepted changed " + identityField);
        session.Level = oldRoom; session.Time = oldTime; SaveData.Instance.FileSlot = oldSlot;
    }
    Bridge.ImportReloadState(identitySnapshot);
    var legacy = System.Text.Json.Nodes.JsonNode.Parse(identitySnapshot)!.AsObject(); legacy.Remove("identity");
    Bridge.ImportReloadState(legacy.ToJsonString());
    for (int i = 0; i < 30; i++) ClockToken(nextToken++, false);
    Check(receivedEnergy.All(e => e > 0), "Actual dual PCM receiver has no audible baseline");
    for (int cycle = 0; cycle < 3; cycle++) {
        WeakReference previous = oldContext!;
        JsonElement prepared = Call("reload_prepare");
        Check(prepared.GetProperty("ok").GetBoolean(), prepared.ToString());
        string lease = prepared.GetProperty("lease").GetString()!;
        long before = BridgeRuntime.Frame, audioBefore = RecordingAudio.RenderedFrames;
        int? musicBefore = RecordingAudio.PositionFor(music), fullMusicBefore = RecordingAudio.FullPositionFor(music);
        int ticksBefore = logicTicks;
        for (int i = 0; i < 4; i++) ClockToken(nextToken++, true);
        Check(!Call("step").GetProperty("ok").GetBoolean(), "Held core accepted a new game action");
        JsonElement held = Call("hello");
        Check(held.GetProperty("ok").GetBoolean() && held.GetProperty("reload_hold").GetBoolean() &&
            held.GetProperty("reload").GetProperty("stage").GetString() == "prepared" &&
            held.GetProperty("reload").GetProperty("abortable").GetBoolean(), "Held hello did not report an abortable hold: " + held);
        Check(!Call("reload_finish", "not-the-lease").GetProperty("ok").GetBoolean(), "Mismatched lease resumed the core");
        Check(BridgeRuntime.ShouldHaltEngineUpdate(), "Hold does not freeze game");
        Check(BridgeRuntime.Frame == before && RecordingAudio.RenderedFrames == audioBefore && logicTicks == ticksBefore,
            "Held reload advanced payload, video or audio");
        Check(RecordingAudio.PositionFor(music) == musicBefore && RecordingAudio.FullPositionFor(music) == fullMusicBefore,
            "Held reload advanced an independent music timeline");
        JsonElement committed = Call("reload_commit", lease);
        Check(committed.GetProperty("ok").GetBoolean(), committed.ToString());
        Pump();
        JsonElement status = Call("reload_status", lease);
        Check(status.GetProperty("stage").GetString() == "ready", status.ToString());
        Check(ReferenceEquals(full, AudioMixer("full")) && ReferenceEquals(play, AudioMixer("play")), "Audio mixers restarted");
        Check(RecordingAudio.RenderedFrames == audioBefore, "Reload mixed phantom samples");
        Check(Call("reload_finish", lease).GetProperty("ok").GetBoolean(), "Finish rejected");
        double[] energyBefore = (double[])receivedEnergy.Clone();
        for (int i = 0; i < 5; i++) ClockToken(nextToken++, false);
        Check(receivedEnergy.Zip(energyBefore).All(pair => pair.First > pair.Second), "Reload silenced a recorded track");
        for (int i = 0; i < 3 && previous.IsAlive; i++) { GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect(); }
        Check(!previous.IsAlive, "Old collectible payload context remains rooted");
    }
    string abortLease = Call("reload_prepare").GetProperty("lease").GetString()!;
    long abortFrame = BridgeRuntime.Frame;
    ClockToken(nextToken++, true);
    Check(!Call("reload_abort", "not-the-lease").GetProperty("ok").GetBoolean(), "Mismatched lease released the hold");
    JsonElement aborted = Call("reload_abort", abortLease);
    Check(aborted.GetProperty("ok").GetBoolean() && aborted.GetProperty("stage").GetString() == "aborted" && !BridgeRuntime.HoldingReload,
        "Prepared hold was not released: " + aborted);
    Check(BridgeRuntime.Frame == abortFrame && imports == 3, "Abort advanced the game or imported state");
    ClockToken(nextToken++, false);
    string failedLease = Call("reload_prepare").GetProperty("lease").GetString()!;
    failReload = true;
    Check(Call("reload_commit", failedLease).GetProperty("stage").GetString() == "failed", "Loader failure not reported");
    ClockToken(nextToken++, true);
    Check(BridgeRuntime.HoldingReload && BridgeRuntime.ShouldHaltEngineUpdate(), "Failure released hold");
    Check(!Call("reload_abort", failedLease).GetProperty("ok").GetBoolean() && BridgeRuntime.HoldingReload,
        "A committed reload whose payload was unloaded was released by abort");
    failReload = false;
    Call("reload_commit", failedLease); Pump();
    Check(Call("reload_status", failedLease).GetProperty("stage").GetString() == "ready", "Same-lease recovery failed");
    Check(Call("reload_finish", failedLease).GetProperty("ok").GetBoolean(), "Recovered lease could not finish");
    ClockToken(nextToken++, false);
    string mismatchLease = Call("reload_prepare").GetProperty("lease").GetString()!;
    wrongSlot = true;
    Call("reload_commit", mismatchLease); Pump();
    JsonElement mismatch = Call("reload_status", mismatchLease);
    Check(mismatch.GetProperty("stage").GetString() == "failed" &&
        mismatch.GetProperty("error").GetString()!.Contains("identity"), "Wrong save identity was accepted: " + mismatch);
    ClockToken(nextToken++, true);
    wrongSlot = false; SaveData.Instance.FileSlot = 10;
    // The failed attempt already rebuilt the room. The retry must wait for its own rebuild.
    deferScene = true;
    Call("reload_commit", mismatchLease); Pump();
    Check(Call("reload_status", mismatchLease).GetProperty("stage").GetString() == "reloading",
        "Retry became ready before its own room reload");
    deferScene = false; Scene(NewLevel()); Pump();
    Check(Call("reload_status", mismatchLease).GetProperty("stage").GetString() == "ready", "Identity retry did not recover");
    Check(Call("reload_finish", mismatchLease).GetProperty("ok").GetBoolean(), "Identity recovery finish rejected");
    ClockToken(nextToken++, false);
    Check(imports == 5 && ipc.Connected && clock.Connected && RecordingAudio.Enabled, "Resident session ownership changed");
    // A preserve-scene handoff is generic: Runtime never interprets this game's
    // results type or session. The real Bridge owns those identity checks.
    var results = (AreaComplete)RuntimeHelpers.GetUninitializedObject(typeof(AreaComplete));
    results.Session = session; Scene(results); deferScene = true;
    string resultsSnapshot = Bridge.ExportReloadState();
    Scene(NewLevel()); bool wrongKind = false;
    try { Bridge.ImportReloadState(resultsSnapshot); } catch (InvalidOperationException) { wrongKind = true; }
    Check(wrongKind, "Results snapshot accepted a different scene kind");
    Scene(results);
    long resultsFrame = BridgeRuntime.Frame, resultsAudio = RecordingAudio.RenderedFrames;
    string resultsLease = Call("reload_prepare").GetProperty("lease").GetString()!;
    Call("reload_commit", resultsLease, expectSceneChange: false); Pump();
    Check(Call("reload_status", resultsLease).GetProperty("stage").GetString() == "ready",
        "Same-scene reload waited for an unnecessary scene replacement");
    Check(ReferenceEquals(Engine.Scene, results) && BridgeRuntime.Frame == resultsFrame &&
        RecordingAudio.RenderedFrames == resultsAudio, "Results reload advanced or replaced its scene");
    Call("reload_finish", resultsLease);
    string changedLease = Call("reload_prepare").GetProperty("lease").GetString()!;
    var otherResults = (AreaComplete)RuntimeHelpers.GetUninitializedObject(typeof(AreaComplete));
    otherResults.Session = session; reloadSceneOverride = otherResults;
    Call("reload_commit", changedLease, expectSceneChange: false); Pump();
    Check(Call("reload_status", changedLease).GetProperty("stage").GetString() == "failed",
        "Preserve-scene handoff accepted a different object with the same session");
    reloadSceneOverride = null;
    Call("reload_commit", changedLease, expectSceneChange: false); Pump();
    Check(Call("reload_status", changedLease).GetProperty("stage").GetString() == "failed",
        "Preserve-scene retry replaced the original lease identity with the wrong scene");
    Check(!Call("reload_commit", changedLease, expectSceneChange: true).GetProperty("ok").GetBoolean(),
        "Retry changed the scene completion contract");
    reloadSceneOverride = null; Scene(results);
    Call("reload_commit", changedLease, expectSceneChange: false); Pump();
    Check(Call("reload_status", changedLease).GetProperty("stage").GetString() == "ready",
        "Preserve-scene retry failed");
    Call("reload_finish", changedLease);
    Console.WriteLine("PASS: generic preserve-scene handoff retains actual AreaComplete, rejects another same-session scene and retries without frame/audio advance");
    Console.WriteLine("PASS: same real IPC/clock connections, token accounting, audio mixers and recording frame sequence survive three payload reloads");
    Console.WriteLine("PASS: failed loader remains frozen; old payload collectible contexts are released");
    Console.WriteLine("PASS: resident core imposes no player/cutscene rules; real bridge imports verify slot/room/time and accept legacy schema-1 snapshots");
    Console.WriteLine("PASS: held hello is read-only; only an uncommitted hold can abort; a retry waits for its own scene replacement");
    Console.WriteLine("BOUNDARY: Everest loader is replaced here; stock Loader.ReloadMod and room rebuild require separate isolated game acceptance");
} finally {
    BridgeRuntime.Shutdown(); IpcServer.Stop(); ClockServer.Stop(); RecordingAudio.Unload(); live.release();
}

sealed class FixtureModule : EverestModule { public override void Load() { } public override void Unload() { } }
public static class ReloadFixture {
    public static string Export() => Bridge.ExportReloadState();
    [MethodImpl(MethodImplOptions.NoInlining)]
    public static WeakReference Attach(Guid owner, Action<Action> tick, Action<Request> enqueue, Action<string> import) {
        var context = new FixtureContext();
        var assembly = context.LoadFromAssemblyPath(Assembly.GetExecutingAssembly().Location);
        var export = assembly.GetType(nameof(ReloadFixture))!.GetMethod(nameof(Export))!.CreateDelegate<Func<string>>();
        BridgeRuntime.Attach(owner, 1, "test", tick, enqueue, () => true, export, import, () => { });
        var weak = new WeakReference(context);
        context.Unload();
        return weak;
    }
}
sealed class FixtureContext : AssemblyLoadContext {
    public FixtureContext() : base(isCollectible: true) { }
    protected override Assembly? Load(AssemblyName name) => Default.Assemblies.FirstOrDefault(a => a.GetName().Name == name.Name);
}
