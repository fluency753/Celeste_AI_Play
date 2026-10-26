using System;
using System.Collections.Generic;
using FMOD;
using FMOD.Studio;

namespace CelesteAI.Bridge;

/// <summary>One source snapshot seeds both independent tracks. Later changes use setter hooks.</summary>
internal sealed class RecordingMixSnapshot {
    private sealed record BusState(Guid Id, float? Volume, bool? Mute, bool? LocalPause);
    private sealed record VcaState(Guid Id, float Volume);
    private readonly List<BusState> buses = new();
    private readonly List<VcaState> vcas = new();
    private FMOD.Studio._3D_ATTRIBUTES? listener;

    public static RecordingMixSnapshot Capture(FMOD.Studio.System live, IReadOnlyDictionary<IntPtr, bool> requests) {
        var snapshot = new RecordingMixSnapshot();
        if (live.getListenerAttributes(0, out var attributes) == RESULT.OK) snapshot.listener = attributes;
        if (live.getBankList(out Bank[] banks) != RESULT.OK) return snapshot;
        foreach (Bank bank in banks) {
            if (bank.getVCAList(out VCA[] vcas) == RESULT.OK)
                foreach (VCA vca in vcas)
                    if (vca.getID(out Guid id) == RESULT.OK && vca.getVolume(out float volume, out _) == RESULT.OK)
                        snapshot.vcas.Add(new VcaState(id, volume));
            if (bank.getBusList(out Bus[] buses) != RESULT.OK) continue;
            foreach (Bus bus in buses) {
                if (bus.getID(out Guid id) != RESULT.OK) continue;
                float? volume = bus.getVolume(out float v, out _) == RESULT.OK ? v : null;
                bool? mute = bus.getMute(out bool m) == RESULT.OK ? m : null;
                bool? pause = LocalPause(live, bus, requests);
                snapshot.buses.Add(new BusState(id, volume, mute, pause));
            }
        }
        return snapshot;
    }

    private static bool? LocalPause(FMOD.Studio.System live, Bus bus, IReadOnlyDictionary<IntPtr, bool> requests) {
        if (requests.TryGetValue(bus.getRaw(), out bool requested)) return requested;
        if (bus.getPaused(out bool effective) != RESULT.OK) return null;
        if (!effective || bus.getPath(out string path) != RESULT.OK) return effective;
        // With no captured setter, the getter includes ancestor pauses. Do not seed them as local.
        while (path.LastIndexOf('/') > "bus:".Length) {
            path = path.Substring(0, path.LastIndexOf('/'));
            if (live.getBus(path, out Bus parent) == RESULT.OK &&
                parent.getPaused(out bool parentPaused) == RESULT.OK && parentPaused) return false;
        }
        if (path != "bus:/" && live.getBus("bus:/", out Bus root) == RESULT.OK &&
            root.getPaused(out bool rootPaused) == RESULT.OK && rootPaused) return false;
        return true;
    }

    public void ApplyTo(FMOD.Studio.System target) {
        if (listener is {} attributes) target.setListenerAttributes(0, attributes);
        foreach (VcaState state in vcas)
            if (target.getVCAByID(state.Id, out VCA copy) == RESULT.OK) copy.setVolume(state.Volume);
        foreach (BusState state in buses) {
            if (target.getBusByID(state.Id, out Bus copy) != RESULT.OK) continue;
            if (state.Volume is {} volume) copy.setVolume(volume);
            if (state.Mute is {} mute) copy.setMute(mute);
            if (state.LocalPause is {} pause) copy.setPaused(pause);
        }
    }
}
