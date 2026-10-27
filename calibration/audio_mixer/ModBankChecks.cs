using System.IO.Compression;
using System.Reflection;
using Celeste;
using Celeste.Mod;
using CelesteAI.Bridge;
using FMOD;
using FMOD.Studio;

internal static class ModBankChecks {
    private static readonly BindingFlags Flags = BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
    private static void Check(RESULT value) { if (value != RESULT.OK) throw new Exception(value.ToString()); }
    private static void Assert(bool value, string message) { if (!value) throw new Exception(message); }
    private static object Mixer(string name) => typeof(RecordingAudio).GetField(name, Flags)!.GetValue(null)!;
    private static FMOD.Studio.System Studio(string name) => (FMOD.Studio.System)Mixer(name).GetType().GetField("studio", Flags)!.GetValue(Mixer(name))!;
    private static EventInstance Copy(string name, EventInstance source) {
        var pairs = (System.Collections.IDictionary)Mixer(name).GetType().GetField("Pairs")!.GetValue(Mixer(name))!;
        Assert(pairs.Contains(source.getRaw()), name + ": Mod event has no recording copy");
        object pair = pairs[source.getRaw()]!;
        return (EventInstance)pair.GetType().GetProperty("Copy")!.GetValue(pair)!;
    }
    private static void CheckBank(Guid id, bool exists) {
        foreach (string name in new[] { "full", "play" }) {
            var result = Studio(name).getBankByID(id, out Bank bank);
            Assert((result == RESULT.OK && bank.isValid()) == exists, name + ": bank lifetime did not follow source " + id);
        }
    }

    private static void WriteWav(string path, List<float> samples) {
        using var writer = new BinaryWriter(File.Create(path));
        writer.Write(System.Text.Encoding.ASCII.GetBytes("RIFF")); writer.Write(36 + samples.Count * 4);
        writer.Write(System.Text.Encoding.ASCII.GetBytes("WAVEfmt ")); writer.Write(16);
        writer.Write((short)3); writer.Write((short)2); writer.Write(48000); writer.Write(48000 * 8);
        writer.Write((short)8); writer.Write((short)32);
        writer.Write(System.Text.Encoding.ASCII.GetBytes("data")); writer.Write(samples.Count * 4);
        foreach (float sample in samples) writer.Write(sample);
    }

