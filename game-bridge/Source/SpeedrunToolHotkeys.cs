using System;
using System.Linq;
using System.Reflection;
using Celeste.Mod;
using Monocle;
using MonoMod.RuntimeDetour;

namespace CelesteAI.Bridge;

/// <summary>
/// 桥接接管期间不让 Speedrun Tool 的快捷键生效。
/// Speedrun Tool 在 MInput.Update 之后自己读真实键盘与手柄（Keyboard.GetState、GamePad.GetState），不看桥接写入的合成输入；
/// 它只在 MInput.ControllerHasFocus 为假时停用快捷键，而接管期间 InputInjector 把它设为真，窗口没有焦点时手柄（包括虚拟手柄）
/// 的按键也照样触发。读档、存档、清档、传送房间、刷出望远镜、切全屏都会改动受控的关卡或画面：2026-11-22 续跑时，冻结期间有手柄
/// 刷出了望远镜（SpeedrunTool 的 PortableLookout），开局冲刺时 Madeline 被它拉进 Dummy，章节计时照走。
/// 这里在它的门闸 Hotkeys_Rebase.Update(Scene) 外面包一层：照常调用（键盘、手柄状态与按下沿照常更新，交还控制时不会补发
/// 旧的按下），接管期间返回 false，与 CelesteTAS 运行时相同，Speedrun Tool 自己就跳过全部快捷键（包括开关快捷键本身）。
/// 桥接自己的练习存读档走 Speedrun Tool 的 TAS 接口，不经过快捷键，不受影响。没装 Speedrun Tool 时什么也不做。
/// </summary>
internal static class SpeedrunToolHotkeys {
    private delegate bool OrigGate(Scene scene);
    private static Hook? hook;
    private static bool present;

    /// <summary>握手里报告：absent（没装 Speedrun Tool）、guarded（接管期间快捷键停用）、unguarded（装了但没能挂上，快捷键照常生效）。</summary>
    internal static string State => hook != null ? "guarded" : present ? "unguarded" : "absent";

    internal static void Bind() {
        var mod = Everest.Modules.FirstOrDefault(module => module.Metadata.Name == "SpeedrunTool");
        present = mod != null;
        if (mod == null) return;
        Type? gate = mod.GetType().Assembly.GetType("Celeste.Mod.SpeedrunTool.Other.Hotkeys_Rebase");
        MethodInfo? update = gate?.GetMethod("Update", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic,
            null, new[] { typeof(Scene) }, null);
        if (update?.ReturnType != typeof(bool))
            throw new InvalidOperationException("Speedrun Tool hotkey gate Hotkeys_Rebase.Update(Scene) was not found");
        hook = new Hook(update, typeof(SpeedrunToolHotkeys).GetMethod(nameof(Gate), BindingFlags.Static | BindingFlags.NonPublic)!);
    }

    internal static void Unbind() {
        hook?.Dispose();
        hook = null;
    }

    private static bool Gate(OrigGate orig, Scene scene) {
        bool enabled = orig(scene);
        return enabled && !Bridge.Active;
    }
}
