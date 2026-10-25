using System.Reflection;
using FMOD;
using FMOD.Studio;
using CelesteAI.Bridge;

internal static class LifecycleChecks {
    private static void Check(RESULT result) { if (result != RESULT.OK) throw new Exception(result.ToString()); }
    private static void Step(FMOD.Studio.System live) {
        live.update();
        if (Environment.GetEnvironmentVariable("ASYNC_SOURCE") == "1") Thread.Sleep(1);
        RecordingAudio.Advance();
    }

    private static EventInstance CopyOf(EventInstance instance) {
        var mixer = typeof(RecordingAudio).GetField("full", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
        var pairs = (System.Collections.IDictionary)mixer.GetType().GetField("Pairs")!.GetValue(mixer)!;
        return (EventInstance)pairs[instance.getRaw()]!.GetType().GetProperty("Copy")!.GetValue(pairs[instance.getRaw()])!;
    }

    private static object Mixer(string name) => typeof(RecordingAudio).GetField(name, BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
    private static int Tracked(string name) => ((System.Collections.IDictionary)Mixer(name).GetType().GetField("Pairs")!.GetValue(Mixer(name))!).Count;
    private static double Energy(string name) => ((float[])Mixer(name).GetType().GetField("Pcm")!.GetValue(Mixer(name))!).Sum(x => (double)x*x);

    public static void Run(FMOD.Studio.System live, EventInstance music) {
        // Game code can keep controlling a playing instance after release(): release is not stop.
        Check(live.getEvent("event:/char/madeline/dash_red_right", out var dash));
        Check(dash.createInstance(out var instance));
        Check(instance.setVolume(0)); Check(instance.start()); Check(instance.release());
        var copy = CopyOf(instance);
        Check(instance.setVolume(0.7f));
        RecordingAudio.Advance();
        Check(copy.getVolume(out float volume, out _));
        if (Math.Abs(volume - 0.7f) > 0.001f)
            throw new Exception($"Released dash lost its volume update: expected 0.7, got {volume}");
        Check(instance.stop(STOP_MODE.IMMEDIATE));
        Step(live);
        Console.WriteLine("PASS: released dash still receives volume changes");

        Check(live.getEvent("event:/char/dialogue/madeline", out var dialogue));
        Check(dialogue.createInstance(out var voice));
        Check(voice.setParameterValue("dialogue_portrait", 1));
        Check(voice.setParameterValue("dialogue_end", 0));
        Check(voice.start()); Check(voice.release());
        var voiceCopy = CopyOf(voice);
        Check(voice.setParameterValue("dialogue_end", 1));
        RecordingAudio.Advance();
        Check(voiceCopy.getParameterValue("dialogue_end", out float ended, out _));
        if (ended != 1) throw new Exception("Released dialogue lost its end parameter");
        // dialogue_end controls speech, not the lifetime of this persistent event.
        Check(voice.stop(STOP_MODE.IMMEDIATE));
        for (int i = 0; i < 120; i++) Step(live);
        if (voiceCopy.isValid()) throw new Exception("Finished dialogue copy was not reclaimed");
        Console.WriteLine("PASS: dialogue parameters are mirrored; explicit stop reclaims the voice");

        // Playback control must not regress to the live async system's older getter values.
        Check(dash.createInstance(out var controlled));
        Check(controlled.setVolume(0.4f)); Check(controlled.setPitch(0.8f));
        Check(controlled.setPaused(true)); Check(controlled.start());
        var attributes = new FMOD.Studio._3D_ATTRIBUTES {
            position = new VECTOR { x = 100, y = 50 }, forward = new VECTOR { z = 1 }, up = new VECTOR { y = 1 }
        };
        Check(controlled.set3DAttributes(attributes));
        var controlledCopy = CopyOf(controlled);
        RecordingAudio.Advance();
        Check(controlledCopy.getVolume(out volume, out _));
        Check(controlledCopy.getPitch(out float pitch, out _));
        Check(controlledCopy.getPaused(out bool paused));
        Check(controlledCopy.get3DAttributes(out var copiedAttributes));
        if (Math.Abs(volume - 0.4f) > 0.001f || Math.Abs(pitch - 0.8f) > 0.001f || !paused)
            throw new Exception("Pre-start controls were lost");
        if (copiedAttributes.position.x != 100 || copiedAttributes.position.y != 50)
            throw new Exception("3D position was not mirrored");
        Check(controlled.setPaused(false)); Check(controlled.stop(STOP_MODE.IMMEDIATE)); Check(controlled.release());
        for (int i = 0; i < 3; i++) Step(live);
        Console.WriteLine("PASS: pre-start volume, pitch and pause survive start and render");

        Check(dialogue.createInstance(out var indexed)); Check(indexed.start());
        Check(indexed.setParameterValueByIndex(0, 1));
        var indexedCopy = CopyOf(indexed);
        Check(indexedCopy.getParameterValueByIndex(0, out float indexedValue, out _));
        if (indexedValue != 1) throw new Exception("Indexed parameter was not mirrored");
        Check(indexed.setParameterValuesByIndices(new[] { 0 }, new[] { 0f }, 1));
        Check(indexedCopy.getParameterValueByIndex(0, out indexedValue, out _));
        if (indexedValue != 0) throw new Exception("Batch parameters were not mirrored");
        Check(indexed.stop(STOP_MODE.IMMEDIATE)); Check(indexed.release());
        Step(live);
        Console.WriteLine("PASS: parameter index and batch updates reach the copy immediately");

        var fullStudio = (FMOD.Studio.System)Mixer("full").GetType().GetField("studio", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(Mixer("full"))!;
        Check(live.getBus("bus:/gameplay_sfx", out var bus));
        Check(fullStudio.getBus("bus:/gameplay_sfx", out var copyBus));
        Check(bus.setVolume(0.6f)); Check(bus.setMute(true)); Step(live);
        Check(copyBus.getVolume(out volume, out _)); Check(copyBus.getMute(out bool muted));
        if (Math.Abs(volume - 0.6f) > 0.001f || !muted) throw new Exception("Bus controls were lost or overwritten");
        Check(bus.setVolume(1)); Check(bus.setMute(false));
        Check(live.getVCA("vca:/gameplay_sfx", out var vca));
        Check(fullStudio.getVCA("vca:/gameplay_sfx", out var copyVca));
        Check(vca.setVolume(0.8f)); Step(live); Check(copyVca.getVolume(out volume, out _));
        if (Math.Abs(volume - 0.8f) > 0.001f) throw new Exception("VCA control was lost or overwritten");
        Check(vca.setVolume(1));
        Console.WriteLine("PASS: 3D position, bus volume/mute and VCA settings reach the recording mixer");

        // A source loop can disappear through a bank/description-wide operation, bypassing instance.stop.
        Check(live.getEvent("event:/char/madeline/wallslide", out var loopDesc));
        Check(loopDesc.createInstance(out var loop)); Check(loop.start()); Check(loop.release());
        var loopCopy = CopyOf(loop);
        Check(loopDesc.releaseAllInstances());
        for (int i = 0; i < 6; i++) Step(live);
        if (loopCopy.isValid()) throw new Exception("Orphaned loop copy survived the end of its live owner");
        Console.WriteLine("PASS: description-wide source cleanup also retires its recording loop");

        // Check real PCM with the background music muted, so BGM cannot conceal missing effects.
        Check(music.setVolume(0));
        for (int i = 0; i < 12; i++) Step(live);
        Check(dash.createInstance(out var delayedDash)); Check(delayedDash.start()); Check(delayedDash.release());
        var delayedCopy = CopyOf(delayedDash);
        Check(dash.releaseAllInstances()); // Live wall time can finish a short sound during thinking.
        double delayedEnergy = 0;
        for (int i = 0; i < 90; i++) { Step(live); delayedEnergy += Energy("play"); }
        if (delayedEnergy < 1 || delayedCopy.isValid())
            throw new Exception("One-shot tail was truncated or was not eventually reclaimed");
        Console.WriteLine("PASS: short effect keeps its recording tail after the live source has ended");
        Check(dash.createInstance(out var audibleDash));
        Check(audibleDash.setVolume(0)); Check(audibleDash.start()); Check(audibleDash.release());
        Check(audibleDash.setVolume(1));
        double fullEnergy = 0, playEnergy = 0;
        for (int i = 0; i < 90; i++) {
            Step(live);
            fullEnergy += Energy("full"); playEnergy += Energy("play");
        }
        if (fullEnergy < 1 || playEnergy < 1) throw new Exception("Dash did not reach both PCM tracks");
        Console.WriteLine($"PASS: dash PCM full={fullEnergy:F2} play={playEnergy:F2} without BGM");

        Check(dialogue.createInstance(out var audibleVoice));
        Check(audibleVoice.start()); Check(audibleVoice.release());
        Check(audibleVoice.setParameterValue("dialogue_portrait", 1));
        Check(audibleVoice.setParameterValue("dialogue_end", 0));
        fullEnergy = playEnergy = 0;
        for (int i = 0; i < 90; i++) {
            Step(live);
            fullEnergy += Energy("full"); playEnergy += Energy("play");
        }
        if (fullEnergy < 1 || playEnergy < 1) throw new Exception("Dialogue did not reach both PCM tracks");
        Check(audibleVoice.stop(STOP_MODE.IMMEDIATE));
        Console.WriteLine($"PASS: dialogue PCM full={fullEnergy:F2} play={playEnergy:F2} without BGM");

        int baseline = Tracked("full");
        var timer = System.Diagnostics.Stopwatch.StartNew();
        // Alternate parameter-driven voices and short dashes over many batches; no accumulated voices.
        for (int batch = 0; batch < 100; batch++) {
            var voices = new List<EventInstance>();
            for (int i = 0; i < 20; i++) {
                Check(dialogue.createInstance(out var v)); Check(v.start()); Check(v.release());
                Check(v.setParameterValue("dialogue_portrait", 1)); Check(v.setParameterValue("dialogue_end", 0));
                voices.Add(v);
            }
            Step(live);
            foreach (var v in voices) { Check(v.setParameterValue("dialogue_end", 1)); Check(v.stop(STOP_MODE.IMMEDIATE)); }
            for (int i = 0; i < 5; i++) Step(live);
            if (Tracked("full") > baseline || Tracked("play") > baseline)
                throw new Exception($"Voices accumulated in batch {batch}");
        }
        Check(music.setVolume(1));
        Console.WriteLine($"PASS: 2000 dialogue instances reclaimed, tracked={Tracked("full")}, elapsed={timer.Elapsed.TotalMilliseconds:F1} ms");
        for (int batch = 0; batch < 100; batch++) {
            for (int i = 0; i < 20; i++) {
                Check(loopDesc.createInstance(out var v)); Check(v.start()); Check(v.release());
            }
            Check(loopDesc.releaseAllInstances());
            for (int i = 0; i < 6; i++) Step(live);
            if (Tracked("full") > baseline || Tracked("play") > baseline)
                throw new Exception($"Orphaned loops accumulated in batch {batch}");
        }
        Console.WriteLine($"PASS: 2000 orphaned loops reclaimed, tracked={Tracked("full")}");
        // The reusable DSP buffer must not replay old samples when FMOD supplies a null (silent) input.
        var mixer = Mixer("full");
        var read = mixer.GetType().GetMethod("Read", BindingFlags.NonPublic | BindingFlags.Instance)!;
        IntPtr input = System.Runtime.InteropServices.Marshal.AllocHGlobal(64);
        IntPtr output = System.Runtime.InteropServices.Marshal.AllocHGlobal(64);
        try {
            System.Runtime.InteropServices.Marshal.Copy(Enumerable.Repeat(0.5f, 16).ToArray(), 0, input, 16);
            read.Invoke(mixer, new object[] { new DSP_STATE(), input, output, 8u, 2, 2 });
            read.Invoke(mixer, new object[] { new DSP_STATE(), IntPtr.Zero, output, 8u, 2, 2 });
            var silent = new float[16];
            System.Runtime.InteropServices.Marshal.Copy(output, silent, 0, 16);
            if (silent.Any(x => x != 0)) throw new Exception("DSP reused stale samples for silent input");
        } finally {
            System.Runtime.InteropServices.Marshal.FreeHGlobal(input);
            System.Runtime.InteropServices.Marshal.FreeHGlobal(output);
        }
        if (RecordingAudio.Error != null) throw new Exception(RecordingAudio.Error);
        Console.WriteLine("PASS: null DSP input stays silent after buffer reuse");
    }
}
