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
        EventDescription desc;
        string? extra = Environment.GetEnvironmentVariable("EXTRA_RECORDING_BANK");
        Hook? hook = null;
        Bank? extraBank = null;
        liveHandle = live.getRaw();
        lookupFailure = RESULT.ERR_EVENT_NOTFOUND;
        if (extra != null) {
            // Optional real Mod-bank acceptance: assets stay outside the repository. No game connection is used.
            Check(live.loadBankFile(extra, LOAD_BANK_FLAGS.NORMAL, out extraBank));
            Check(extraBank.loadSampleData()); Check(live.flushSampleLoading());
            Check(extraBank.getEventList(out EventDescription[] events));
            desc = events.First();
        } else {
            Check(live.getEvent("event:/ui/main/button_back", out desc));
            hook = new Hook(typeof(FMOD.Studio.System).GetMethod("getEventByID")!,
                typeof(MissingEventChecks).GetMethod(nameof(OnLookup), Flags)!);
        }
        Check(desc.getID(out missingId));
        Check(desc.createInstance(out var missingEvent)); Check(missingEvent.setPaused(true)); Check(missingEvent.start());
        Check(live.update()); Check(live.flushCommands());
        try {
            CheckMissing(missingId);
            Check(missingEvent.setVolume(0.5f)); Check(missingEvent.setVolume(1));
            CheckMissing(missingId);
            Check(music.setVolume(1)); Check(music.start());
            for (int cycle = 0; cycle < 2; cycle++) {
                // A paused live event remains present while seeding a replacement recorder, as in the failure.
                RecordingAudio.Stop();
                RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876);
                CheckMissing(missingId);
                double full = 0, play = 0;
                for (int frame = 0; frame < 120; frame++) {
                    Check(live.update()); RecordingAudio.Advance();
                    full += Energy("full"); play += Energy("play");
                }
                if (full < 1 || play < 1) throw new Exception($"Missing event silenced other audio: {full}/{play}");
                CheckMissing(missingId);
            }
            if (hook != null) {
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
            try { RecordingAudio.Start(live, Path.Combine(game, "missing-recording-banks"), 47876); }
            catch (InvalidOperationException ex) when (ex.Message.Contains("ERR_FILE_NOTFOUND")) { bankRejected = true; }
            if (!bankRejected) throw new Exception("Missing required official bank was swallowed");
        } finally {
            hook?.Dispose();
            Check(missingEvent.stop(STOP_MODE.IMMEDIATE)); Check(missingEvent.release()); Check(live.update()); Check(live.flushCommands());
            extraBank?.unload();
            RecordingAudio.Stop();
            RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876);
        }
        Console.WriteLine("PASS: absent individual event is diagnosed and deduplicated; both PCM tracks and recorder restart survive; other lookup/required-bank failures remain fatal" + (extra == null ? " (lookup boundary injected)" : " (real external bank)"));
    }
}
