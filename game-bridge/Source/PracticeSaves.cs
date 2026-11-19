using System;
using System.Linq;
using System.Reflection;
using Celeste;
using Celeste.Mod;
using Microsoft.Xna.Framework;
using Mono.Cecil;
using Mono.Cecil.Cil;
using MonoMod.Cil;
using MonoMod.RuntimeDetour;

namespace CelesteAI.Bridge;

internal static class PracticeSaves {
    // SpeedrunTool's instant, script-oriented API. It is exported by
    // SpeedrunTool itself and works without CelesteTAS or its input libraries.
    private static class Immediate {
        public static Func<string, bool>? SaveState = null;
        public static Func<string, bool>? LoadState = null;
        public static Action<string>? ClearState = null;
        public static Func<string, bool>? TasIsSaved = null;
    }

    private static class Selection {
        public static Func<string>? GetSlotName = null;
        public static Func<string, bool>? SwitchSlot = null;
    }

    internal sealed record Saved(int FileSlot, string Sid, AreaMode Side, string Room,
        Vector2 Position, Vector2 Speed, int Dashes, InputFrame Input);

    private static readonly Saved?[] slots = new Saved?[2];
    private static readonly string prefix = "CelesteAI-" + Guid.NewGuid().ToString("N") + "-";
    private static ILHook? hook;
    private static int applied;
    private static readonly (Act Button, string Key)[] tasKeys = {
        (Act.Left, "L"), (Act.Right, "R"), (Act.Up, "U"), (Act.Down, "D"),
        (Act.Jump, "J"), (Act.Jump2, "K"), (Act.Dash, "X"), (Act.Dash2, "C"),
        (Act.CrouchDash, "Z"), (Act.CrouchDash2, "V"), (Act.Grab, "G"), (Act.Grab2, "H"),
        (Act.Pause, "S"), (Act.Restart, "Q"), (Act.Journal, "N"), (Act.Confirm, "O"),
    };
    internal static bool Available => hook != null && Immediate.SaveState != null && Immediate.LoadState != null &&
        Immediate.ClearState != null && Immediate.TasIsSaved != null && Selection.GetSlotName != null &&
        Selection.SwitchSlot != null;

    internal static void Bind() {
        var mod = Everest.Modules.FirstOrDefault(module => module.Metadata.Name == "SpeedrunTool");
        if (mod == null || mod.Metadata.Version < new Version(3, 29, 2)) return;
        // Resolve the exact exports without MonoMod's process-global import
        // registry retaining the unloadable Bridge assembly.
        Assembly assembly = mod.GetType().Assembly;
        BindExports(assembly, "TasSpeedrunToolInterop+Exports", typeof(Immediate));
        BindExports(assembly, "SaveLoadControlInterop+SaveLoadControlExports", typeof(Selection));
        Type? manager = assembly.GetType("Celeste.Mod.SpeedrunTool.SaveLoad.StateManager");
        MethodInfo? load = manager?.GetMethod("LoadStateCore", BindingFlags.Instance | BindingFlags.NonPublic,
            null, new[] { typeof(Level), typeof(bool), typeof(bool), typeof(string).MakeByRefType() }, null);
        if (load?.ReturnType == typeof(bool))
            hook = new ILHook(load, GuardLoad);
    }

    private static void GuardLoad(ILContext il) {
        // The public save/load extension callbacks change Speedrun Tool's
        // snapshot schema and invalidate other mods' existing slots. Inject
        // around the verified clone boundaries without registering a schema.
        const string actions = "Celeste.Mod.SpeedrunTool.SaveLoad.SaveLoadAction";
        static Func<Instruction, bool> Calls(string name) => instruction =>
            instruction.OpCode == OpCodes.Call && instruction.Operand is MethodReference method &&
            method.Name == name && method.DeclaringType.FullName == actions;
        var cursor = new ILCursor(il);
        if (!cursor.TryGotoNext(MoveType.Before, Calls("OnBeforeLoadState")))
            throw new InvalidOperationException("Speedrun Tool before-load boundary was not found");
        cursor.Emit(OpCodes.Ldarg_1);
        cursor.EmitDelegate<Action<Level>>(level => {
            if (!Bridge.Active) return;
            Bridge.InvalidateRecordedChapter();
            level.Session.StartedFromBeginning = false;
        });
        if (!cursor.TryGotoNext(MoveType.After, Calls("OnLoadState")))
            throw new InvalidOperationException("Speedrun Tool after-load boundary was not found");
        cursor.Emit(OpCodes.Ldarg_1);
        cursor.EmitDelegate<Action<Level>>(level => {
            applied++;
            if (Bridge.Active) level.Session.StartedFromBeginning = false;
        });
    }

