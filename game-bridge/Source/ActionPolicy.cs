using System;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

internal sealed record ActionPermissions(bool RestartChapter = false, bool ReturnToMap = false, bool SaveQuit = false) {
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
    private Level? departureLevel;
    private Session? departureSession;

    public ActionPolicy(ActionPermissions permissions, MapEntryGuard? entry = null, bool departure = false) {
        Permissions = permissions;
        Entry = entry;
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
        if (action == null || Permissions.Allows(action)) return null;
        if ((action is "return_to_map" or "save_quit") &&
                (departureLevel != null || Engine.Scene is Level { Completed: true })) return null;
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
            return new(new(Bool(permissions, "restart_chapter"), Bool(permissions, "return_to_map"), Bool(permissions, "save_quit")),
                ReadEntry(data, "entry_target"), departure == "current_level");
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
        return new(sid, slot);
    }
}
