using System;
using System.Collections.Generic;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using FMOD;
using FMOD.Studio;
using MonoMod.RuntimeDetour;

namespace CelesteAI.Bridge;

/// <summary>
/// A second FMOD mixer with no sound device. One synchronous update renders exactly
/// 800 stereo samples; only real game frames advance it. OBS reads PCM by clock
/// token, so idle wall time, missed captures and live audio buffering cannot skew it.
/// No game banks are redistributed: this reads the installed official banks.
/// </summary>
internal static class RecordingAudio {
    private const int Samples = 800, Channels = 2, Slots = 1024, SlotSize = 64 + Samples * Channels * 4;
    private const int Magic = 0x43414941;
    private static readonly object Gate = new();
    private static readonly List<Hook> Hooks = new();
    private static readonly Dictionary<IntPtr, Pair> Pairs = new();
    private sealed record Pair(EventInstance Source, EventInstance Copy) { public bool Released; }
    [ThreadStatic] private static bool copying;
    private static FMOD.Studio.System? studio, source;
    private static FMOD.System? core;
    private static DSP? tap;
    private static MemoryMappedFile? mapping;
    private static MemoryMappedViewAccessor? view;
    private static readonly float[] Pcm = new float[Samples * Channels];
    private static readonly byte[] Bytes = new byte[Samples * Channels * 4];
    private static readonly DSP_READCALLBACK ReadCallback = Read;
    private static int captured;
    private static volatile bool capturing;
    public static string? Error { get; private set; }
    public static bool Enabled => studio != null;
    public static long RenderedFrames { get; private set; }
    public static int? PositionFor(EventInstance? live) {
        lock (Gate) {
            if (live != null && Pairs.TryGetValue(live.getRaw(), out Pair? pair) &&
                pair.Copy.getTimelinePosition(out int position) == RESULT.OK) return position;
            return null;
        }
    }

    public static void Load() {
        AddHook(nameof(EventInstance.start), nameof(OnStart));
        AddHook(nameof(EventInstance.stop), nameof(OnStop));
        AddHook(nameof(EventInstance.release), nameof(OnRelease));
        AddHook(nameof(EventInstance.triggerCue), nameof(OnCue));
        AddHook(nameof(EventInstance.setTimelinePosition), nameof(OnSeek));
        Hooks.Add(new Hook(typeof(Bus).GetMethod(nameof(Bus.stopAllEvents))!,
            typeof(RecordingAudio).GetMethod(nameof(OnBusStop), BindingFlags.NonPublic | BindingFlags.Static)!));
    }

    private static void AddHook(string method, string handler) => Hooks.Add(new Hook(
        typeof(EventInstance).GetMethod(method)!,
        typeof(RecordingAudio).GetMethod(handler, BindingFlags.NonPublic | BindingFlags.Static)!));

    public static void Unload() {
        Stop();
        foreach (Hook hook in Hooks) hook.Dispose();
        Hooks.Clear();
    }

    private static void Check(RESULT result) {
        if (result != RESULT.OK) throw new InvalidOperationException("Recording FMOD: " + result);
    }

