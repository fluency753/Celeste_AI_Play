using System;

namespace CelesteAI.Bridge;

/// <summary>Check the actual chapter panel before feeding its confirmation key.</summary>
internal sealed record MapEntryGuard(string? Sid, int? Slot) {
    public string? Check(InputFrame input, string panelSid, int? slot) {
        bool confirms = input.Has(Act.Confirm) || (input.Tas && input.Has(Act.Jump));
        if (!confirms) return null;
        if (Slot.HasValue && Slot != slot)
            return $"entry_blocked: save slot {slot} does not match required slot {Slot}";
        if (Sid != null && !string.Equals(Sid, panelSid, StringComparison.Ordinal))
            return $"entry_blocked: map {panelSid} does not match required map {Sid}; navigate back and choose the requested chapter";
        return null;
    }
}
