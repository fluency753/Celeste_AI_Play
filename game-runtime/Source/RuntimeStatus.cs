using System;
using System.Diagnostics;
using System.IO.MemoryMappedFiles;
using System.Text;

namespace CelesteAI.Bridge;

// The payload owns ordinary status reporting. During its absence only the
// resident owner can keep the existing shared HUD status alive.
internal static class RuntimeStatus {
    private static MemoryMappedFile? mapping;
    private static MemoryMappedViewAccessor? view;
    private static readonly Stopwatch SinceReport = Stopwatch.StartNew();
    private static bool warned;

    internal static void ReportHold(string version) {
        if (!OperatingSystem.IsWindows() || RuntimeModule.Instance == null || SinceReport.ElapsedMilliseconds < 250) return;
        SinceReport.Restart();
        try {
            mapping ??= MemoryMappedFile.CreateOrOpen($"Local\\CelesteAIHud-{RuntimeModule.Settings.Port}", 4096 + 7 * 1024 * 1024);
            view ??= mapping.CreateViewAccessor();
            view.Write(2072, BridgeRuntime.Active ? 1 : 0);
            view.Write(2076, ClockServer.LockstepRequested ? 1 : 0);
            view.Write(2080, BridgeRuntime.Frame);
            view.Write(2088, ClockServer.Connected ? 1 : 0);
            view.Write(2092, RecordingAudio.Enabled ? 1 : 0);
            WriteText(2096, 32, version);
            WriteText(2128, 64, "Bridge reload (held)");
            view.Write(2192, 1);
            view.Write(2196, BridgeRuntime.InputOwned ? 1 : 0);
            view.Write(2064, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        } catch (Exception ex) {
            if (!warned) Log.Warn($"resident status report failed: {ex.Message}");
            warned = true;
        }
    }

    private static void WriteText(long offset, int size, string text) {
        var bytes = new byte[size];
        byte[] encoded = Encoding.UTF8.GetBytes(text);
        Array.Copy(encoded, bytes, Math.Min(size - 1, encoded.Length));
        view!.WriteArray(offset, bytes, 0, size);
    }

    internal static void Unload() {
        view?.Dispose(); view = null;
        mapping?.Dispose(); mapping = null;
    }
}
