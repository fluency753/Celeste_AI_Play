using System;
using System.Reflection;
using Celeste;
using Celeste.Mod;
using Microsoft.Xna.Framework;
using Monocle;
using Mono.Cecil.Cil;
using MonoMod.Cil;
using MonoMod.RuntimeDetour;

namespace CelesteAI.Bridge;

internal static class FreezeGate {
    private static ILHook? engineGate;
    internal static bool Available { get; private set; }

    internal static void Load() {
        On.Celeste.Celeste.Update += OnUpdate;
        // Everest's initialization batch defers even explicit detour Apply.
        // Register here; BridgeRuntime checks Available before activation.
        engineGate = new ILHook(typeof(Engine).GetMethod("Update", BindingFlags.Instance | BindingFlags.NonPublic)!, EngineGate);
        On.Monocle.MInput.Update += OnInput;
        On.Celeste.Level.Update += OnLevelUpdate;
    }

    internal static void Unload() {
        Available = false;
        On.Celeste.Celeste.Update -= OnUpdate;
        engineGate?.Dispose();
        engineGate = null;
        On.Monocle.MInput.Update -= OnInput;
        On.Celeste.Level.Update -= OnLevelUpdate;
    }

    private static void OnUpdate(On.Celeste.Celeste.orig_Update orig, global::Celeste.Celeste self, GameTime time) {
        BridgeRuntime.Tick(() => orig(self, time), () => orig(self,
            BridgeRuntime.Active ? new GameTime(time.TotalGameTime, self.TargetElapsedTime) : time));
    }

    private static void EngineGate(ILContext il) {
        Available = false;
        // Keep Everest's scene handoff and Game.Components pump alive while
        // held. MainThreadHelper schedules entry to AssetReloadHelper there;
        // returning from Engine.Update would deadlock before the loader starts.
        var tail = new ILCursor(il);
        if (!tail.TryGotoNext(MoveType.Before,
                i => i.MatchLdarg(0), i => i.MatchLdfld(typeof(Engine), "scene"),
                i => i.MatchLdarg(0), i => i.MatchLdfld(typeof(Engine), "nextScene")))
            throw new InvalidOperationException("Resident freeze gate: scene handoff site not found");
        var maintenance = tail.MarkLabel();
        var cursor = new ILCursor(il);
        if (!cursor.TryGotoNext(MoveType.After, i => i.MatchCall(typeof(MInput), nameof(MInput.Update))))
            throw new InvalidOperationException("Resident freeze gate: MInput.Update site not found");
        var resume = cursor.DefineLabel();
        cursor.EmitDelegate<Func<bool>>(BridgeRuntime.ShouldHaltEngineUpdate);
        cursor.Emit(OpCodes.Brfalse, resume);
        cursor.EmitDelegate<Func<bool>>(() => BridgeRuntime.HoldingReload);
        cursor.Emit(OpCodes.Brtrue, maintenance);
        cursor.Emit(OpCodes.Ret);
        cursor.MarkLabel(resume);
        Available = true;
    }

    private static void OnInput(On.Monocle.MInput.orig_Update orig) {
        if (BridgeRuntime.HoldingReload) {
            // Maintenance UI may run, but neither real input nor a retained
            // synthetic key may advance its menus or the newly loaded Level.
            InputInjector.Feed(InputFrame.Neutral);
            return;
        }
        if (!BridgeRuntime.Active || (!BridgeRuntime.InputOwned && !BridgeRuntime.ShouldHaltEngineUpdate())) {
            orig();
        } else if (BridgeRuntime.FeedInputThisFrame) {
            InputInjector.Feed(BridgeRuntime.CurrentInput);
        }
    }

    private static void OnLevelUpdate(On.Celeste.Level.orig_Update orig, Level self) {
        // Engine.Update can enter as LevelLoader and switch to a Level after
        // the IL gate. The scene's own update boundary closes that same-frame
        // gap, so no physics or Session.Time advances before ready/finish.
        if (!BridgeRuntime.HoldingReload) orig(self);
    }
}