    private static void BindExports(Assembly assembly, string exportType, Type target) {
        Type? exports = assembly.GetType("Celeste.Mod.SpeedrunTool.ModInterop." + exportType);
        foreach (FieldInfo field in target.GetFields(BindingFlags.Static | BindingFlags.Public)) {
            var invoke = field.FieldType.GetMethod(nameof(Action.Invoke))!;
            MethodInfo? method = exports?.GetMethod(field.Name, BindingFlags.Static | BindingFlags.Public,
                null, invoke.GetParameters().Select(param => param.ParameterType).ToArray(), null);
            if (method?.ReturnType == invoke.ReturnType)
                field.SetValue(null, method.CreateDelegate(field.FieldType));
        }
    }

    internal static void Unbind() {
        try {
            Clear();
        } finally {
            hook?.Dispose();
            hook = null;
        }
    }

    internal static Saved? Get(int slot) =>
        Immediate.TasIsSaved?.Invoke(Name(slot)) == true ? slots[slot - 1] : null;

    internal static void Save(int slot, Level level, Player player, InputFrame input) {
        slots[slot - 1] = null;
        var saved = new Saved(SaveData.Instance.FileSlot, level.Session.Area.SID, level.Session.Area.Mode,
            level.Session.Level, player.Position, player.Speed, player.Dashes, input);
        if (!InSlot(slot, Immediate.SaveState!) || Immediate.TasIsSaved?.Invoke(Name(slot)) != true)
            throw new InvalidOperationException("Speedrun Tool refused to save; the practice slot is unavailable");
        slots[slot - 1] = saved;
    }

    internal static Saved Restore(int slot, Level current) {
        Saved saved = Get(slot) ?? throw new InvalidOperationException("practice slot is empty");
        if (saved.FileSlot != SaveData.Instance?.FileSlot || saved.Sid != current.Session.Area.SID ||
            saved.Side != current.Session.Area.Mode)
            throw new InvalidOperationException("practice slot belongs to another save or map");
        int before = applied;
        Bridge.InvalidateRecordedChapter();
        current.Session.StartedFromBeginning = false;
        if (!InSlot(slot, Immediate.LoadState!) || applied == before || Monocle.Engine.Scene is not Level level ||
            SaveData.Instance?.FileSlot != saved.FileSlot || level.Session.Area.SID != saved.Sid ||
            level.Session.Area.Mode != saved.Side || level.Session.Level != saved.Room ||
            StateExtractor.GetPlayer(level) is not { } player || player.Position != saved.Position ||
            player.Speed != saved.Speed || player.Dashes != saved.Dashes)
            throw new InvalidOperationException("Speedrun Tool did not confirm restore; observe the current state before retrying");
        level.Session.StartedFromBeginning = false;
        return saved;
    }

    internal static void Clear() {
        if (!Available) return;
        for (int slot = 1; slot <= slots.Length; slot++) {
            try {
                if (Immediate.TasIsSaved?.Invoke(Name(slot)) == true)
                    InSlot(slot, name => { Immediate.ClearState!(name); return true; });
            } catch (Exception e) {
                Log.Warn($"Speedrun Tool practice slot cleanup failed: {e.Message}");
            }
            slots[slot - 1] = null;
        }
    }

    internal static string HeldKeys(InputFrame input) => input.Tas
        ? string.Join(",", tasKeys.Where(binding => input.Has(binding.Button)).Select(binding => binding.Key))
        : input.ToString();

    private static string Name(int slot) => prefix + slot;

    private static bool InSlot(int slot, Func<string, bool> operation) {
        string previous = Selection.GetSlotName!();
        try {
            return operation(Name(slot));
        } finally {
            if (!Selection.SwitchSlot!(previous))
                Log.Warn("Speedrun Tool did not restore the user's selected slot");
        }
    }
}
