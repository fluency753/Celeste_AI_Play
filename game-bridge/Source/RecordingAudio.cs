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
/// Recording audio: two FMOD mixers with no sound device that mirror the live game's events.
/// One synchronous update renders exactly 800 stereo samples (one 60 fps frame).
/// - full: advances on every recorded frame. Real game frames render everything; planning-replay
///   frames render with the SFX buses paused (as the live game does), so the music keeps going
///   through the replay and the full movie's soundtrack is continuous.
/// - play: advances only on real game frames, so the pure cut (replay frames removed) has a
///   continuous soundtrack too.
/// OBS reads both blocks by clock token, so idle wall time, missed captures and live audio
/// buffering cannot skew them. No game banks are redistributed: this reads the installed official banks.
/// </summary>
internal static class RecordingAudio {
    private const int Samples = 800, Channels = 2, Slots = 1024, Tracks = 2;
    private const int BlockBytes = Samples * Channels * 4, SlotSize = 64 + Tracks * BlockBytes;
    private const int Magic = 0x43414941, LayoutVersion = 2;
    public const int TrackCount = Tracks;
    private static readonly object Gate = new();
    private static readonly List<Hook> Hooks = new();
    private sealed record Pair(EventInstance Source, EventInstance Copy, bool OneShot, bool Snapshot, string Path, long CreatedFrame) {
        public bool Released, OrphanRetired, StopRequested;
    }
    [ThreadStatic] private static bool copying;
    private static FMOD.Studio.System? source;
    private static Mixer? full, play;
    private static MemoryMappedFile? mapping;
    private static MemoryMappedViewAccessor? view;
    private static readonly byte[] Bytes = new byte[BlockBytes];
    public static string? Error { get; private set; }
    public static bool Enabled => full != null;
    private static object? lastDiagnostics;
    public static object? Diagnostics {
        get { lock (Gate) return full == null ? lastDiagnostics : new { full = full.Stats, play = play?.Stats }; }
    }
    /// <summary>Real game frames rendered (both tracks).</summary>
    public static long RenderedFrames { get; private set; }
    /// <summary>Planning-replay frames rendered (full track only).</summary>
    public static long ReplayFrames { get; private set; }
    /// <summary>Timeline of the play track's copy of a live event (the track the pure cut uses).</summary>
    public static int? PositionFor(EventInstance? live) { lock (Gate) return play?.PositionOf(live); }
    /// <summary>Timeline of the full track's copy (ahead of the play track by the replays' length).</summary>
    public static int? FullPositionFor(EventInstance? live) { lock (Gate) return full?.PositionOf(live); }
    private static IEnumerable<Mixer> Mixers {
        get {
            if (full != null) yield return full;
            if (play != null) yield return play;
        }
    }

