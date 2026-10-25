using System;
using Celeste;

namespace CelesteAI.Bridge;

internal static class RoomAreaSelection {
    public static AreaKey Select(AreaKey current, int? chapter, int? mode) {
        int id = chapter ?? current.ID;
        int side = mode ?? (chapter.HasValue ? 0 : (int)current.Mode);
        if (id < 0 || side < 0 || side > 2)
            throw new ArgumentException("chapter must be nonnegative; mode must be 0 (A), 1 (B), or 2 (C)");
        return new AreaKey(id, (AreaMode)side);
    }
}
