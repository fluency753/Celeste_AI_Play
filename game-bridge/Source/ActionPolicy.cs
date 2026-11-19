using System;
using System.Collections.Generic;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

internal sealed record ActionPermissions(bool RestartChapter = false, bool ReturnToMap = false, bool SaveQuit = false) {
    public bool AllowsAll => RestartChapter && ReturnToMap && SaveQuit;
    public bool Allows(string action) => action switch {
        "restart_chapter" => RestartChapter, "return_to_map" => ReturnToMap, "save_quit" => SaveQuit,
        _ => throw new ArgumentException("Unknown game action: " + action),
    };
}

/// <summary>User permissions/target and a job-local departure grant tied to one Level.</summary>
internal sealed class ActionPolicy {
    public const int Version = 1;
    public readonly ActionPermissions Permissions;
    public readonly MapEntryGuard? Entry;
    /// <summary>Whether options, mod options, assist and variant submenus may be opened (still only with all three
    /// permissions). Clients that do not send settings_menus keep the earlier behavior.</summary>
    public readonly bool SettingsMenus;
    private Level? departureLevel;
    private Session? departureSession;

    public ActionPolicy(ActionPermissions permissions, MapEntryGuard? entry = null, bool departure = false, bool settingsMenus = true) {
        Permissions = permissions;
        Entry = entry;
        SettingsMenus = settingsMenus;
        // A job starting in a menu/loading scene cannot authorize a later Level.
        departureLevel = departure ? Engine.Scene as Level : null;
        departureSession = departureLevel?.Session;
    }

    public void NoteScene(Scene? scene) {
        if (!ReferenceEquals(scene, departureLevel) || departureLevel?.Session != departureSession) {
            departureLevel = null;
            departureSession = null;
        }
    }

    public ActionBlock? CheckMenu(TextMenu.Item item) {
        NoteScene(Engine.Scene);
        string? action = ChapterResetGuard.ActionOf(item);
        bool completed = Engine.Scene is Level { Completed: true };
        if (action == ChapterResetGuard.SettingsAction) {
            // Key bindings, grab mode, assists and the bridge's own mod settings would break input and simulation.
            if (Engine.Scene is Level or IntroVignette && !(SettingsMenus && Permissions.AllowsAll))
                return new("chapter_reset_blocked", "menu.settings", ChapterResetGuard.SettingsAction,
                    "Controller policy keeps the options, mod options, assist and variant submenus closed",
                    "no settings submenu", ChapterResetGuard.LabelOf(item));
            return null;
        }
        if (action == null) {
            // A list of dangerous callbacks alone would let through a button a mod adds or changes.
            // Completion and departure grants apply only to authenticated
            // return/save callbacks, never to an unknown operation. Other scenes retain normal menu controls.
            if (Engine.Scene is Level or IntroVignette && !Permissions.AllowsAll &&
                    !ChapterResetGuard.IsSafe(item))
                return new("chapter_reset_blocked", "menu.unknown_item", "menu_item",
                    "Only authenticated native resume, retry, skip cutscene and cancel callbacks may be confirmed without all action permissions",
                    "resume/retry/skip_cutscene/cancel", ChapterResetGuard.LabelOf(item));
            return null;
        }
        if (Permissions.Allows(action)) return null;
        if ((action is "return_to_map" or "save_quit") && (departureLevel != null || completed)) return null;
        return new("chapter_reset_blocked", "permission." + action, action,
            "Controller policy forbids " + action, true, false);
    }

    public static ActionPolicy Read(JsonElement request) {
        if (request.TryGetProperty("action_policy", out JsonElement data)) {
            if (data.ValueKind != JsonValueKind.Object || !data.TryGetProperty("version", out var version) ||
                version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out int v) || v != Version)
                throw new FormatException("Unsupported action_policy version");
            JsonElement permissions = data.GetProperty("permissions");
            if (permissions.ValueKind != JsonValueKind.Object) throw new FormatException("permissions must be an object");
            foreach (var field in permissions.EnumerateObject())
                if (field.Name is not ("restart_chapter" or "return_to_map" or "save_quit"))
                    throw new FormatException("Unknown permission: " + field.Name);
            string departure = data.GetProperty("departure").GetString() ?? "none";
            if (departure is not ("none" or "current_level")) throw new FormatException("Unknown departure scope");
            bool settings = !data.TryGetProperty("settings_menus", out _) || Bool(data, "settings_menus");
            return new(new(Bool(permissions, "restart_chapter"), Bool(permissions, "return_to_map"), Bool(permissions, "save_quit")),
                ReadEntry(data, "entry_target"), departure == "current_level", settings);
        }
        // Older clients retain their wire format, but grants get the fixed lifetime.
        bool allow = Bool(request, "allow_chapter_reset", optional: true);
        return new(new(allow, allow, allow), ReadEntry(request, "entry_guard"), Bool(request, "allow_map_exit", optional: true));
    }

    private static bool Bool(JsonElement data, string key, bool optional = false) {
        if (!data.TryGetProperty(key, out var value) && optional) return false;
        if (value.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) throw new FormatException(key + " must be boolean");
        return value.GetBoolean();
    }

    private static MapEntryGuard? ReadEntry(JsonElement data, string name) {
        if (!data.TryGetProperty(name, out var target) || target.ValueKind == JsonValueKind.Null) return null;
        if (target.ValueKind != JsonValueKind.Object) throw new FormatException(name + " must be an object");
        string? sid = target.TryGetProperty("sid", out var s) && s.ValueKind != JsonValueKind.Null ? s.GetString() : null;
        int? slot = target.TryGetProperty("slot", out var n) && n.ValueKind != JsonValueKind.Null ? n.GetInt32() : null;
        List<MapIdentity>? maps = null;
        if (target.TryGetProperty("maps", out var entries)) {
            if (entries.ValueKind != JsonValueKind.Array) throw new FormatException("maps must be an array");
            if (sid != null) throw new FormatException("sid and maps cannot both constrain the destination");
            maps = new();
            foreach (var map in entries.EnumerateArray()) {
                if (map.ValueKind != JsonValueKind.Object || !map.TryGetProperty("sid", out var mapSid) ||
                        mapSid.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(mapSid.GetString()) ||
                        !map.TryGetProperty("side", out var mapSide) || mapSide.ValueKind != JsonValueKind.String ||
                        mapSide.GetString() is not ("A" or "B" or "C"))
                    throw new FormatException("Each map must contain a nonempty sid and side A/B/C");
                foreach (var field in map.EnumerateObject())
                    if (field.Name is not ("sid" or "side")) throw new FormatException("Unknown map identity field: " + field.Name);
                var identity = new MapIdentity(mapSid.GetString()!, mapSide.GetString()!);
                if (maps.Contains(identity)) throw new FormatException("Duplicate map identity");
                maps.Add(identity);
            }
        }
        return new(sid, slot, maps);
    }
}
