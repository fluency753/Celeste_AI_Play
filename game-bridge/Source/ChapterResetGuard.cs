using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Celeste;
using Mono.Cecil;
using Mono.Cecil.Cil;
using MonoMod.Utils;

namespace CelesteAI.Bridge;

/// <summary>Identify the native callback, never the localized text displayed on its button.</summary>
internal static class ChapterResetGuard {
    private sealed record Binding(string Key, string? Action, FieldInfo[] Owner, FieldInfo? RestartArea);
    private static Dictionary<MethodInfo, Binding> bindings = new();
    private static bool dispatchReady, identitiesReady, settingsReady;
    public static bool Available { get => dispatchReady && identitiesReady; set => dispatchReady = value; }
    /// <summary>The settings submenus (options, mod options, assist, variants) are recognized by their native callbacks.</summary>
    public static bool SettingsAvailable => Available && settingsReady;
    public const string SettingsAction = "settings_menu";
    private static readonly Dictionary<string, string?> Roles = new() {
        ["menu_pause_restartarea"] = "restart_chapter", ["menu_pause_return"] = "return_to_map",
        ["menu_pause_savequit"] = "save_quit", ["intro_vignette_quit"] = "return_to_map",
        ["menu_pause_resume"] = null, ["menu_pause_retry"] = null, ["menu_pause_skip_cutscene"] = null,
        ["intro_vignette_resume"] = null, ["intro_vignette_skip"] = null,
    };
    // Submenus that change key bindings, grab mode, assists or mod settings (the bridge and runtime among them).
    private static readonly HashSet<string> SettingsRoles = new() {
        "menu_pause_options", "menu_pause_assist", "menu_pause_variant", "menu_pause_modoptions",
    };

    internal static bool Initialize() {
        identitiesReady = false;
        settingsReady = false;
        bindings = new();
        var settingsFound = new HashSet<string>();
        try {
            var found = settingsFound;
            ReadConstruction(typeof(Level).GetMethod("orig_Pause", All)!, false, found);
            ReadConstruction(typeof(Level).GetMethod("GiveUp", All)!, true, found);
            ReadConstruction(typeof(IntroVignette).GetMethod("OpenMenu", All)!, false, found);
            if (Roles.Keys.Any(key => !found.Contains(key)) || !found.Contains("menu_restart_continue") || !found.Contains("menu_restart_cancel"))
                throw new InvalidOperationException("Native menu construction is missing required action callbacks");
            identitiesReady = true;
        } catch (Exception e) {
            bindings.Clear();
            Log.Error("chapter reset guard native callback identities unavailable: " + e);
        }
        if (identitiesReady) {
            // Mod Options is added by Everest's core module, not by Level.orig_Pause. Missing it only disables
            // the settings guard (reported separately in hello); the reset guard stays available.
            try {
                MethodInfo? modOptions = typeof(Celeste.Mod.Core.CoreModule).GetMethod("CreatePauseMenuButtons", All);
                if (modOptions == null) throw new InvalidOperationException("Everest CoreModule.CreatePauseMenuButtons not found");
                ReadConstruction(modOptions, false, found: settingsFound);
                settingsReady = SettingsRoles.All(settingsFound.Contains);
                if (!settingsReady)
                    Log.Error("settings menu guard missing callbacks: " + string.Join(", ", SettingsRoles.Where(k => !settingsFound.Contains(k))));
            } catch (Exception e) {
                Log.Error("settings menu guard unavailable: " + e);
            }
        }
        return identitiesReady;
    }

    private const BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

