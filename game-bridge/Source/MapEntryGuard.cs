using System;

namespace CelesteAI.Bridge;

/// <summary>Validate the destination after native navigation/confirmation dispatch.</summary>
internal sealed record MapEntryGuard(string? Sid, int? Slot) {
    public static bool Available { get; set; }
    public ActionBlock? Check(string panelSid, int? slot) {
        if (Slot.HasValue && Slot != slot)
            return new("entry_blocked", "target.save_slot", "enter_map", "Save slot does not match the task", Slot, slot);
        if (Sid != null && !string.Equals(Sid, panelSid, StringComparison.Ordinal))
            return new("entry_blocked", "target.map", "enter_map", "Map does not match the task; choose the requested chapter", Sid, panelSid);
        return null;
    }
}
