using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Celeste;
using Celeste.Mod;
using FMOD;
using FMOD.Studio;
using MonoMod.RuntimeDetour;
using StudioSystem = FMOD.Studio.System;

namespace CelesteAI.Bridge;

/// <summary>
/// Bank sources belong to the live System, while every recording System owns its
/// own bank handles. Never share Everest's callback stream handles between them.
/// </summary>
internal sealed class RecordingBanks : IDisposable {
    private static readonly object Gate = new();
    private static readonly List<Hook> Hooks = new();
    private static readonly Dictionary<(IntPtr System, IntPtr Bank), Registration> Sources = new();
    private static readonly HashSet<IntPtr> RecordingSystems = new();
    private static long generation;

    private abstract record Source {
        public abstract string Description { get; }
        public abstract RESULT Load(StudioSystem target, out Bank bank);
    }

    private sealed record FileSource(string Path) : Source {
        public override string Description => Path;
        public override RESULT Load(StudioSystem target, out Bank bank) =>
            target.loadBankFile(Path, LOAD_BANK_FLAGS.NORMAL, out bank);
    }

    private sealed record MemorySource(byte[] Bytes) : Source {
        public override string Description => $"memory bank ({Bytes.Length} bytes)";
        // This installed FMOD binding uses LOAD_MEMORY, which copies the data.
        public override RESULT Load(StudioSystem target, out Bank bank) =>
            target.loadBankMemory(Bytes, LOAD_BANK_FLAGS.NORMAL, out bank);
    }

    private sealed record AssetSource(ModAsset Asset) : Source {
        public override string Description => "Everest asset " + Asset.PathVirtual;
        public override RESULT Load(StudioSystem target, out Bank bank) {
            // Everest's existing cache streams ZIP assets to disk and supports
            // non-seekable sources. Do not retain the whole bank as managed PCM
            // or reuse the live System's custom callbacks and their userdata.
            string path = Asset.GetCachedPath();
            return target.loadBankFile(path, LOAD_BANK_FLAGS.NORMAL, out bank);
        }
    }

    private sealed record Registration(Source Source, long Generation);
    private sealed record Mirror(Guid Id, Registration Registration, Bank Bank);
    private readonly StudioSystem live, target;
    private readonly string bankDirectory;
    private readonly Dictionary<IntPtr, Mirror> mirrors = new();
    private string[] unsupported = Array.Empty<string>();
    private bool disposed;
    public int Count => mirrors.Count;
    public string[] Unsupported => unsupported;

    public RecordingBanks(StudioSystem live, StudioSystem target, string bankDirectory) {
        this.live = live;
        this.target = target;
        this.bankDirectory = bankDirectory;
        lock (Gate) RecordingSystems.Add(target.getRaw());
    }

    public static void LoadHooks() {
        lock (Gate) {
            if (Hooks.Count != 0) return;
            try {
                Add(typeof(StudioSystem), "loadBankFile", nameof(OnFile));
                Add(typeof(StudioSystem), "loadBankMemory", nameof(OnMemory));
                Add(typeof(Bank), "unload", nameof(OnUnload));
                Add(typeof(StudioSystem), "unloadAll", nameof(OnUnloadAll));
                Add(typeof(StudioSystem), "release", nameof(OnRelease));
            } catch {
                foreach (Hook hook in Hooks) hook.Dispose();
                Hooks.Clear();
                throw;
            }
        }
    }

    private static void Add(Type owner, string method, string replacement) => Hooks.Add(new Hook(
        owner.GetMethod(method) ?? throw new MissingMethodException(owner.FullName, method),
        typeof(RecordingBanks).GetMethod(replacement, BindingFlags.Static | BindingFlags.NonPublic)!));

    public static void UnloadHooks() {
        lock (Gate) {
            foreach (Hook hook in Hooks) hook.Dispose();
            Hooks.Clear();
            Sources.Clear();
            RecordingSystems.Clear();
        }
    }

    private static bool IsRecording(StudioSystem system) {
        lock (Gate) return RecordingSystems.Contains(system.getRaw());
    }

    private static void Remember(StudioSystem system, Bank bank, Source source) {
        lock (Gate) {
            if (RecordingSystems.Contains(system.getRaw())) return;
            Sources[(system.getRaw(), bank.getRaw())] = new(source, ++generation);
        }
    }

