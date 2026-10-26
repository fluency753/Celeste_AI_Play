using CelesteAI.Bridge;
using FMOD;
using FMOD.Studio;
using System.Reflection;

internal static class SnapshotChecks {
    private static void Check(RESULT result) { if (result != RESULT.OK) throw new Exception(result.ToString()); }

    public static void Run(FMOD.Studio.System live) {
        Check(live.getBus("bus:/", out Bus root));
        Check(live.getBus("bus:/gameplay_sfx", out Bus sfx));
        Check(root.setPaused(true)); Check(sfx.setPaused(false)); Check(sfx.setVolume(.25f));
        Check(live.update()); Check(live.flushCommands());
        // The effective child pause is inherited. One immutable snapshot must seed both tracks.
        var snapshot = RecordingMixSnapshot.Capture(live, new Dictionary<IntPtr, bool>());
        Check(root.setPaused(false)); Check(sfx.setPaused(true)); Check(sfx.setVolume(.75f));
        Check(live.update()); Check(live.flushCommands());
        var copying = typeof(RecordingAudio).GetField("copying", BindingFlags.NonPublic | BindingFlags.Static)!;
        bool previous = (bool)copying.GetValue(null)!;
        copying.SetValue(null, true); // Production Start applies snapshots while copy hooks are suppressed.
        try {
        foreach (string name in new[] { "full", "play" }) {
            var mixer = typeof(RecordingAudio).GetField(name, BindingFlags.NonPublic | BindingFlags.Static)!.GetValue(null)!;
            var system = (FMOD.Studio.System)mixer.GetType().GetField("studio", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(mixer)!;
            snapshot.ApplyTo(system);
            Check(system.update()); Check(system.flushCommands());
            Check(system.getBus("bus:/", out Bus copyRoot));
            Check(system.getBus("bus:/gameplay_sfx", out Bus copySfx));
            Check(copySfx.getVolume(out float volume, out _));
            Check(copyRoot.getPaused(out bool paused));
            if (Math.Abs(volume - .25f) > .0001f || !paused) throw new Exception("Source mutation changed the captured mix");
            Check(copyRoot.setPaused(false)); Check(system.update()); Check(system.flushCommands());
            Check(copySfx.getPaused(out paused));
            if (paused) throw new Exception("Inherited source pause became a local copy pause");
        }
        } finally { copying.SetValue(null, previous); }
        Check(root.setPaused(false)); Check(sfx.setPaused(false)); Check(sfx.setVolume(1));
        Check(live.update()); Check(live.flushCommands());
        Console.WriteLine("PASS: one immutable source mix seeds both tracks; inherited pause stays inherited");
    }
}