    public static void Run(FMOD.Studio.System live, string game, EventInstance music) {
        string? file = Environment.GetEnvironmentVariable("EXTRA_RECORDING_BANK");
        if (string.IsNullOrEmpty(file)) {
            Console.WriteLine("SKIP: actual Mod bank PCM acceptance requires EXTRA_RECORDING_BANK (local resource, never committed)");
            return;
        }
        string output = Path.GetFullPath("local/mod-bank-acceptance"); Directory.CreateDirectory(output);
        string directory = Path.Combine(game, "Content", "FMOD", "Desktop");
        // An audible BGM must not make a missing Mod sound pass the PCM assertion.
        Check(live.getBankList(out Bank[] oldBanks));
        foreach (Bank bank in oldBanks) {
            Check(bank.getEventList(out EventDescription[] oldEvents));
            foreach (EventDescription oldEvent in oldEvents) Check(oldEvent.releaseAllInstances());
        }
        Check(live.getBus("bus:/", out Bus sourceMaster)); Check(sourceMaster.stopAllEvents(STOP_MODE.IMMEDIATE));
        // All cases using old source EventInstances run before this last check.
        Check(live.update()); Check(live.flushCommands());
        for (int frame = 0; frame < 30; frame++) { Check(live.update()); RecordingAudio.Advance(); }
        foreach (string name in new[] { "full", "play" }) {
            var silent = (float[])Mixer(name).GetType().GetField("Pcm")!.GetValue(Mixer(name))!;
            Assert(silent.Sum(x => (double)x * x) < 0.000001, name + ": baseline is not silent; Mod audibility would be ambiguous");
        }

        void Play(Bank bank, string label, bool rebuild = false, Action<EventInstance>? enrich = null) {
            Check(bank.getEventList(out EventDescription[] events));
            Assert(events.Length > 0, "Mod bank has no Studio events");
            Check(events[0].createInstance(out EventInstance sound));
            Check(sound.setPaused(true)); Check(sound.start()); Check(live.flushCommands());
            try {
                if (rebuild) { RecordingAudio.Stop(); RecordingAudio.Start(live, directory, 47876); }
                enrich?.Invoke(sound);
                foreach (string name in new[] { "full", "play" }) {
                    EventInstance copy = Copy(name, sound);
                    Check(Studio(name).flushCommands()); Check(copy.getPaused(out bool paused));
                    Assert(paused, name + ": Mod event pause was not mirrored");
                }
                Check(sound.setVolume(0.35f));
                foreach (string name in new[] { "full", "play" }) {
                    Check(Studio(name).flushCommands()); Check(Copy(name, sound).getVolume(out float volume, out _));
                    Assert(Math.Abs(volume - 0.35f) < 0.001f, name + ": Mod volume was not mirrored");
                }
                Check(sound.setPaused(false));
                var pcm = new Dictionary<string, List<float>> { ["full"] = new(), ["play"] = new() };
                for (int frame = 0; frame < 90; frame++) {
                    Check(live.update()); RecordingAudio.Advance();
                    foreach (string name in pcm.Keys) pcm[name].AddRange((float[])Mixer(name).GetType().GetField("Pcm")!.GetValue(Mixer(name))!);
                }
                foreach (var track in pcm) {
                    double energy = track.Value.Sum(x => (double)x * x);
                    Assert(energy > 0.01, label + ": Mod audio is absent from " + track.Key + ": " + energy);
                    WriteWav(Path.Combine(output, label + "-" + track.Key + ".wav"), track.Value);
                    Console.WriteLine($"Mod PCM {label} {track.Key}: energy={energy:F6}, samples={track.Value.Count}");
                }
                Assert(RecordingAudio.Error == null, "Mod bank failed the recorder: " + RecordingAudio.Error);
            } finally {
                if (sound.isValid()) { Check(sound.stop(STOP_MODE.IMMEDIATE)); Check(sound.release()); }
                Check(live.update()); Check(live.flushCommands());
            }
        }

        // Recording startup seeds an already-active Mod sound from the source bank.
        Check(live.loadBankFile(file, LOAD_BANK_FLAGS.NORMAL, out Bank source));
        Check(source.loadSampleData()); Check(live.flushSampleLoading()); Check(source.getID(out Guid id));
        try {
            Play(source, "file-startup", rebuild: true);
            CheckBank(id, true);
        } finally { Check(source.unload()); Check(live.flushCommands()); RecordingAudio.Advance(); }
        CheckBank(id, false);

        // The caller may reuse its memory buffer immediately after FMOD LOAD_MEMORY returns.
        byte[] bytes = File.ReadAllBytes(file);
        Check(live.loadBankMemory(bytes, LOAD_BANK_FLAGS.NORMAL, out source)); Array.Clear(bytes);
        Check(source.loadSampleData()); Check(live.flushSampleLoading());
        try {
            Play(source, "memory-reload"); CheckBank(id, true);
            Play(source, "memory-rebuild", rebuild: true);
        } finally { Check(source.unload()); Check(live.flushCommands()); RecordingAudio.Advance(); }
        CheckBank(id, false);

        string zipPath = Path.Combine(output, "resource.zip");
        using (var zipFile = File.Create(zipPath))
        using (var zip = new ZipArchive(zipFile, ZipArchiveMode.Create)) zip.CreateEntryFromFile(file, "Audio/recording-test.bank");
        var asset = new ZipBankAsset(zipPath, Path.Combine(output, "cached-mod.bank"));
        Check(live.loadBankFile(file, LOAD_BANK_FLAGS.NORMAL, out source));
        // Metadata arrives after the low-level load, as Audio.IngestBank fills
        // ModCache only after FMOD has returned the Bank.
        RecordingAudio.Advance(); CheckBank(id, true);
        bool hadOfficialName = Audio.Banks.Banks.TryGetValue("music", out Bank? oldOfficial);
        try {
            Play(source, "zip-late-cache", enrich: sound => {
                Check(sound.setTimelinePosition(30)); Check(live.flushCommands());
                Audio.Banks.ModCache[asset] = source;
                Audio.Banks.Banks["music"] = source;
                RecordingAudio.Advance();
                // No further source setter/start is allowed to recreate the
                // Pair after bank replacement: the production reconciler owns it.
                foreach (string name in new[] { "full", "play" }) {
                    EventInstance copy = Copy(name, sound);
                    Check(Studio(name).flushCommands()); Check(copy.getTimelinePosition(out int position));
                    Assert(Math.Abs(position - 30) < 5, name + ": late bank metadata lost active event timeline " + position);
                }
            });
            Play(source, "zip-rebuild", rebuild: true);
            Assert(asset.CacheRequests > 0 && asset.Opened > 0 && asset.Opened == asset.Closed,
                $"ZIP source was not used or leaked streams: cache={asset.CacheRequests}, open={asset.Opened}, close={asset.Closed}");
        } finally {
            Audio.Banks.ModCache.Remove(asset);
            if (hadOfficialName) Audio.Banks.Banks["music"] = oldOfficial!;
            else Audio.Banks.Banks.Remove("music");
            Check(source.unload()); Check(live.flushCommands()); RecordingAudio.Advance();
        }
        CheckBank(id, false);
        // Keep this check last in the native suite: releasing the old source
        // instances must not leave later cases holding stale EventInstances.
        Console.WriteLine("PASS: real Mod sound alone is audible on both PCM tracks; file/memory/ZIP source, pause/volume, recorder rebuild and same-GUID unload/reload follow the live game");
    }

    private sealed class ZipBankAsset : ModAsset {
        private readonly string zip, cache;
        public int Opened, Closed, CacheRequests;
        public ZipBankAsset(string zip, string cache) : base(null!) {
            this.zip = zip; this.cache = cache; PathVirtual = "Audio/recording-test"; Format = "bank"; Type = typeof(AssetTypeBank);
        }
        protected override void Open(out Stream stream, out bool isSection) {
            var archive = ZipFile.OpenRead(zip);
            stream = new OwnedZipStream(archive.GetEntry("Audio/recording-test.bank")!.Open(), archive, () => Closed++);
            Opened++; isSection = false;
        }
        public override string GetCachedPath() {
            CacheRequests++;
            using Stream input = Stream;
            using Stream output = File.Create(cache);
            input.CopyTo(output);
            return cache;
        }
    }
    private sealed class OwnedZipStream(Stream inner, ZipArchive archive, Action closed) : Stream {
        public override bool CanRead => inner.CanRead;
        public override bool CanSeek => inner.CanSeek;
        public override bool CanWrite => false;
        public override long Length => inner.Length;
        public override long Position { get => inner.Position; set => inner.Position = value; }
        public override int Read(byte[] buffer, int offset, int count) => inner.Read(buffer, offset, count);
        public override long Seek(long offset, SeekOrigin origin) => inner.Seek(offset, origin);
        public override void Flush() => inner.Flush();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing) { if (disposing) { inner.Dispose(); archive.Dispose(); closed(); } base.Dispose(disposing); }
    }
}