    public static void Start(FMOD.Studio.System live, string bankDirectory, int clockPort) {
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("Recording audio requires Windows");
        lock (Gate) {
            if (Enabled) throw new InvalidOperationException("Recording audio is already running");
            Stop();
            copying = true;
            try {
                Error = null;
                RenderedFrames = 0;
                source = live;
                Check(FMOD.Studio.System.create(out studio));
                Check(studio.getLowLevelSystem(out core));
                Check(core.setOutput(OUTPUTTYPE.NOSOUND_NRT));
                Check(core.setSoftwareFormat(48000, SPEAKERMODE.STEREO, 0));
                Check(core.setDSPBufferSize(Samples, 2));
                Check(studio.initialize(256, FMOD.Studio.INITFLAGS.SYNCHRONOUS_UPDATE, FMOD.INITFLAGS.NORMAL, IntPtr.Zero));
                Check(core.getDSPBufferSize(out uint size, out _));
                if (size != Samples) throw new InvalidOperationException("FMOD changed the recording block size");
                foreach (string name in new[] { "Master Bank.bank", "Master Bank.strings.bank", "music.bank", "dlc_music.bank", "sfx.bank", "dlc_sfx.bank", "ui.bank" }) {
                    Check(studio.loadBankFile(Path.Combine(bankDirectory, name), LOAD_BANK_FLAGS.NORMAL, out Bank bank));
                    Check(bank.loadSampleData());
                }
                Check(studio.flushSampleLoading());
                Check(live.getBankList(out Bank[] banks));
                foreach (Bank bank in banks) {
                    if (bank.getEventList(out EventDescription[] events) != RESULT.OK) continue;
                    foreach (EventDescription desc in events) {
                        if (desc.getInstanceList(out EventInstance[] instances) != RESULT.OK) continue;
                        foreach (EventInstance instance in instances) {
                            if (instance.getPlaybackState(out PLAYBACK_STATE state) == RESULT.OK && state != PLAYBACK_STATE.STOPPED)
                                MirrorStart(instance, seed: true);
                        }
                    }
                }
                // Finish bank/command setup before installing the capture tap. Those
                // operations may mix extra blocks, which must never enter the movie.
                Check(studio.flushCommands());
                var description = new DSP_DESCRIPTION {
                    pluginsdkversion = 0x00011000,
                    name = "AI recording tap".PadRight(32, '\0').ToCharArray(),
                    version = 1, numinputbuffers = 1, numoutputbuffers = 1, read = ReadCallback,
                };
                Check(core.createDSP(ref description, out tap));
                Check(tap.setChannelFormat(CHANNELMASK.STEREO, Channels, SPEAKERMODE.STEREO));
                Check(core.getMasterChannelGroup(out ChannelGroup master));
                Check(master.addDSP(0, tap));
                mapping = MemoryMappedFile.CreateOrOpen($"Local\\CelesteAIRecordAudio-{clockPort}", 64L + Slots * SlotSize);
                view = mapping.CreateViewAccessor();
                view.Write(0, 0); // invalidate an old mapping before resetting slots
                for (int i = 0; i < Slots; i++) view.Write(64L + i * SlotSize, 0L);
                view.Write(4, 1); view.Write(8, 48000); view.Write(12, Channels);
                view.Write(16, Samples); view.Write(20, Slots); view.Write(24, SlotSize);
                Thread.MemoryBarrier(); view.Write(0, Magic);
                Log.Info("independent recording audio ready (48 kHz stereo, no sound device)");
            } catch {
                Stop();
                throw;
            } finally { copying = false; }
        }
    }

    public static void Stop() {
        lock (Gate) {
            copying = true;
            try {
                capturing = false;
                studio?.release();
                studio = null; source = null; core = null; tap = null;
                Pairs.Clear();
                view?.Dispose(); view = null;
                mapping?.Dispose(); mapping = null;
            } finally { copying = false; }
        }
    }

    private static void MirrorStart(EventInstance instance, bool seed = false) {
        if (studio == null) return;
        IntPtr key = instance.getRaw();
        if (!Pairs.TryGetValue(key, out Pair? pair) || pair.Released || !pair.Copy.isValid()) {
            Check(instance.getDescription(out EventDescription desc));
            Check(desc.getID(out Guid id));
            Check(studio.getEventByID(id, out EventDescription copyDesc));
            Check(copyDesc.createInstance(out EventInstance copy));
            pair = new Pair(instance, copy);
            Pairs[key] = pair;
        }
        Sync(pair);
        Check(pair.Copy.start());
        if (seed && instance.getTimelinePosition(out int position) == RESULT.OK)
            pair.Copy.setTimelinePosition(position);
    }

    private static void Sync(Pair pair) {
        EventInstance a = pair.Source, b = pair.Copy;
        if (!a.isValid()) return;
        if (a.getVolume(out float volume, out _) == RESULT.OK) b.setVolume(volume);
        if (a.getPitch(out float pitch, out _) == RESULT.OK) b.setPitch(pitch);
        if (a.getPaused(out bool paused) == RESULT.OK) b.setPaused(paused);
        if (a.get3DAttributes(out FMOD.Studio._3D_ATTRIBUTES attributes) == RESULT.OK) b.set3DAttributes(attributes);
        if (a.getParameterCount(out int count) == RESULT.OK)
            for (int i = 0; i < count; i++)
                if (a.getParameterValueByIndex(i, out float value, out _) == RESULT.OK)
                    b.setParameterValueByIndex(i, value);
    }

    private static void SyncMix() {
        if (source == null || studio == null) return;
        if (source.getListenerAttributes(0, out FMOD.Studio._3D_ATTRIBUTES listener) == RESULT.OK)
            studio.setListenerAttributes(0, listener);
        if (source.getBankList(out Bank[] banks) != RESULT.OK) return;
        foreach (Bank bank in banks) {
            if (bank.getVCAList(out VCA[] vcas) == RESULT.OK)
                foreach (VCA vca in vcas)
                    if (vca.getID(out Guid id) == RESULT.OK && studio.getVCAByID(id, out VCA copy) == RESULT.OK &&
                        vca.getVolume(out float volume, out _) == RESULT.OK) copy.setVolume(volume);
            if (bank.getBusList(out Bus[] buses) != RESULT.OK) continue;
            foreach (Bus bus in buses) {
                if (bus.getID(out Guid id) != RESULT.OK || studio.getBusByID(id, out Bus copy) != RESULT.OK) continue;
                if (bus.getVolume(out float volume, out _) == RESULT.OK) copy.setVolume(volume);
                if (bus.getMute(out bool mute) == RESULT.OK) copy.setMute(mute);
                if (bus.getPaused(out bool paused) == RESULT.OK) copy.setPaused(paused);
            }
        }
    }

