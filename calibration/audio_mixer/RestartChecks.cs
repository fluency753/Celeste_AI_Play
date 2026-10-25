using System.Reflection;
using CelesteAI.Bridge;
using FMOD;
using FMOD.Studio;

internal static class RestartChecks {
    private static void Check(RESULT result) { if (result != RESULT.OK) throw new Exception(result.ToString()); }
    private static object Mixer(string name) => typeof(RecordingAudio).GetField(name, BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
    private static double Energy(string name) => ((float[])Mixer(name).GetType().GetField("Pcm")!.GetValue(Mixer(name))!).Sum(x => (double)x*x);
    private static void Flush(FMOD.Studio.System live) { Check(live.update()); Check(live.flushCommands()); }

    public static void Run(FMOD.Studio.System live, string game, EventInstance music) {
        Check(music.setVolume(0)); // BGM must not conceal missing effects.
        Check(live.getBus("bus:/", out var master));
        Check(live.getBus("bus:/gameplay_sfx", out var sfx));
        Check(live.getEvent("event:/char/madeline/dash_red_right", out var dash));
        Check(live.getEvent("event:/char/dialogue/madeline", out var dialogue));
        for (int cycle = 0; cycle < 3; cycle++) {
            // A released voice already exists when the controller stops/recreates the mixer.
            Check(dialogue.createInstance(out var voice));
            Check(voice.setParameterValue("dialogue_portrait", 1));
            Check(voice.setParameterValue("dialogue_end", 0));
            Check(voice.start()); Check(voice.release());
            Check(master.setPaused(true)); Check(sfx.setPaused(true)); Flush(live);
            RecordingAudio.Stop();
            if (cycle == 1) {
                // Also cover attaching hooks after the source was already frozen.
                ((System.Collections.IDictionary)typeof(RecordingAudio).GetField("BusPauses", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!).Clear();
            }
            RecordingAudio.Start(live, Path.Combine(game, "Content", "FMOD", "Desktop"), 47876);
            Check(master.setPaused(false)); Flush(live); RecordingAudio.Advance();
            var system = (FMOD.Studio.System)Mixer("full").GetType().GetField("studio", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(Mixer("full"))!;
            if (cycle != 1) {
                Check(system.getBus("bus:/gameplay_sfx", out var stillPaused)); Check(stillPaused.getPaused(out bool paused));
                if (!paused) throw new Exception("An explicitly paused child bus was unpaused with its parent");
            }
            Check(sfx.setPaused(false));
            // Existing released voice must receive its new speech parameter after reconnect.
            Check(voice.setParameterValue("dialogue_end", 0));
            double full = 0, play = 0;
            for (int frame = 0; frame < 90; frame++) {
                live.update(); if (Environment.GetEnvironmentVariable("ASYNC_SOURCE") == "1") Thread.Sleep(1);
                RecordingAudio.Advance(); full += Energy("full"); play += Energy("play");
            }
            if (full < 1 || play < 1) {
                foreach (var path in new[] { "bus:/", "bus:/gameplay_sfx", "bus:/ui_sfx", "bus:/music" }) {
                    if (system.getBus(path, out var cb) == RESULT.OK && live.getBus(path, out var lb) == RESULT.OK) { cb.getPaused(out var cp); lb.getPaused(out var lp); Console.WriteLine($"AFTER {path} live={lp} copy={cp}"); }
                }
                throw new Exception($"Restart {cycle}: existing dialogue is silent: {full}/{play}");
            }
            Console.WriteLine($"PASS: restart {cycle} existing released dialogue PCM full={full:F2} play={play:F2}");
            Check(live.getBankList(out Bank[] banks));
            foreach (var bank in banks) {
                Check(bank.getBusList(out Bus[] buses));
                foreach (var bus in buses) {
                    Check(bus.getPath(out string path)); Check(bus.getPaused(out bool expected));
                    foreach (var name in new[] { "full", "play" }) {
                        var mirror = (FMOD.Studio.System)Mixer(name).GetType().GetField("studio", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(Mixer(name))!;
                        Check(mirror.getBus(path, out var actual)); Check(actual.getPaused(out bool paused));
                        if (paused != expected) throw new Exception($"Restart {cycle}: stale pause on {name} {path}");
                    }
                }
            }
            Check(voice.stop(STOP_MODE.IMMEDIATE)); Flush(live);
            for (int frame = 0; frame < 120; frame++) RecordingAudio.Advance();
            Check(dash.createInstance(out var effect)); Check(effect.setVolume(0));
            Check(effect.start()); Check(effect.release()); Check(effect.setVolume(1));
            full = play = 0;
            for (int frame = 0; frame < 90; frame++) {
                live.update(); if (Environment.GetEnvironmentVariable("ASYNC_SOURCE") == "1") Thread.Sleep(1);
                RecordingAudio.Advance(); full += Energy("full"); play += Energy("play");
            }
            if (full < 1 || play < 1) throw new Exception($"Restart {cycle}: new dash is silent: {full}/{play}");
            Console.WriteLine($"PASS: restart {cycle} new released dash PCM full={full:F2} play={play:F2}");
            if (RecordingAudio.Error != null) throw new Exception(RecordingAudio.Error);
        }
    }
}
