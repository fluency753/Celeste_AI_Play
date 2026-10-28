using System;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using Celeste;
using Celeste.Mod;
using Monocle;

namespace CelesteAI.Bridge;

// The payload owns game policy and identity. Runtime only keeps this opaque
// snapshot and verifies whether the original scene is replaced or preserved.
internal static class ReloadBoundary {
    internal sealed record SaveIdentity(int slot, string name);
    internal sealed record SessionIdentity(string sid, string side, string room, long chapterTimeTicks);
    internal sealed record Identity(string mode, string? scene, SaveIdentity? save, SessionIdentity? session,
        [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)] bool requires_origin_hold = false);

    // loaded marks the end of native background construction. The postcard
    // field survives RemoveSelf, so ownership is required, not just visibility.
    internal static bool LoaderPrompt(Scene? scene) => scene is OverworldLoader && StoryText.ScenePostcard(scene) is { Visible: true };
    internal static bool LoaderPresentation(Scene? scene) => scene is OverworldLoader loader &&
        Volatile.Read(ref loader.loaded) && LoaderPrompt(loader);

    internal static bool AutomaticLoader => Engine.Scene is LevelLoader or LevelExit or GameLoader or
        AssetReloadHelper || Engine.Scene is OverworldLoader && !LoaderPresentation(Engine.Scene);

    internal static bool Waiting => UserIO.Saving || Engine.Scene == null || AutomaticLoader;

    internal static Identity Capture() {
        Session? session = Engine.Scene switch {
            Level level => level.Session,
            AreaComplete results => results.Session,
            LevelEnter enter => enter.session,
            _ => SaveData.Instance?.CurrentSession_Safe,
        };
        return new(Waiting ? "wait" : Engine.Scene is Level ? "replace" : "preserve",
            Engine.Scene?.GetType().FullName,
            SaveData.Instance is { } save ? new(save.FileSlot, save.Name) : null,
            session == null ? null : new(session.Area.SID,
                session.Area.Mode switch { AreaMode.Normal => "A", AreaMode.BSide => "B", _ => "C" },
                session.Level, session.Time), LoaderPresentation(Engine.Scene));
    }

    internal static void Write(Utf8JsonWriter writer) {
        writer.WritePropertyName("reload_boundary");
        JsonSerializer.Serialize(writer, Capture());
    }

    internal static void Verify(JsonElement identity) {
        Identity current = Capture();
        if (identity.TryGetProperty("mode", out _)) {
            if (JsonSerializer.Deserialize<Identity>(identity.GetRawText()) != current)
                throw new InvalidOperationException("Reloaded scene, save or chapter identity does not match the bridge snapshot");
            return;
        }
        // Older schema-1 snapshots had a flat Level/AreaComplete identity.
        string scene = identity.TryGetProperty("scene", out var kind) ? kind.GetString() ?? "" : "Level";
        Session? session = Engine.Scene switch { Level level => level.Session, AreaComplete results => results.Session, _ => null };
        if (session == null || Engine.Scene.GetType().Name != scene ||
                session.Area.SID != identity.GetProperty("sid").GetString() ||
                (int)session.Area.Mode != identity.GetProperty("side").GetInt32() ||
                session.Level != identity.GetProperty("room").GetString() ||
                session.Time != identity.GetProperty("time").GetInt64() ||
                (SaveData.Instance?.FileSlot) != (identity.GetProperty("slot").ValueKind == JsonValueKind.Null
                    ? (int?)null : identity.GetProperty("slot").GetInt32()))
            throw new InvalidOperationException("Reloaded map, side, room, save slot or chapter time does not match the bridge snapshot");
    }
}