    private delegate RESULT FileLoad(StudioSystem self, string path, LOAD_BANK_FLAGS flags, out Bank bank);
    private static RESULT OnFile(FileLoad orig, StudioSystem self, string path, LOAD_BANK_FLAGS flags, out Bank bank) {
        RESULT result = orig(self, path, flags, out bank);
        if (result == RESULT.OK && bank != null && !IsRecording(self)) {
            try { Remember(self, bank, new FileSource(Path.GetFullPath(path))); }
            catch (Exception ex) { Log.Warn("recording bank source capture failed for " + path + ": " + ex.Message); }
        }
        return result;
    }

    private delegate RESULT MemoryLoad(StudioSystem self, byte[] bytes, LOAD_BANK_FLAGS flags, out Bank bank);
    private static RESULT OnMemory(MemoryLoad orig, StudioSystem self, byte[] bytes, LOAD_BANK_FLAGS flags, out Bank bank) {
        RESULT result = orig(self, bytes, flags, out bank);
        if (result == RESULT.OK && bank != null && !IsRecording(self)) {
            try { Remember(self, bank, new MemorySource((byte[])bytes.Clone())); }
            catch (Exception ex) { Log.Warn("recording memory bank source capture failed: " + ex.Message); }
        }
        return result;
    }

    private delegate RESULT BankUnload(Bank self);
    private static RESULT OnUnload(BankUnload orig, Bank self) {
        IntPtr raw = self.getRaw();
        RESULT result = orig(self);
        if (result == RESULT.OK) {
            lock (Gate) {
                foreach (var key in Sources.Keys.Where(key => key.Bank == raw).ToArray()) Sources.Remove(key);
            }
        }
        return result;
    }

    private delegate RESULT SystemCall(StudioSystem self);
    private static RESULT OnUnloadAll(SystemCall orig, StudioSystem self) {
        IntPtr raw = self.getRaw();
        RESULT result = orig(self);
        if (result == RESULT.OK) Forget(raw);
        return result;
    }

    private static RESULT OnRelease(SystemCall orig, StudioSystem self) {
        IntPtr raw = self.getRaw();
        RESULT result = orig(self);
        if (result == RESULT.OK) {
            Forget(raw);
            lock (Gate) RecordingSystems.Remove(raw);
        }
        return result;
    }

    private static void Forget(IntPtr raw) {
        lock (Gate) {
            foreach (var key in Sources.Keys.Where(key => key.System == raw).ToArray()) Sources.Remove(key);
        }
    }

    private Registration? Resolve(Bank bank, IReadOnlyDictionary<IntPtr, ModAsset> assets,
                                  IReadOnlyDictionary<IntPtr, string> officialNames) {
        var key = (live.getRaw(), bank.getRaw());
        // This is the precise asset chosen by Everest, including overrides of
        // official banks. Looking at all Mods/Audio files would load disabled
        // or conflicting versions that the game never loaded.
        assets.TryGetValue(key.Item2, out ModAsset? asset);
        lock (Gate) {
            if (Sources.TryGetValue(key, out Registration? known)) {
                if (asset == null || known.Source is AssetSource a && ReferenceEquals(a.Asset, asset)) return known;
                // The authoritative ModCache entry can arrive after the native
                // load. Invalidate an earlier fallback copy even if its GUID is
                // unchanged: a Mod may replace the official bank's contents.
                return Sources[key] = new(new AssetSource(asset), ++generation);
            }
        }
        officialNames.TryGetValue(key.Item2, out string? officialName);
        Source? source = asset == null ? ResolveOfficial(bank, officialName) : new AssetSource(asset);
        if (source == null) return null;
        lock (Gate) {
            // Another capture hook may have registered it while resolving assets.
            if (Sources.TryGetValue(key, out Registration? current)) return current;
            return Sources[key] = new(source, ++generation);
        }
    }

    private Source? ResolveOfficial(Bank bank, string? name) {
        if (string.IsNullOrEmpty(name) && bank.getPath(out string path) == RESULT.OK && path.StartsWith("bank:/", StringComparison.Ordinal))
            name = path.Substring("bank:/".Length);
        // Only known vanilla identities may fall back to installed files. An
        // anonymous custom bank is not evidence for loading an unrelated file.
        string? file = new[] { "Master Bank", "Master Bank.strings", "music", "dlc_music", "sfx", "dlc_sfx", "ui" }
            .FirstOrDefault(candidate => string.Equals(candidate, name, StringComparison.OrdinalIgnoreCase));
        return file == null ? null : new FileSource(Path.Combine(bankDirectory, file + ".bank"));
    }

