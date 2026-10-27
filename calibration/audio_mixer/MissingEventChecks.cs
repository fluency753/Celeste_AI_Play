using System.Reflection;
using CelesteAI.Bridge;
using FMOD;
using FMOD.Studio;
using MonoMod.RuntimeDetour;

internal static class MissingEventChecks {
    private static readonly BindingFlags Flags = BindingFlags.Static | BindingFlags.NonPublic;
    private static IntPtr liveHandle;
    private static Guid missingId;
    private static RESULT lookupFailure = RESULT.ERR_EVENT_NOTFOUND;
    private delegate RESULT Lookup(FMOD.Studio.System self, Guid id, out EventDescription description);
    private delegate RESULT LoadFile(FMOD.Studio.System self, string file, LOAD_BANK_FLAGS flags, out Bank bank);
    private delegate RESULT LoadMemory(FMOD.Studio.System self, byte[] bytes, LOAD_BANK_FLAGS flags, out Bank bank);
    private static RESULT OnFile(LoadFile orig, FMOD.Studio.System self, string file, LOAD_BANK_FLAGS flags, out Bank bank) {
        if (self.getRaw() != liveHandle) { bank = null!; return RESULT.ERR_FILE_NOTFOUND; }
        return orig(self, file, flags, out bank);
    }
    private static RESULT OnMemory(LoadMemory orig, FMOD.Studio.System self, byte[] bytes, LOAD_BANK_FLAGS flags, out Bank bank) {
        if (self.getRaw() != liveHandle) { bank = null!; return RESULT.ERR_FILE_NOTFOUND; }
        return orig(self, bytes, flags, out bank);
    }
    private static RESULT OnLookup(Lookup orig, FMOD.Studio.System self, Guid id, out EventDescription description) {
        if (self.getRaw() != liveHandle && id == missingId) { description = null!; return lookupFailure; }
        return orig(self, id, out description);
    }
    private static void Check(RESULT result) { if (result != RESULT.OK) throw new Exception(result.ToString()); }
    private static object Mixer(string name) => typeof(RecordingAudio).GetField(name, Flags)!.GetValue(null)!;
    private static double Energy(string name) => ((float[])Mixer(name).GetType().GetField("Pcm")!.GetValue(Mixer(name))!).Sum(x => (double)x*x);
    private static void CheckMissing(Guid id) {
        foreach (string name in new[] { "full", "play" }) {
            object mixer = Mixer(name);
            var missing = (System.Collections.IDictionary)mixer.GetType().GetField("missingEvents", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(mixer)!;
            if (missing.Count != 1 || !missing.Contains(id) || !missing[id]!.ToString()!.Contains(id.ToString()))
                throw new Exception($"{name}: missing event not deduplicated or GUID diagnostic lost");
        }
        if (RecordingAudio.Error != null) throw new Exception("A missing event failed the recording: " + RecordingAudio.Error);
    }

    public static void Run(FMOD.Studio.System live, string game, EventInstance music) {
        liveHandle = live.getRaw();
        lookupFailure = RESULT.ERR_EVENT_NOTFOUND;
        Check(live.getEvent("event:/ui/main/button_back", out var desc));
        using var hook = new Hook(typeof(FMOD.Studio.System).GetMethod("getEventByID")!,
            typeof(MissingEventChecks).GetMethod(nameof(OnLookup), Flags)!);
        Check(desc.getID(out missingId));
        Check(desc.createInstance(out var missingEvent)); Check(missingEvent.setPaused(true)); Check(missingEvent.start());
        Check(live.update()); Check(live.flushCommands());
        try {
            CheckMissing(missingId);
            Check(missingEvent.setVolume(0.5f)); Check(missingEvent.setVolume(1));
            CheckMissing(missingId);
            Check(music.setVolume(1)); Check(music.start());
            Check(live.getEvent("event:/char/madeline/dash_red_right", out var audible));
            for (int cycle = 0; cycle < 2; cycle++) {
                // A paused live event remains present while seeding a replacement recorder, as in the failure.
                RecordingAudio.Stop();
                RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876);
                CheckMissing(missingId);
                double full = 0, play = 0;
                for (int frame = 0; frame < 120; frame++) {
                    // An explicit unrelated one-shot proves PCM survives, without
                    // relying on a music intro's wall-clock/seek scheduling.
                    if (frame % 20 == 0) { Check(audible.createInstance(out var dash)); Check(dash.start()); Check(dash.release()); }
                    Check(live.update()); RecordingAudio.Advance();
                    full += Energy("full"); play += Energy("play");
                }
                if (full < 1 || play < 1) {
                    music.getTimelinePosition(out int sourceTime); music.getPaused(out bool sourcePaused); music.getVolume(out float sourceVolume, out _);
                    Console.WriteLine($"MISSING DEBUG source time={sourceTime} paused={sourcePaused} volume={sourceVolume}");
                    foreach (string name in new[] { "full", "play" }) {
                        var studio = (FMOD.Studio.System)Mixer(name).GetType().GetField("studio", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(Mixer(name))!;
                        var pairs = (System.Collections.IDictionary)Mixer(name).GetType().GetField("Pairs")!.GetValue(Mixer(name))!;
                        var pair = pairs[music.getRaw()]!;
                        var copy = (EventInstance)pair.GetType().GetProperty("Copy")!.GetValue(pair)!;
                        copy.getTimelinePosition(out int copyTime); copy.getPaused(out bool copyPaused); copy.getVolume(out float copyVolume, out _);
                        Console.WriteLine($"MISSING DEBUG {name} time={copyTime} paused={copyPaused} volume={copyVolume}");
                        foreach (string path in new[] { "bus:/", "bus:/music" }) {
                            studio.getBus(path, out var bus); bus.getPaused(out bool paused); bus.getVolume(out float volume, out _);
                            Console.WriteLine($"MISSING DEBUG {name} {path} paused={paused} volume={volume}");
                        }
                    }
                    throw new Exception($"Missing event silenced other audio: {full}/{play}");
                }
                CheckMissing(missingId);
            }
            {
                lookupFailure = RESULT.ERR_INTERNAL;
                RecordingAudio.Stop();
                bool rejected = false;
                try { RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876); }
                catch (InvalidOperationException ex) when (ex.Message.Contains("ERR_INTERNAL") && ex.Message.Contains(missingId.ToString())) { rejected = true; }
                if (!rejected) throw new Exception("Unexpected event lookup failure was swallowed");
                lookupFailure = RESULT.ERR_EVENT_NOTFOUND;
            }
            RecordingAudio.Stop();
            bool bankRejected = false;
            using (var fileFailure = new Hook(typeof(FMOD.Studio.System).GetMethod("loadBankFile")!,
                    typeof(MissingEventChecks).GetMethod(nameof(OnFile), Flags)!))
            using (var memoryFailure = new Hook(typeof(FMOD.Studio.System).GetMethod("loadBankMemory")!,
                    typeof(MissingEventChecks).GetMethod(nameof(OnMemory), Flags)!))
            try { RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876); }
            catch (InvalidOperationException ex) when (ex.Message.Contains("ERR_FILE_NOTFOUND")) { bankRejected = true; }
            if (!bankRejected) throw new Exception("Missing required official bank was swallowed");
        } finally {
            hook.Dispose();
            Check(missingEvent.stop(STOP_MODE.IMMEDIATE)); Check(missingEvent.release()); Check(live.update()); Check(live.flushCommands());
            RecordingAudio.Stop();
            RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876);
        }
        Console.WriteLine("PASS: absent individual event is diagnosed and deduplicated; both PCM tracks and recorder restart survive; other lookup/required-bank failures remain fatal" + " (lookup/load boundaries injected)");
    }
}
