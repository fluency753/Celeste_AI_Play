using System;
using Celeste.Mod;

namespace CelesteAI.Bridge;

/// <summary>Recording, transport and freezing outlive the reloadable bridge module.</summary>
public sealed class RuntimeModule : EverestModule {
    public static RuntimeModule Instance { get; private set; } = null!;
    public override Type SettingsType => typeof(RuntimeSettings);
    public static RuntimeSettings Settings => (RuntimeSettings)Instance._Settings;

    public RuntimeModule() { Instance = this; }

    public override void Load() {
        RecordingAudio.Load();
        FreezeGate.Load();
        IpcServer.Start(Settings.Port);
        ClockServer.Start(Settings.ClockPort);
        Log.Info($"resident runtime ABI {BridgeRuntime.ContractVersion}, IPC {Settings.Port}, clock {Settings.ClockPort}");
    }

    public override void Unload() {
        BridgeRuntime.Shutdown();
        IpcServer.Stop();
        ClockServer.Stop();
        RecordingAudio.Unload();
        InputInjector.Uninstall();
        FreezeGate.Unload();
        RuntimeStatus.Unload();
    }
}

public sealed class RuntimeSettings : EverestModuleSettings {
    [SettingIgnore] public int Port { get; set; } = 47774;
    [SettingIgnore] public int ClockPort { get; set; } = 47776;
}

internal static class Log {
    public static void Info(string message) => Logger.Log(LogLevel.Info, "CelesteAIRuntime", message);
    public static void Warn(string message) => Logger.Log(LogLevel.Warn, "CelesteAIRuntime", message);
    public static void Error(string message) => Logger.Log(LogLevel.Error, "CelesteAIRuntime", message);
}
