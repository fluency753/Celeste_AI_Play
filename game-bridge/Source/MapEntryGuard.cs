using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Serialization;

namespace CelesteAI.Bridge;

/// <summary>Validate the destination after native navigation/confirmation dispatch.</summary>
internal sealed record MapIdentity([property: JsonPropertyName("sid")] string Sid,
    [property: JsonPropertyName("side")] string Side);

internal sealed record MapEntryGuard(string? Sid, int? Slot, IReadOnlyList<MapIdentity>? Maps = null) {
    public static bool Available { get; set; }
    public ActionBlock? CheckSlot(int? slot) {
        if (Slot.HasValue && Slot != slot)
            return new("entry_blocked", "target.save_slot", "enter_map", "Save slot does not match the task", Slot, slot);
        return null;
    }

    public ActionBlock? Check(string panelSid, int? slot, string side = "A") {
        if (CheckSlot(slot) is { } blocked) return blocked;
        if (Sid != null && !string.Equals(Sid, panelSid, StringComparison.Ordinal))
            return new("entry_blocked", "target.map", "enter_map", "Map does not match the task; choose the requested chapter", Sid, panelSid);
        if (Maps != null && !Maps.Any(map => map.Sid == panelSid && map.Side == side))
            return new("entry_blocked", "target.maps", "enter_map", "Destination is outside the allowed visited maps", Maps, new MapIdentity(panelSid, side));
        return null;
    }
}