    /// <summary>Reconcile actual loaded banks. Returns true when the event set may have changed.</summary>
    public bool Sync() {
        if (disposed) throw new ObjectDisposedException(nameof(RecordingBanks));
        Check(live.getBankList(out Bank[] banks), "enumerating live banks");
        var assets = new Dictionary<IntPtr, ModAsset>();
        var officialNames = new Dictionary<IntPtr, string>();
        try {
            // Build once per reconciliation, not once per bank (quadratic when
            // many enabled Mods add banks). Native handles are global identities.
            foreach (var row in Audio.Banks.ModCache) {
                if (row.Value != null && row.Value.getRaw() != IntPtr.Zero) assets[row.Value.getRaw()] = row.Key;
            }
            foreach (var row in Audio.Banks.Banks) {
                if (row.Value != null && row.Value.getRaw() != IntPtr.Zero) officialNames[row.Value.getRaw()] = row.Key;
            }
        } catch (InvalidOperationException) {
            // Everest is changing its metadata; do not guess a replacement's
            // source while registration is incomplete. Retry on the next hook.
            return false;
        }
        var current = new HashSet<IntPtr>(banks.Select(bank => bank.getRaw()));
        bool changed = false;
        foreach (IntPtr stale in mirrors.Keys.Where(key => !current.Contains(key)).ToArray()) {
            Check(mirrors[stale].Bank.unload(), "unloading a removed recording bank");
            mirrors.Remove(stale);
            changed = true;
        }
        var unknown = new List<string>();
        foreach (Bank bank in banks) {
            // A source can unload and reload between two rendered frames, even
            // reusing the native handle. Stop its old copy while the replacement
            // is still loading; waiting for a new GUID would replay stale audio.
            if (mirrors.TryGetValue(bank.getRaw(), out Mirror? previous)) {
                bool currentRegistration;
                lock (Gate) currentRegistration = Sources.TryGetValue((live.getRaw(), bank.getRaw()), out var captured)
                    && captured.Generation == previous.Registration.Generation;
                if (!currentRegistration) {
                    Check(previous.Bank.unload(), "unloading an invalidated recording bank");
                    mirrors.Remove(bank.getRaw());
                    changed = true;
                }
            }
            RESULT status = bank.getLoadingState(out LOADING_STATE state);
            if (status == RESULT.ERR_INVALID_HANDLE || state == LOADING_STATE.LOADING) continue;
            Check(status, "reading live bank state");
            if (state != LOADING_STATE.LOADED) continue;
            Check(bank.getID(out Guid id), "reading live bank identity");
            Registration? registration = Resolve(bank, assets, officialNames);
            if (registration == null) {
                // Never keep the old known bank under the native handle of an
                // unknown replacement, even when the replacement reuses a GUID.
                if (mirrors.TryGetValue(bank.getRaw(), out Mirror? stale)) {
                    Check(stale.Bank.unload(), "unloading an unresolved replacement bank");
                    mirrors.Remove(bank.getRaw());
                    changed = true;
                }
                unknown.Add($"Bank [{id}] has no captured file/memory or Everest asset source");
                continue;
            }
            if (mirrors.TryGetValue(bank.getRaw(), out Mirror? old)) {
                if (old.Id == id && old.Registration.Generation == registration.Generation) continue;
                Check(old.Bank.unload(), "unloading a replaced recording bank");
                mirrors.Remove(bank.getRaw());
                changed = true;
            }
            Bank copy;
            try {
                Check(registration.Source.Load(target, out copy), "loading " + registration.Source.Description);
                Check(copy.getID(out Guid copiedId), "reading recording bank identity");
                if (copiedId != id) {
                    copy.unload();
                    throw new InvalidOperationException($"Recording bank identity mismatch: live {id}, copy {copiedId}, {registration.Source.Description}");
                }
                Check(copy.loadSampleData(), "loading samples for " + registration.Source.Description);
            } catch (Exception ex) {
                throw new InvalidOperationException($"Recording bank [{id}] from {registration.Source.Description}: {ex.Message}", ex);
            }
            mirrors.Add(bank.getRaw(), new(id, registration, copy));
            changed = true;
        }
        unsupported = unknown.ToArray();
        if (changed) Check(target.flushSampleLoading(), "finishing recording bank samples");
        return changed;
    }

    private static void Check(RESULT result, string operation) {
        if (result != RESULT.OK) throw new InvalidOperationException($"Recording bank {operation}: {result}");
    }

    public void Dispose() {
        if (disposed) return;
        disposed = true;
        foreach (Mirror mirror in mirrors.Values) {
            if (mirror.Bank.isValid()) mirror.Bank.unload();
        }
        mirrors.Clear();
        lock (Gate) RecordingSystems.Remove(target.getRaw());
    }
}
