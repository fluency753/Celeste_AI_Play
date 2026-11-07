using System;
using System.IO;
using System.Text.Json;
using Celeste.Mod;
using Monocle;

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
        ApplyInstanceTitle();
    }

    /// <summary>
    /// AI 专用游戏副本（游戏目录里有 celeste-ai-instance.json）：模组加载时就把窗口标题改成标记里的标题。
    /// 这时还没画第一帧，OBS 按标题采集时不会在改名之前误挂上副本（个人 OBS 只认标题 Celeste）。
    /// 主控接管时仍会再核对、补改一次。
    /// </summary>
    private static void ApplyInstanceTitle() {
        try {
            string path = Path.Combine(Everest.PathGame, "celeste-ai-instance.json");
            if (!File.Exists(path)) return;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("format", out JsonElement format) || format.ValueKind != JsonValueKind.Number
                || format.GetInt32() != 1
                || !root.TryGetProperty("window_title", out JsonElement value) || value.ValueKind != JsonValueKind.String)
                return;
            string title = value.GetString()!;
            if (title.Trim().Length == 0 || title.Length > 80) return;
            void Set() {
                if (Engine.Instance?.Window != null && Engine.Instance.Window.Title != title)
                    Engine.Instance.Window.Title = title;
            }
            if (MainThreadHelper.IsMainThread) Set();
            else MainThreadHelper.Schedule(Set);
            Log.Info($"AI instance window title: {title}");
        } catch (Exception e) {
            Log.Warn($"AI instance window title not applied: {e.Message}");
        }
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
