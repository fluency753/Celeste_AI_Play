using System;
using Celeste;
using Microsoft.Xna.Framework;
using Mono.Cecil.Cil;
using Monocle;
using MonoMod.Cil;

namespace CelesteAI.Bridge;

/// <summary>
/// 所有对游戏的挂钩集中在这里。帧闸门的做法参考了 CelesteTAS（MIT）：
/// 在 Engine.Update 里 MInput.Update 之后插入"若处于冻结就直接返回"，
/// 这样冻结时游戏逻辑完全不推进，但窗口消息与绘制照常进行。
/// </summary>
internal static class Hooks {
    public static void Load() {
        On.Celeste.Celeste.Update += OnCelesteUpdate;
        IL.Monocle.Engine.Update += ILEngineUpdate;
        On.Monocle.MInput.Update += OnMInputUpdate;
        On.Celeste.Celeste.RenderCore += OnRenderCore;
        On.Celeste.RunThread.Start += OnRunThreadStart;
    }

    public static void Unload() {
        On.Celeste.Celeste.Update -= OnCelesteUpdate;
        IL.Monocle.Engine.Update -= ILEngineUpdate;
        On.Monocle.MInput.Update -= OnMInputUpdate;
        On.Celeste.Celeste.RenderCore -= OnRenderCore;
        On.Celeste.RunThread.Start -= OnRunThreadStart;
    }

    // 每个 FNA 节拍进入一次；由 Bridge 决定本节拍推进几帧
    private static void OnCelesteUpdate(On.Celeste.Celeste.orig_Update orig, global::Celeste.Celeste self, GameTime gameTime) {
        Bridge.Tick(() => orig(self, gameTime));
    }

    private static void ILEngineUpdate(ILContext il) {
        var cur = new ILCursor(il);
        if (!cur.TryGotoNext(MoveType.After, ins => ins.MatchCall(typeof(MInput), nameof(MInput.Update)))) {
            Log.Error("IL hook failed: MInput.Update call not found in Engine.Update; frame gate disabled");
            return;
        }

        var resume = cur.DefineLabel();
        cur.EmitDelegate<Func<bool>>(Bridge.ShouldHaltEngineUpdate);
        cur.Emit(OpCodes.Brfalse, resume);
        cur.Emit(OpCodes.Ret);
        cur.MarkLabel(resume);
    }

    // 接管期间不读真实键盘手柄，只写入桥接给出的按键
    private static void OnMInputUpdate(On.Monocle.MInput.orig_Update orig) {
        if (!Bridge.Active) {
            orig();
            return;
        }
        if (Bridge.FeedInputThisFrame) {
            InputInjector.Feed(Bridge.CurrentInput);
        }
    }

    private static void OnRenderCore(On.Celeste.Celeste.orig_RenderCore orig, global::Celeste.Celeste self) {
        orig(self);
        Bridge.AfterRender();
    }

    // 接管期间让关卡加载等后台线程同步执行，保证逐帧推进的确定性（同 CelesteTAS）
    private static void OnRunThreadStart(On.Celeste.RunThread.orig_Start orig, Action method, string name, bool highPriority) {
        if (Bridge.Active && name != "USER_IO" && name != "MOD_IO") {
            RunThread.RunThreadWithLogging(method);
            return;
        }
        orig(method, name, highPriority);
    }
}
