using System;
using Celeste.Mod;

namespace CelesteAI.Bridge;

/// <summary>
/// 可重载的执行与观测逻辑。连接、冻结和录音由 CelesteAIRuntime 持有。
/// </summary>
public class BridgeModule : EverestModule {
    private readonly Guid owner = Guid.NewGuid();
    private bool attached;
    public static BridgeModule Instance { get; private set; } = null!;

    public override Type SettingsType => typeof(BridgeSettings);
    public static BridgeSettings Settings => (BridgeSettings) Instance._Settings;

    public BridgeModule() {
        Instance = this;
    }

    public override void Load() {
        // 1.3.1 依赖核心 1.0.2 的输入绑定重新套用；旧核心缺少这个成员时在这里加载失败，不会接上桥接
        if (!InputInjector.ReinitializeHooked)
            Log.Warn("resident runtime cannot re-apply input bindings after Input.Initialize; reloads will be refused by the controller");
        try {
            Hooks.Load();
            Metadata.VersionString = Bridge.Version;
            BridgeRuntime.Attach(owner, 1, Bridge.Version, Bridge.Tick, Bridge.Enqueue, Bridge.CanReload,
                Bridge.ExportReloadState, Bridge.ImportReloadState, Bridge.ServiceReloadRequests);
            attached = true;
        } catch {
            Hooks.Unload();
            throw;
        }
        Log.Info($"loaded v{Bridge.Version}, resident IPC 127.0.0.1:{RuntimeModule.Settings.Port}");
    }

    public override void Unload() {
        if (attached) {
            BridgeRuntime.Detach(owner);
            attached = false;
        }
        void Cleanup() {
            Bridge.ReleasePayloadResources();
            ModHud.Unload();
            Hooks.Unload();
        }
        if (Celeste.Mod.MainThreadHelper.IsMainThread) Cleanup();
        else Celeste.Mod.MainThreadHelper.Schedule(Cleanup).GetAwaiter().GetResult();
    }
}

public class BridgeSettings : EverestModuleSettings {
    /// <summary>IPC 端口，只监听 127.0.0.1。</summary>
    [SettingIgnore]
    public int Port { get; set; } = 47774;

    /// <summary>锁步时钟端口（OBS 插件连接），只监听 127.0.0.1。</summary>
    [SettingIgnore]
    public int ClockPort { get; set; } = 47776;
}

internal static class Log {
    private const string Tag = "CelesteAIBridge";
    public static void Info(string message) => Logger.Log(LogLevel.Info, Tag, message);
    public static void Warn(string message) => Logger.Log(LogLevel.Warn, Tag, message);
    public static void Error(string message) => Logger.Log(LogLevel.Error, Tag, message);
}
