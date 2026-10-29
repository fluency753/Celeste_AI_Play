using System;
using System.Collections.Generic;
using System.Linq;
using Celeste;

namespace CelesteAI.Bridge;

/// <summary>Positive evidence of entry, never the set of unlocked/selectable maps.</summary>
internal static class VisitedMaps {
    public static IReadOnlyList<MapIdentity> Read(SaveData save) {
        var found = new HashSet<MapIdentity>();
        void ReadAreas(IEnumerable<AreaStats>? areas) {
            if (areas == null) return;
            foreach (var area in areas) {
                string? sid = area.SID;
                if (string.IsNullOrEmpty(sid) && area.ID >= 0 && area.ID < AreaData.Areas.Count)
                    sid = AreaData.Areas[area.ID].SID;
                if (string.IsNullOrEmpty(sid) || area.Modes == null) continue;
                for (int mode = 0; mode < Math.Min(3, area.Modes.Length); mode++) {
                    var stats = area.Modes[mode];
                    if (stats != null && (stats.TimePlayed > 0 || stats.Deaths > 0 || stats.Completed ||
                            stats.SingleRunCompleted || stats.FullClear || stats.HeartGem || stats.TotalStrawberries > 0 ||
                            stats.BestTime > 0 || stats.BestFullClearTime > 0 ||
                            stats.Checkpoints?.Count > 0 || stats.Strawberries?.Count > 0))
                        found.Add(new(sid, Side((AreaMode)mode)));
                }
            }
        }
        // Areas is vanilla; LevelSets contains active mod sets. Recycle-bin data
        // is not currently playable and must not grant entry permissions.
        ReadAreas(save.Areas);
        if (save.LevelSets != null)
            foreach (var set in save.LevelSets) ReadAreas(set.Areas);
        // StartSession commits this identity before any gameplay time accrues.
        // LastArea has a default value on a new save and is not entry evidence.
        if ((save.CurrentSession_Safe ?? save.CurrentSession) is { } session && !string.IsNullOrEmpty(session.Area.SID))
            found.Add(new(session.Area.SID, Side(session.Area.Mode)));
        return found.OrderBy(map => map.Sid, StringComparer.Ordinal).ThenBy(map => map.Side, StringComparer.Ordinal).ToArray();
    }

    public static IReadOnlyList<MapIdentity>? ReadCandidate(OuiFileSelectSlot slot) {
        if (!slot.Exists || slot.Corrupted || slot.SaveData == null) return null;
        try { return Read(slot.SaveData); }
        catch (Exception) { return null; } // Unknown is distinct from an empty, valid history.
    }

    private static string Side(AreaMode mode) => mode switch {
        AreaMode.Normal => "A", AreaMode.BSide => "B", _ => "C",
    };
}
