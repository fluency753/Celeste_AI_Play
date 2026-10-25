using System;
using System.Reflection;
using Celeste;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using MonoMod.RuntimeDetour;
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
    private static Hook? endDrawHook;

    public static void Load() {
        // FNA 的 Game.EndDraw 里调用 Present：钩在它之后，锁步时回复"这一帧已经呈现"
        MethodInfo? endDraw = typeof(Game).GetMethod("EndDraw", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (endDraw != null) {
            endDrawHook = new Hook(endDraw, typeof(Hooks).GetMethod(nameof(OnEndDraw), BindingFlags.Static | BindingFlags.NonPublic)!);
        } else {
            Log.Error("Game.EndDraw not found; lockstep acks fall back to the next update");
        }
        On.Celeste.Celeste.Update += OnCelesteUpdate;
        IL.Monocle.Engine.Update += ILEngineUpdate;
        On.Monocle.MInput.Update += OnMInputUpdate;
        On.Celeste.Celeste.RenderCore += OnRenderCore;
        On.Celeste.GameplayRenderer.Render += OnGameplayRender;
        IL.Monocle.EntityList.RenderExcept += ILEntityRenderExcept;
        On.Celeste.RunThread.Start += OnRunThreadStart;
    }

    public static void Unload() {
        endDrawHook?.Dispose();
        endDrawHook = null;
        On.Celeste.Celeste.Update -= OnCelesteUpdate;
        IL.Monocle.Engine.Update -= ILEngineUpdate;
        On.Monocle.MInput.Update -= OnMInputUpdate;
        On.Celeste.Celeste.RenderCore -= OnRenderCore;
        On.Celeste.GameplayRenderer.Render -= OnGameplayRender;
        IL.Monocle.EntityList.RenderExcept -= ILEntityRenderExcept;
        On.Celeste.RunThread.Start -= OnRunThreadStart;
    }

    // 每个 FNA 节拍进入一次；由 Bridge 决定本节拍推进几帧
    private static void OnCelesteUpdate(On.Celeste.Celeste.orig_Update orig, global::Celeste.Celeste self, GameTime gameTime) {
        // 接管期间每帧的时长固定为目标帧时长（锁步时游戏循环不限速，FNA 给出的实际间隔会变）
        GameTime time = Bridge.Active ? new GameTime(gameTime.TotalGameTime, self.TargetElapsedTime) : gameTime;
        Bridge.Tick(() => orig(self, time));
    }

    private delegate void OrigEndDraw(Game self);

    private static void OnEndDraw(OrigEndDraw orig, Game self) {
        orig(self);
        Bridge.AfterPresent();
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

    // 章节和显式 step 使用合成输入；空闲菜单恢复真实输入，锁步等令牌时不提前消费按键。
    private static void OnMInputUpdate(On.Monocle.MInput.orig_Update orig) {
        if (!Bridge.Active || (!Bridge.OwnsInput && !Bridge.ShouldHaltEngineUpdate())) {
            orig();
            return;
        }
        if (Bridge.FeedInputThisFrame) {
            InputInjector.Feed(Bridge.CurrentInput);
        }
    }

    private static void OnRenderCore(On.Celeste.Celeste.orig_RenderCore orig, global::Celeste.Celeste self) {
        // 规划回放：这一帧借用回放镜头与单独的随机数渲染，渲染完原样换回（见 Playback）
        Playback.BeforeRender();
        try {
            orig(self);
        } finally {
            Playback.AfterRender();
        }
        if (Bridge.DrawFrameMarker) {
            DrawFrameMarker();
        }
        ModHud.DrawOverlay(); // 游戏内 HUD（--hud-mod）：画在后备缓冲上，观测截图里没有
        Bridge.AfterRender();
    }

    private static void OnGameplayRender(On.Celeste.GameplayRenderer.orig_Render orig, GameplayRenderer self, Scene scene) {
        try {
            if (Playback.ShowSpinnerVisuals && scene is Level level) PlaybackSpinners.BeginPass(level);
            orig(self, scene);
        } finally {
            PlaybackSpinners.EndPassScope();
        }
        Playback.DrawWorld(self.Camera);
    }

    private static void ILEntityRenderExcept(ILContext il) {
        if (!EntityRenderDepthHook.Inject(il, PlaybackSpinners.BeforeEntity, PlaybackSpinners.FinishPass))
            Log.Error("spinner depth render hook unavailable; visual proxies disabled rather than drawn above gameplay");
    }

    /// <summary>录制校准：在窗口左下角画 16 格黑白条码，表示当前帧号的低 16 位（每格 12×12 像素，外加白框）。</summary>
    private static void DrawFrameMarker() {
        const int Cell = 12;
        GraphicsDevice device = Engine.Instance.GraphicsDevice;
        device.SetRenderTarget(null);
        int width = device.PresentationParameters.BackBufferWidth;
        int height = device.PresentationParameters.BackBufferHeight;
        device.Viewport = new Viewport(0, 0, width, height);
        int y = height - Cell - 4;
        long frame = Bridge.Frame;
        Draw.SpriteBatch.Begin();
        Draw.Rect(0, y - 4, Cell * 16 + 8, Cell + 8, Color.White);
        for (int bit = 0; bit < 16; bit++) {
            bool on = ((frame >> bit) & 1) != 0;
            Draw.Rect(4 + bit * Cell, y, Cell, Cell, on ? Color.Black : Color.White);
        }
        Draw.SpriteBatch.End();
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