    public static void Load() {
        AddHook(nameof(EventInstance.start), nameof(OnStart));
        AddHook(nameof(EventInstance.stop), nameof(OnStop));
        AddHook(nameof(EventInstance.release), nameof(OnRelease));
        AddHook(nameof(EventInstance.triggerCue), nameof(OnCue));
        AddHook(nameof(EventInstance.setTimelinePosition), nameof(OnSeek));
        AddHook(nameof(EventInstance.setVolume), nameof(OnVolume));
        AddHook(nameof(EventInstance.setPitch), nameof(OnPitch));
        AddHook(nameof(EventInstance.setPaused), nameof(OnPause));
        AddHook(nameof(EventInstance.set3DAttributes), nameof(OnAttributes));
        AddHook(nameof(EventInstance.setParameterValue), nameof(OnParameter));
        AddHook(nameof(EventInstance.setParameterValueByIndex), nameof(OnParameterIndex));
        AddHook(nameof(EventInstance.setParameterValuesByIndices), nameof(OnParameters));
        Hooks.Add(new Hook(typeof(Bus).GetMethod(nameof(Bus.stopAllEvents))!,
            typeof(RecordingAudio).GetMethod(nameof(OnBusStop), BindingFlags.NonPublic | BindingFlags.Static)!));
        Hooks.Add(new Hook(typeof(Bus).GetMethod(nameof(Bus.setPaused))!,
            typeof(RecordingAudio).GetMethod(nameof(OnBusPause), BindingFlags.NonPublic | BindingFlags.Static)!));
        Hooks.Add(new Hook(typeof(Bus).GetMethod(nameof(Bus.setVolume))!,
            typeof(RecordingAudio).GetMethod(nameof(OnBusVolume), BindingFlags.NonPublic | BindingFlags.Static)!));
        Hooks.Add(new Hook(typeof(Bus).GetMethod(nameof(Bus.setMute))!,
            typeof(RecordingAudio).GetMethod(nameof(OnBusMute), BindingFlags.NonPublic | BindingFlags.Static)!));
        Hooks.Add(new Hook(typeof(VCA).GetMethod(nameof(VCA.setVolume))!,
            typeof(RecordingAudio).GetMethod(nameof(OnVcaVolume), BindingFlags.NonPublic | BindingFlags.Static)!));
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
                lastDiagnostics = null;
                RenderedFrames = 0;
                ReplayFrames = 0;
                source = live;
                full = new Mixer("full");
                play = new Mixer("play");
                foreach (Mixer mixer in Mixers) { mixer.Open(bankDirectory); mixer.SeedMix(live); }
                Check(live.getBankList(out Bank[] banks));
                foreach (Bank bank in banks) {
                    if (bank.getEventList(out EventDescription[] events) != RESULT.OK) continue;
                    foreach (EventDescription desc in events) {
                        if (desc.getInstanceList(out EventInstance[] instances) != RESULT.OK) continue;
                        foreach (EventInstance instance in instances) {
                            if (instance.getPlaybackState(out PLAYBACK_STATE state) != RESULT.OK || state == PLAYBACK_STATE.STOPPED) continue;
                            foreach (Mixer mixer in Mixers) mixer.MirrorStart(instance, seed: true);
                            desc.getPath(out string path);
                            instance.getTimelinePosition(out int position);
                            Log.Info($"recording audio: mirroring {path} ({state}, {position} ms)");
                        }
                    }
                }
                // Finish bank/command setup before installing the capture taps. Those
                // operations may mix extra blocks, which must never enter the movie.
                foreach (Mixer mixer in Mixers) mixer.InstallTap();
                mapping = MemoryMappedFile.CreateOrOpen($"Local\\CelesteAIRecordAudio-{clockPort}", 64L + Slots * SlotSize);
                view = mapping.CreateViewAccessor();
                view.Write(0, 0); // invalidate an old mapping before resetting slots
                for (int i = 0; i < Slots; i++) view.Write(64L + i * SlotSize, 0L);
                view.Write(4, LayoutVersion); view.Write(8, 48000); view.Write(12, Channels);
                view.Write(16, Samples); view.Write(20, Slots); view.Write(24, SlotSize); view.Write(28, Tracks);
                Thread.MemoryBarrier(); view.Write(0, Magic);
                Log.Info("independent recording audio ready (48 kHz stereo, two tracks: full with replay music, play without replays; no sound device)");
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
                if (full != null) {
                    lastDiagnostics = new { full = full.Stats, play = play?.Stats };
                    Log.Info("recording audio summary: " + System.Text.Json.JsonSerializer.Serialize(lastDiagnostics));
                }
                foreach (Mixer mixer in Mixers) mixer.Close();
                full = null; play = null; source = null;
                view?.Dispose(); view = null;
                mapping?.Dispose(); mapping = null;
            } finally { copying = false; }
        }
    }

    /// <summary>A real game frame: both tracks render it.</summary>
    public static void Advance() => Render(replay: false);

    /// <summary>A planning-replay frame (the game does not advance): only the full track renders it,
    /// with the bus state the live game has during replays (music on, SFX paused).</summary>
    public static void AdvanceReplay() => Render(replay: true);

    private static void Render(bool replay) {
        lock (Gate) {
            if (full == null || play == null || source == null || Error != null) return;
            copying = true;
            try {
                full.Render(source);
                if (replay) ReplayFrames++;
                else { play.Render(source); RenderedFrames++; }
            } catch (Exception ex) { Fail(ex); }
            finally { copying = false; }
        }
    }

    public static void Publish(long token, bool replay) {
        lock (Gate) {
            if (view == null) return;
            long offset = 64L + token % Slots * SlotSize;
            view.Write(offset, 0L);
            view.Write(offset + 8, Error == null ? 0 : 1);
            view.Write(offset + 12, replay ? 1 : 0);
            WriteBlock(offset + 64, full);
            WriteBlock(offset + 64 + BlockBytes, replay ? null : play); // the play track skips replay frames
            Thread.MemoryBarrier(); view.Write(offset, token);
        }
    }

    /// <summary>The block rendered for this frame; silence if the mixer did not render one
    /// (never repeat an old block: a repeated 1/60 s block is an audible buzz).</summary>
    private static void WriteBlock(long offset, Mixer? mixer) {
        if (mixer != null && mixer.Fresh) Buffer.BlockCopy(mixer.Pcm, 0, Bytes, 0, BlockBytes);
        else Array.Clear(Bytes);
        if (mixer != null) mixer.Fresh = false;
        view!.WriteArray(offset, Bytes, 0, BlockBytes);
    }

    private static void Fail(Exception ex) { Error = ex.Message; Log.Error("independent recording audio: " + ex.Message); }
    private static void ReportCallbackError(string message) => Error ??= message;
    private static void Mirror(Action<Mixer> action) {
        if (copying || full == null) return;
        lock (Gate) {
            if (full == null || Error != null) return;
            copying = true;
            try { foreach (Mixer mixer in Mixers) action(mixer); } catch (Exception ex) { Fail(ex); }
            finally { copying = false; }
        }
    }

    /// <summary>One mirrored FMOD Studio system rendering into <see cref="Pcm"/>.</summary>
    private sealed class Mixer {
        private readonly string name;
        private FMOD.Studio.System? studio;
        private FMOD.System? core;
        private DSP? tap;
        private readonly DSP_READCALLBACK callback; // kept alive while FMOD holds the native pointer
        private int captured;
        private volatile bool capturing;
        private float[] callbackBuffer = Array.Empty<float>();
        public readonly Dictionary<IntPtr, Pair> Pairs = new();
        public readonly float[] Pcm = new float[Samples * Channels];
        /// <summary>Pcm holds a block rendered since the last Publish.</summary>
        public bool Fresh;
        public long Created, Released, OrphanedLoops;
        public int PeakTracked;
        private long frameIndex;
        public object Stats => new { created = Created, released = Released, tracked = Pairs.Count,
                                     peak_tracked = PeakTracked, orphaned_loops = OrphanedLoops,
                                     oldest = Pairs.Values.OrderBy(p => p.CreatedFrame).Take(8).Select(Describe).ToArray() };

        private object Describe(Pair pair) {
            pair.Copy.getPlaybackState(out PLAYBACK_STATE state);
            pair.Copy.getPaused(out bool paused);
            pair.Copy.getPitch(out float pitch, out _);
            return new { path = pair.Path, released = pair.Released, age_frames = frameIndex - pair.CreatedFrame,
                         state = state.ToString(), paused, pitch, source_valid = pair.Source.isValid() };
        }

        public Mixer(string name) { this.name = name; callback = Read; }

        public void Open(string bankDirectory) {
            Check(FMOD.Studio.System.create(out FMOD.Studio.System system));
            studio = system;
            Check(system.getLowLevelSystem(out FMOD.System lowLevel));
            core = lowLevel;
            Check(lowLevel.setOutput(OUTPUTTYPE.NOSOUND_NRT));
            Check(lowLevel.setSoftwareFormat(48000, SPEAKERMODE.STEREO, 0));
            Check(lowLevel.setDSPBufferSize(Samples, 2));
            // Streams and asset loading are driven from update, not from FMOD's background threads: the mixer runs
            // in movie time (one update per recorded frame), so a stream thread working in wall time can starve it
            // (music dropouts) and makes the two tracks differ. Driven from update, the output is deterministic.
            Check(system.initialize(256, FMOD.Studio.INITFLAGS.SYNCHRONOUS_UPDATE | FMOD.Studio.INITFLAGS.LOAD_FROM_UPDATE,
                FMOD.INITFLAGS.STREAM_FROM_UPDATE, IntPtr.Zero));
            Check(lowLevel.getDSPBufferSize(out uint size, out _));
            if (size != Samples) throw new InvalidOperationException("FMOD changed the recording block size");
            foreach (string file in new[] { "Master Bank.bank", "Master Bank.strings.bank", "music.bank", "dlc_music.bank", "sfx.bank", "dlc_sfx.bank", "ui.bank" }) {
                Check(system.loadBankFile(Path.Combine(bankDirectory, file), LOAD_BANK_FLAGS.NORMAL, out Bank bank));
                Check(bank.loadSampleData());
            }
            Check(system.flushSampleLoading());
        }

        public void InstallTap() {
            Check(studio!.flushCommands());
            var description = new DSP_DESCRIPTION {
                pluginsdkversion = 0x00011000,
                name = $"AI recording tap {name}".PadRight(32, '\0').Substring(0, 32).ToCharArray(),
                version = 1, numinputbuffers = 1, numoutputbuffers = 1, read = callback,
            };
            Check(core!.createDSP(ref description, out DSP dsp));
            tap = dsp;
            Check(dsp.setChannelFormat(CHANNELMASK.STEREO, Channels, SPEAKERMODE.STEREO));
            Check(core.getMasterChannelGroup(out ChannelGroup master));
            Check(master.addDSP(0, dsp));
        }

        public void Close() {
            capturing = false;
            studio?.release();
            studio = null; core = null; tap = null;
            Pairs.Clear();
            Fresh = false;
        }

        public int? PositionOf(EventInstance? live) {
            if (live != null && Pairs.TryGetValue(live.getRaw(), out Pair? pair) &&
                pair.Copy.getTimelinePosition(out int position) == RESULT.OK) return position;
            return null;
        }

        private Pair? EnsurePair(EventInstance instance, bool restart = false) {
            if (studio == null) return null;
            IntPtr key = instance.getRaw();
            if (!Pairs.TryGetValue(key, out Pair? pair) || !pair.Copy.isValid() || (restart && pair.Released)) {
                // A restarted/reused source must not leave its previous copy without an owner.
                if (pair != null && pair.Copy.isValid()) {
                    Check(pair.Copy.stop(STOP_MODE.IMMEDIATE));
                    if (!pair.Released) Check(pair.Copy.release());
                }
                Check(instance.getDescription(out EventDescription desc));
                Check(desc.getID(out Guid id));
                Check(studio.getEventByID(id, out EventDescription copyDesc));
                Check(copyDesc.createInstance(out EventInstance copy));
                Check(desc.isOneshot(out bool oneShot));
                Check(desc.isSnapshot(out bool snapshot));
                Check(desc.getPath(out string path));
                pair = new Pair(instance, copy, oneShot, snapshot, path, frameIndex);
                Pairs[key] = pair;
                Created++;
                PeakTracked = Math.Max(PeakTracked, Pairs.Count);
                Sync(pair); // Seed once. Live async getters must not overwrite later explicit commands.
            }
            return pair;
        }

        public void Apply(EventInstance instance, Action<EventInstance> action) {
            if (EnsurePair(instance) is { } pair) action(pair.Copy);
        }

        public void MirrorStart(EventInstance instance, bool seed = false) {
            if (EnsurePair(instance, restart: true) is not { } pair) return;
            Check(pair.Copy.start());
            pair.StopRequested = false;
            if (seed && instance.getTimelinePosition(out int position) == RESULT.OK)
                pair.Copy.setTimelinePosition(position);
        }

        public void Stop(EventInstance live, STOP_MODE mode) {
            if (Pairs.TryGetValue(live.getRaw(), out Pair? pair) && pair.Copy.isValid()) {
                Check(pair.Copy.stop(mode));
                pair.StopRequested = true;
            }
        }

        public void Release(EventInstance live) {
            if (Pairs.TryGetValue(live.getRaw(), out Pair? pair) && !pair.Released && pair.Copy.isValid()) {
                Check(pair.Copy.release());
                pair.Released = true;
                Released++;
            }
        }

        public void Cue(EventInstance live) {
            if (Pairs.TryGetValue(live.getRaw(), out Pair? pair)) pair.Copy.triggerCue();
        }

        public void Seek(EventInstance live, int position) {
            if (Pairs.TryGetValue(live.getRaw(), out Pair? pair)) pair.Copy.setTimelinePosition(position);
        }

        public void PauseBus(Bus live, bool paused) {
            if (studio != null && live.getID(out Guid id) == RESULT.OK && studio.getBusByID(id, out Bus copy) == RESULT.OK)
                Check(copy.setPaused(paused));
        }

        public void BusVolume(Bus live, float volume) {
            if (studio != null && live.getID(out Guid id) == RESULT.OK && studio.getBusByID(id, out Bus copy) == RESULT.OK)
                Check(copy.setVolume(volume));
        }

        public void BusMute(Bus live, bool muted) {
            if (studio != null && live.getID(out Guid id) == RESULT.OK && studio.getBusByID(id, out Bus copy) == RESULT.OK)
                Check(copy.setMute(muted));
        }

        public void VcaVolume(VCA live, float volume) {
            if (studio != null && live.getID(out Guid id) == RESULT.OK && studio.getVCAByID(id, out VCA copy) == RESULT.OK)
                Check(copy.setVolume(volume));
        }

        public void StopBus(Bus live, STOP_MODE mode) {
            if (studio != null && live.getID(out Guid id) == RESULT.OK && studio.getBusByID(id, out Bus copy) == RESULT.OK)
                copy.stopAllEvents(mode);
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

        /// <summary>Capture the initial bus/VCA settings once. Later setters are mirrored directly;
        /// polling async getters every frame can overwrite a command with its previous value.</summary>
        public void SeedMix(FMOD.Studio.System live) {
            if (studio == null) return;
            if (live.getListenerAttributes(0, out FMOD.Studio._3D_ATTRIBUTES listener) == RESULT.OK)
                studio.setListenerAttributes(0, listener);
            if (live.getBankList(out Bank[] banks) != RESULT.OK) return;
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

        /// <summary>Copy the live state, then mix exactly one block.</summary>
        public void Render(FMOD.Studio.System live) {
            if (studio == null) return;
            foreach (var entry in Pairs.ToArray()) {
                Pair pair = entry.Value;
                if (!pair.Copy.isValid()) { Pairs.Remove(entry.Key); continue; }
                // release() is not stop(). A loop whose live owner disappeared cannot end itself.
                // Ordinary one-shots and snapshot fades keep their independent recording timeline.
                if (!pair.StopRequested && !pair.OrphanRetired && !pair.OneShot && !pair.Snapshot && !pair.Source.isValid()) {
                    Check(pair.Copy.stop(STOP_MODE.IMMEDIATE));
                    if (!pair.Released) { Check(pair.Copy.release()); pair.Released = true; Released++; }
                    OrphanedLoops++;
                    pair.OrphanRetired = true;
                }
            }
            if (live.getListenerAttributes(0, out FMOD.Studio._3D_ATTRIBUTES listener) == RESULT.OK)
                studio.setListenerAttributes(0, listener);
            Array.Clear(Pcm); captured = 0;
            capturing = true;
            try { Check(studio.update()); } // SYNCHRONOUS_UPDATE also advances the NRT core exactly once
            finally { capturing = false; }
            frameIndex++;
            if (captured != 0 && captured != Samples)
                throw new InvalidOperationException($"Recording audio block ({name}) has {captured} samples");
            Fresh = true;
        }

        private RESULT Read(ref DSP_STATE state, IntPtr input, IntPtr output, uint length, int inputChannels, ref int outputChannels) {
            // No exceptions may cross the native FMOD callback boundary.
            try {
                outputChannels = inputChannels;
                int n = checked((int)length * inputChannels);
                if (callbackBuffer.Length < n) Array.Resize(ref callbackBuffer, n);
                var block = callbackBuffer;
                if (input != IntPtr.Zero && n > 0) Marshal.Copy(input, block, 0, n);
                else if (n > 0) Array.Clear(block, 0, n);
                if (output != IntPtr.Zero && n > 0) Marshal.Copy(block, 0, output, n);
                if (!capturing) return RESULT.OK; // setup/release may mix extra blocks; they are not movie frames
                if (inputChannels == Channels && captured + length <= Samples) {
                    Array.Copy(block, 0, Pcm, captured * Channels, n);
                    captured += (int)length;
                } else if (length != 0) ReportCallbackError($"Unexpected recording audio format ({name}): channels={inputChannels}, length={length}, captured={captured}");
            } catch (Exception ex) { ReportCallbackError(ex.Message); }
            return RESULT.OK;
        }
    }

    private delegate RESULT InstanceCall(EventInstance self);
    private delegate RESULT StopCall(EventInstance self, STOP_MODE mode);
    private delegate RESULT SeekCall(EventInstance self, int position);
    private delegate RESULT FloatCall(EventInstance self, float value);
    private delegate RESULT PauseCall(EventInstance self, bool paused);
    private delegate RESULT AttributesCall(EventInstance self, FMOD.Studio._3D_ATTRIBUTES attributes);
    private delegate RESULT ParameterCall(EventInstance self, string name, float value);
    private delegate RESULT ParameterIndexCall(EventInstance self, int index, float value);
    private delegate RESULT ParametersCall(EventInstance self, int[] indices, float[] values, int count);
    private delegate RESULT BusStopCall(Bus self, STOP_MODE mode);
    private delegate RESULT BusPauseCall(Bus self, bool paused);
    private delegate RESULT BusFloatCall(Bus self, float value);
    private delegate RESULT VcaFloatCall(VCA self, float value);
    private static RESULT OnVolume(FloatCall orig, EventInstance self, float value) {
        RESULT result = orig(self, value);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.setVolume(value))));
        return result;
    }
    private static RESULT OnPitch(FloatCall orig, EventInstance self, float value) {
        RESULT result = orig(self, value);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.setPitch(value))));
        return result;
    }
    private static RESULT OnPause(PauseCall orig, EventInstance self, bool paused) {
        RESULT result = orig(self, paused);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.setPaused(paused))));
        return result;
    }
    private static RESULT OnAttributes(AttributesCall orig, EventInstance self, FMOD.Studio._3D_ATTRIBUTES attributes) {
        RESULT result = orig(self, attributes);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.set3DAttributes(attributes))));
        return result;
    }
    private static RESULT OnParameter(ParameterCall orig, EventInstance self, string name, float value) {
        RESULT result = orig(self, name, value);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.setParameterValue(name, value))));
        return result;
    }
    private static RESULT OnParameterIndex(ParameterIndexCall orig, EventInstance self, int index, float value) {
        RESULT result = orig(self, index, value);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.setParameterValueByIndex(index, value))));
        return result;
    }
    private static RESULT OnParameters(ParametersCall orig, EventInstance self, int[] indices, float[] values, int count) {
        RESULT result = orig(self, indices, values, count);
        if (result == RESULT.OK) Mirror(m => m.Apply(self, copy => Check(copy.setParameterValuesByIndices(indices, values, count))));
        return result;
    }
    private static RESULT OnBusPause(BusPauseCall orig, Bus self, bool paused) {
        RESULT result = orig(self, paused);
        if (result == RESULT.OK) Mirror(m => m.PauseBus(self, paused));
        return result;
    }
    private static RESULT OnBusVolume(BusFloatCall orig, Bus self, float value) {
        RESULT result = orig(self, value);
        if (result == RESULT.OK) Mirror(m => m.BusVolume(self, value));
        return result;
    }
    private static RESULT OnBusMute(BusPauseCall orig, Bus self, bool muted) {
        RESULT result = orig(self, muted);
        if (result == RESULT.OK) Mirror(m => m.BusMute(self, muted));
        return result;
    }
    private static RESULT OnVcaVolume(VcaFloatCall orig, VCA self, float value) {
        RESULT result = orig(self, value);
        if (result == RESULT.OK) Mirror(m => m.VcaVolume(self, value));
        return result;
    }
    private static RESULT OnBusStop(BusStopCall orig, Bus self, STOP_MODE mode) {
        RESULT result = orig(self, mode);
        if (result == RESULT.OK) Mirror(m => m.StopBus(self, mode));
        return result;
    }
    private static RESULT OnStart(InstanceCall orig, EventInstance self) {
        RESULT result = orig(self);
        if (result == RESULT.OK) Mirror(m => m.MirrorStart(self));
        return result;
    }
    private static RESULT OnStop(StopCall orig, EventInstance self, STOP_MODE mode) {
        // The live clock can have ended a sound during thinking while its recording copy is still playing.
        Mirror(m => m.Stop(self, mode));
        return orig(self, mode);
    }
    private static RESULT OnRelease(InstanceCall orig, EventInstance self) {
        Mirror(m => m.Release(self));
        return orig(self);
    }
    private static RESULT OnCue(InstanceCall orig, EventInstance self) {
        RESULT result = orig(self);
        if (result == RESULT.OK) Mirror(m => m.Cue(self));
        return result;
    }
    private static RESULT OnSeek(SeekCall orig, EventInstance self, int position) {
        RESULT result = orig(self, position);
        if (result == RESULT.OK) Mirror(m => m.Seek(self, position));
        return result;
    }
}
