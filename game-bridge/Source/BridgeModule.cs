using System;
using Celeste.Mod;

namespace CelesteAI.Bridge;

/// <summary>
/// Everest 入口：加载时挂钩子并启动本机 IPC 服务，卸载时全部撤销。
/// 桥接默认处于"未接管"状态，游戏照常可玩；主控发送 activate 后才接管帧推进与输入。
/// </summary>
public class BridgeModule : EverestModule {
    public static BridgeModule Instance { get; private set; } = null!;

    public override Type SettingsType => typeof(BridgeSettings);
    public static BridgeSettings Settings => (BridgeSettings) Instance._Settings;

    public BridgeModule() {
        Instance = this;
    }

    public override void Load() {
        Hooks.Load();
        IpcServer.Start(Settings.Port);
        Log.Info($"loaded v{Bridge.Version}, listening on 127.0.0.1:{Settings.Port}");
    }

    public override void Unload() {
        IpcServer.Stop();
        Bridge.Deactivate();
        Hooks.Unload();
    }
}

public class BridgeSettings : EverestModuleSettings {
    /// <summary>IPC 端口，只监听 127.0.0.1。</summary>
    [SettingIgnore]
    public int Port { get; set; } = 47774;
}

internal static class Log {
    private const string Tag = "CelesteAIBridge";
    public static void Info(string message) => Logger.Log(LogLevel.Info, Tag, message);
    public static void Warn(string message) => Logger.Log(LogLevel.Warn, Tag, message);
    public static void Error(string message) => Logger.Log(LogLevel.Error, Tag, message);
}