    private static void ReadConstruction(MethodInfo method, bool giveUp, HashSet<string> found) {
        using var body = new DynamicMethodDefinition(method);
        string? key = null;
        MethodInfo? callback = null;
        int confirmations = 0;
        foreach (var instruction in body.Definition.Body.Instructions) {
            if (instruction.OpCode == OpCodes.Ldstr && instruction.Operand is string text)
                key = Roles.ContainsKey(text) || SettingsRoles.Contains(text) ? text : giveUp ? text switch {
                    "menu_restart_continue" or "menu_return_continue" => "menu_restart_continue",
                    "menu_restart_cancel" or "menu_return_cancel" => "menu_restart_cancel", _ => null,
                } : null;
            if (instruction.OpCode == OpCodes.Ldftn && instruction.Operand is MethodReference reference)
                callback = reference.ResolveReflection() as MethodInfo;
            if (instruction.Operand is not MethodReference call || call.Name != "Pressed" ||
                    call.DeclaringType.FullName != typeof(TextMenu.Item).FullName!.Replace('+' , '/')) continue;
            if (giveUp) confirmations++;
            if (key != null) {
                if (callback == null || callback.Module.Assembly != typeof(Level).Assembly)
                    throw new InvalidOperationException("Native menu callback could not be resolved: " + key);
                var owner = FindOwner(callback.DeclaringType!, new HashSet<Type>());
                if (owner == null)
                    throw new InvalidOperationException("Native menu callback has no scene owner: " + key);
                FieldInfo? restart = key == "menu_restart_continue" ? callback.DeclaringType!.GetField("restartArea", All) : null;
                if (key == "menu_restart_continue" && restart?.FieldType != typeof(bool))
                    throw new InvalidOperationException("Native confirmation callback has no restartArea choice");
                if (restart != null) {
                    using var effect = new DynamicMethodDefinition(callback);
                    if (!effect.Definition.Body.Instructions.Any(ins => ins.OpCode == OpCodes.Ldfld &&
                            ins.Operand is FieldReference field && field.ResolveReflection() == restart))
                        throw new InvalidOperationException("Native confirmation no longer reads its restartArea choice");
                }
                var binding = new Binding(key, SettingsRoles.Contains(key) ? SettingsAction : Roles.GetValueOrDefault(key), owner, restart);
                if (bindings.TryGetValue(callback, out var prior) && prior != binding)
                    throw new InvalidOperationException("Native callback has conflicting menu meanings: " + key);
                bindings[callback] = binding;
                found.Add(key);
            }
            key = null;
            callback = null;
        }
        if (giveUp && confirmations != 2)
            throw new InvalidOperationException("Native GiveUp confirmation layout changed");
    }

    private static FieldInfo[]? FindOwner(Type type, HashSet<Type> seen) {
        if (typeof(Monocle.Scene).IsAssignableFrom(type)) return Array.Empty<FieldInfo>();
        if (!seen.Add(type)) return null;
        foreach (var field in type.GetFields(All)) {
            if (field.IsStatic || field.FieldType.Assembly != typeof(Level).Assembly) continue;
            if (field.FieldType == typeof(Level) || field.FieldType == typeof(IntroVignette)) return new[] { field };
            if (field.FieldType.IsNested && field.FieldType.IsDefined(typeof(System.Runtime.CompilerServices.CompilerGeneratedAttribute))) {
                var tail = FindOwner(field.FieldType, seen);
                if (tail != null) return new[] { field }.Concat(tail).ToArray();
            }
        }
        return null;
    }

    private static Binding? Recognize(TextMenu.Item item) {
        if (!identitiesReady || item.GetType() != typeof(TextMenu.Button) || item.OnPressed == null) return null;
        Delegate[] callbacks = item.OnPressed.GetInvocationList();
        if (callbacks.Length != 1 || callbacks[0].Method is not MethodInfo method || !bindings.TryGetValue(method, out var binding)) return null;
        object? owner = callbacks[0].Target;
        foreach (var field in binding.Owner) owner = owner == null ? null : field.GetValue(owner);
        return ReferenceEquals(owner, Monocle.Engine.Scene) ? binding : null;
    }

    public static bool IsSafe(TextMenu.Item item) => Recognize(item) is { Action: null, RestartArea: null };
    public static string? LabelOf(TextMenu.Item item) => item is TextMenu.Button button ? button.Label : item.SearchLabel();
    public static string? ActionOf(TextMenu.Item item) {
        Binding? binding = Recognize(item);
        if (binding?.RestartArea is { } restart)
            return (bool)restart.GetValue(item.OnPressed.Target)! ? "restart_chapter" : "return_to_map";
        return binding?.Action;
    }
}
