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
    private static ILHook? menuConfirmationHook;
    private static Hook? chapterEntryHook;
    private static Hook? fileContinueHook, fileNewGameHook, firstAreaHook, levelEntryHook;
    private static Hook? picoConsoleHook, picoMenuHook;
    internal static bool Pico8GuardAvailable { get; private set; }

    public static void Load() {
        InstallMenuGuard();
        InstallEntryGuard();
        InstallPico8Guard();
        // FNA 的 Game.EndDraw 里调用 Present：钩在它之后，锁步时回复"这一帧已经呈现"
        MethodInfo? endDraw = typeof(Game).GetMethod("EndDraw", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (endDraw != null) {
            endDrawHook = new Hook(endDraw, typeof(Hooks).GetMethod(nameof(OnEndDraw), BindingFlags.Static | BindingFlags.NonPublic)!);
        } else {
            Log.Error("Game.EndDraw not found; lockstep acks fall back to the next update");
        }
        On.Celeste.Celeste.RenderCore += OnRenderCore;
        IL.Monocle.Engine.RenderCore += ILRenderCore;
        IL.Celeste.Level.Render += ILLevelRender;
        On.Celeste.GameplayRenderer.Render += OnGameplayRender;
        On.Celeste.DustGraphic.Render += PlaybackDust.Render;
        IL.Monocle.EntityList.RenderExcept += ILEntityRenderExcept;
        On.Celeste.RunThread.Start += OnRunThreadStart;
    }

    public static void Unload() {
        Pico8GuardAvailable = false;
        picoConsoleHook?.Dispose(); picoConsoleHook = null;
        picoMenuHook?.Dispose(); picoMenuHook = null;
        ChapterResetGuard.Available = false;
        MapEntryGuard.Available = false;
        try { chapterEntryHook?.Dispose(); } catch (Exception e) { Log.Error("map entry guard: removing the hook failed: " + e.Message); }
        chapterEntryHook = null;
        fileContinueHook?.Dispose(); fileContinueHook = null;
        fileNewGameHook?.Dispose(); fileNewGameHook = null;
        firstAreaHook?.Dispose(); firstAreaHook = null;
        levelEntryHook?.Dispose(); levelEntryHook = null;
        try { menuConfirmationHook?.Dispose(); } catch (Exception e) { Log.Error("chapter reset guard: removing the menu hook failed: " + e.Message); }
        menuConfirmationHook = null;
        endDrawHook?.Dispose();
        endDrawHook = null;
        On.Celeste.Celeste.RenderCore -= OnRenderCore;
        IL.Monocle.Engine.RenderCore -= ILRenderCore;
        IL.Celeste.Level.Render -= ILLevelRender;
        FrameGrabber.CaptureHookAvailable = false;
        FrameGrabber.LevelCaptureHookAvailable = false;
        FrameGrabber.Unload();
        On.Celeste.GameplayRenderer.Render -= OnGameplayRender;
        On.Celeste.DustGraphic.Render -= PlaybackDust.Render;
        PlaybackDust.Clear();
        PlaybackVisualPass.End();
        IL.Monocle.EntityList.RenderExcept -= ILEntityRenderExcept;
        On.Celeste.RunThread.Start -= OnRunThreadStart;
    }

    private delegate void OrigEndDraw(Game self);

    private static void OnEndDraw(OrigEndDraw orig, Game self) {
        orig(self);
        Bridge.AfterPresent();
    }

    // The two guards hook game methods that a game or Everest update, or another mod rewriting them, can change.
    // A failure must only make that guard unavailable (hello reports action_policy=0 and the controller refuses to
    // start before recording or entering the save); throwing here would abort Load and take the frame gate, input,
    // rendering and every other hook down with it.

    /// <summary>Everest's orig_Update navigates before confirming. Guard the actual dispatch site so simultaneous
    /// directions/confirm use native selection.</summary>
    private static void InstallMenuGuard() {
        ChapterResetGuard.Available = false;
        try {
            if (!ChapterResetGuard.Initialize()) return;
            MethodInfo? menuUpdate = typeof(TextMenu).GetMethod("orig_Update", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (menuUpdate == null) {
                Log.Error("chapter reset guard unavailable: TextMenu.orig_Update not found");
                return;
            }
            menuConfirmationHook = new ILHook(menuUpdate, ILMenuConfirmation);
        } catch (Exception e) {
            ChapterResetGuard.Available = false;
            try { menuConfirmationHook?.Dispose(); } catch { /* already broken */ }
            menuConfirmationHook = null;
            Log.Error("chapter reset guard unavailable: installing the menu hook failed: " + e);
        }
    }

    private static void InstallEntryGuard() {
        MapEntryGuard.Available = false;
        try {
            MethodInfo? start = typeof(OuiChapterPanel).GetMethod(nameof(OuiChapterPanel.Start));
            if (start == null) {
                Log.Error("map entry guard unavailable: OuiChapterPanel.Start not found");
                return;
            }
            chapterEntryHook = new Hook(start, typeof(Hooks).GetMethod(nameof(OnChapterStart), BindingFlags.Static | BindingFlags.NonPublic)!);
            const BindingFlags methods = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
            fileContinueHook = new Hook(typeof(OuiFileSelectSlot).GetMethod("OnContinueSelected", methods)!,
                typeof(Hooks).GetMethod(nameof(OnFileContinue), methods)!);
            fileNewGameHook = new Hook(typeof(OuiFileSelectSlot).GetMethod("OnNewGameSelected", methods)!,
                typeof(Hooks).GetMethod(nameof(OnFileNewGame), methods)!);
            firstAreaHook = new Hook(typeof(OuiFileSelectSlot).GetMethod("EnterFirstArea", methods)!,
                typeof(Hooks).GetMethod(nameof(OnFirstArea), methods)!);
            levelEntryHook = new Hook(typeof(LevelEnter).GetMethod(nameof(LevelEnter.Go), methods)!,
                typeof(Hooks).GetMethod(nameof(OnLevelEnter), methods)!);
            MapEntryGuard.Available = true;
        } catch (Exception e) {
            MapEntryGuard.Available = false;
            try { chapterEntryHook?.Dispose(); } catch { /* already broken */ }
            chapterEntryHook = null;
            fileContinueHook?.Dispose(); fileContinueHook = null;
            fileNewGameHook?.Dispose(); fileNewGameHook = null;
            firstAreaHook?.Dispose(); firstAreaHook = null;
            levelEntryHook?.Dispose(); levelEntryHook = null;
            Log.Error("map entry guard unavailable: installing the hook failed: " + e);
        }
    }

    // MonoMod re-runs this whenever the hook chain of TextMenu.orig_Update changes (e.g. another mod hooks it), so it
    // must not throw either: a missing dispatch site only marks the guard unavailable.
    private static void ILMenuConfirmation(ILContext il) {
        ChapterResetGuard.Available = false;
        var cur = new ILCursor(il);
        if (!cur.TryGotoNext(MoveType.Before, ins => ins.MatchCallvirt(typeof(TextMenu.Item), nameof(TextMenu.Item.ConfirmPressed)))) {
            Log.Error("chapter reset guard unavailable: TextMenu confirmation dispatch not found");
            return;
        }
        var allowed = cur.DefineLabel();
        cur.Emit(OpCodes.Dup);
        cur.EmitDelegate<Func<TextMenu.Item, bool>>(Bridge.AllowMenuConfirmation);
        cur.Emit(OpCodes.Brtrue, allowed);
        cur.Emit(OpCodes.Pop);
        cur.Emit(OpCodes.Ret); // neither ConfirmPressed nor OnPressed executes
        cur.MarkLabel(allowed);
        ChapterResetGuard.Available = true;
    }

    private delegate void OrigChapterStart(OuiChapterPanel self, string? checkpoint);
    private static void OnChapterStart(OrigChapterStart orig, OuiChapterPanel self, string? checkpoint) {
        if (Bridge.AllowMapEntry(self, checkpoint)) orig(self, checkpoint);
    }

    private delegate void OrigFileAction(OuiFileSelectSlot self);
    private static void OnFileContinue(OrigFileAction orig, OuiFileSelectSlot self) {
        // Check before SaveData.Start, audio changes and an asynchronous wipe.
        if (Bridge.AllowSaveEntry(self)) orig(self);
    }
    private static void OnFileNewGame(OrigFileAction orig, OuiFileSelectSlot self) {
        if (Bridge.AllowNewSave(self)) orig(self);
    }
    private static void OnFirstArea(OrigFileAction orig, OuiFileSelectSlot self) {
        if (SaveData.Instance is not { } save || Bridge.AllowAreaEntry(save.LastArea_Safe)) orig(self);
    }
    private delegate void OrigLevelEnter(Session session, bool fromSaveData);
    private static void OnLevelEnter(OrigLevelEnter orig, Session session, bool fromSaveData) {
        if (session == null || Bridge.AllowAreaEntry(session.Area)) orig(session!, fromSaveData);
    }

    private static void InstallPico8Guard() {
        Pico8GuardAvailable = false;
        try {
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
            picoConsoleHook = new Hook(typeof(PicoConsole).GetMethod("OnInteract", flags)!,
                typeof(Hooks).GetMethod(nameof(OnPicoConsole), BindingFlags.Static | BindingFlags.NonPublic)!);
            picoMenuHook = new Hook(typeof(OuiMainMenu).GetMethod("OnPico8", flags)!,
                typeof(Hooks).GetMethod(nameof(OnPicoMenu), BindingFlags.Static | BindingFlags.NonPublic)!);
            Pico8GuardAvailable = true;
        } catch (Exception e) {
            picoConsoleHook?.Dispose(); picoConsoleHook = null;
            picoMenuHook?.Dispose(); picoMenuHook = null;
            Log.Error("PICO-8 entry guard unavailable: " + e);
        }
    }

    private delegate void OrigPicoConsole(PicoConsole self, Player player);
    private static void OnPicoConsole(OrigPicoConsole orig, PicoConsole self, Player player) {
        if (Bridge.AllowPico8Entry()) orig(self, player);
    }
    private delegate void OrigPicoMenu(OuiMainMenu self);
    private static void OnPicoMenu(OrigPicoMenu orig, OuiMainMenu self) {
        if (Bridge.AllowPico8Entry()) orig(self);
    }

    private static void ILRenderCore(ILContext il) => FrameGrabber.CaptureHookAvailable = RedirectScreenTarget(il, false);
    private static void ILLevelRender(ILContext il) => FrameGrabber.LevelCaptureHookAvailable = RedirectScreenTarget(il, true);

    private static bool RedirectScreenTarget(ILContext il, bool level) {
        var cursor = new ILCursor(il);
        Instruction? screenAssignment = null;
        int assignments = 0, beforeRender = -1, afterRender = -1, assignmentIndex = -1;
        for (int index = 0; index < il.Body.Instructions.Count; index++) {
            var instruction = il.Body.Instructions[index];
            if (level ? instruction.MatchLdfld<Level>("GameplayRenderer") : instruction.MatchCallvirt<Scene>("BeforeRender"))
                beforeRender = index;
            if (level && instruction.MatchLdfld<Level>("HudRenderer")) afterRender = index;
            if (instruction.MatchCallvirt<GraphicsDevice>("SetRenderTarget") && instruction.Previous?.OpCode == OpCodes.Ldnull
                && instruction.Operand is Mono.Cecil.MethodReference method && method.Parameters.Count == 1
                && method.Parameters[0].ParameterType.FullName == typeof(RenderTarget2D).FullName) {
                assignments++; screenAssignment = instruction; assignmentIndex = index;
            }
        }
        if (assignments != 1 || beforeRender < 0 || assignmentIndex <= beforeRender || screenAssignment == null ||
            (level && (afterRender < 0 || assignmentIndex >= afterRender))) {
            Log.Warn($"native {(level ? "Level.Render" : "Engine.RenderCore")} screen target not found; story HUD captures unavailable");
            return false;
        }
        cursor.Goto(screenAssignment, MoveType.Before);
        cursor.EmitDelegate<Func<RenderTarget2D?, RenderTarget2D?>>(FrameGrabber.ScreenTarget);
        return true;
    }

    private static void OnRenderCore(On.Celeste.Celeste.orig_RenderCore orig, global::Celeste.Celeste self) {
        // 规划回放：这一帧借用回放镜头与单独的随机数渲染，渲染完原样换回（见 Playback）
        FrameGrabber.BeginFrame(Bridge.WantsStoryHudImage);
        try {
            RenderNative(orig, self);
            if (FrameGrabber.RedirectIncomplete) {
                // Part of this frame went past the capture target, so presenting
                // (and, in lockstep, recording) either surface would show a split
                // image. Redraw the frame natively; its gameplay random draws use
                // a private generator so the extra pass leaves game state as is.
                FrameGrabber.AbandonRedirect();
                Calc.PushRandom(unchecked(7927 + (int)Bridge.Frame));
                try {
                    RenderNative(orig, self);
                } finally {
                    Calc.PopRandom();
                }
            }
            Bridge.AfterRender();
        } finally {
            FrameGrabber.EndFrame();
        }
        if (Bridge.DrawFrameMarker) {
            DrawFrameMarker();
        }
        ModHud.DrawOverlay(); // 游戏内 HUD（--hud-mod）：画在后备缓冲上，观测截图里没有
    }

    private static void RenderNative(On.Celeste.Celeste.orig_RenderCore orig, global::Celeste.Celeste self) {
        Playback.BeforeRender();
        try {
            orig(self);
            FrameGrabber.MarkRendered();
        } finally {
            Playback.AfterRender();
        }
    }

    private static void OnGameplayRender(On.Celeste.GameplayRenderer.orig_Render orig, GameplayRenderer self, Scene scene) {
        try {
            if (Playback.ShowSpinnerVisuals && scene is Level level) PlaybackVisualPass.Begin(level);
            orig(self, scene);
        } finally {
            PlaybackVisualPass.End();
        }
        Playback.DrawWorld(self.Camera);
    }

    private static void ILEntityRenderExcept(ILContext il) {
        if (!EntityRenderDepthHook.Inject(il, PlaybackVisualPass.BeforeEntity, PlaybackVisualPass.Finish))
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
