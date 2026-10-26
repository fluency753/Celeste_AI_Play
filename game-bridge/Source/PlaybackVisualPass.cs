using System;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>One depth merge for every supplemental gameplay visual, outside pre-render masks.</summary>
internal static class PlaybackVisualPass {
    private static readonly RenderDepthQueue<Action<int>> queue = new();
    private static EntityList? entities;

    public static void Begin(Level level) {
        End();
        entities = level.Entities;
        PlaybackSpinners.Collect(level);
        PlaybackDust.CollectEyes(level);
        queue.Sort();
    }

    public static void Add(double depth, Action<int> draw) => queue.Add(depth, draw);

    public static void BeforeEntity(Entity entity, EntityList list, int excludedTags) {
        if (ReferenceEquals(list, entities)) queue.Before(entity.actualDepth, draw => draw(excludedTags));
    }

    public static void Finish(EntityList list, int excludedTags) {
        if (ReferenceEquals(list, entities)) queue.Finish(draw => draw(excludedTags));
    }

    public static void End() { entities = null; queue.Clear(); }
}