    public static void Advance() {
        lock (Gate) {
            if (studio == null || Error != null) return;
            copying = true;
            try {
                foreach (var entry in Pairs.ToArray()) {
                    if (!entry.Value.Copy.isValid()) { Pairs.Remove(entry.Key); continue; }
                    if (!entry.Value.Released) Sync(entry.Value);
                }
                SyncMix();
                Array.Clear(Pcm); captured = 0;
                capturing = true;
                Check(studio.update()); // SYNCHRONOUS_UPDATE also advances the NRT core exactly once
                RenderedFrames++;
                if (captured != 0 && captured != Samples)
                    throw new InvalidOperationException($"Recording audio block has {captured} samples");
            } catch (Exception ex) { Fail(ex); }
            finally { capturing = false; copying = false; }
        }
    }

    public static void Publish(long token, bool replay) {
        lock (Gate) {
            if (view == null) return;
            long offset = 64L + token % Slots * SlotSize;
            view.Write(offset, 0L);
            if (replay) Array.Clear(Bytes);
            else Buffer.BlockCopy(Pcm, 0, Bytes, 0, Bytes.Length);
            view.Write(offset + 8, Error == null ? 0 : 1);
            view.WriteArray(offset + 64, Bytes, 0, Bytes.Length);
            Thread.MemoryBarrier(); view.Write(offset, token);
        }
    }

    private static RESULT Read(ref DSP_STATE state, IntPtr input, IntPtr output, uint length, int inputChannels, ref int outputChannels) {
        // No exceptions may cross the native FMOD callback boundary.
        try {
            outputChannels = inputChannels;
            int n = checked((int)length * inputChannels);
            var block = new float[n];
            if (input != IntPtr.Zero && n > 0) Marshal.Copy(input, block, 0, n);
            if (output != IntPtr.Zero && n > 0) Marshal.Copy(block, 0, output, n);
            if (!capturing) return RESULT.OK; // setup/release may mix extra blocks; they are not movie frames
            if (inputChannels == Channels && captured + length <= Samples) {
                Array.Copy(block, 0, Pcm, captured * Channels, n);
                captured += (int)length;
            } else if (length != 0) Error = $"Unexpected recording audio format: channels={inputChannels}, length={length}, captured={captured}";
        } catch (Exception ex) { Error = ex.Message; }
        return RESULT.OK;
    }

    private static void Fail(Exception ex) { Error = ex.Message; Log.Error("independent recording audio: " + ex.Message); }
    private static void Mirror(Action action) {
        if (copying || studio == null) return;
        lock (Gate) {
            if (studio == null || Error != null) return;
            copying = true;
            try { action(); } catch (Exception ex) { Fail(ex); }
            finally { copying = false; }
        }
    }

    private delegate RESULT InstanceCall(EventInstance self);
    private delegate RESULT StopCall(EventInstance self, STOP_MODE mode);
    private delegate RESULT SeekCall(EventInstance self, int position);
    private delegate RESULT BusStopCall(Bus self, STOP_MODE mode);
    private static RESULT OnBusStop(BusStopCall orig, Bus self, STOP_MODE mode) {
        Mirror(() => {
            if (self.getID(out Guid id) == RESULT.OK && studio!.getBusByID(id, out Bus copy) == RESULT.OK)
                copy.stopAllEvents(mode);
        });
        return orig(self, mode);
    }
    private static RESULT OnStart(InstanceCall orig, EventInstance self) {
        RESULT result = orig(self);
        if (result == RESULT.OK) Mirror(() => MirrorStart(self));
        return result;
    }
    private static RESULT OnStop(StopCall orig, EventInstance self, STOP_MODE mode) {
        Mirror(() => { if (Pairs.TryGetValue(self.getRaw(), out Pair? pair)) pair.Copy.stop(mode); });
        return orig(self, mode);
    }
    private static RESULT OnRelease(InstanceCall orig, EventInstance self) {
        Mirror(() => { if (Pairs.TryGetValue(self.getRaw(), out Pair? pair)) { Sync(pair); pair.Copy.release(); pair.Released = true; } });
        return orig(self);
    }
    private static RESULT OnCue(InstanceCall orig, EventInstance self) {
        Mirror(() => { if (Pairs.TryGetValue(self.getRaw(), out Pair? pair)) pair.Copy.triggerCue(); });
        return orig(self);
    }
    private static RESULT OnSeek(SeekCall orig, EventInstance self, int position) {
        Mirror(() => { if (Pairs.TryGetValue(self.getRaw(), out Pair? pair)) pair.Copy.setTimelinePosition(position); });
        return orig(self, position);
    }
}
