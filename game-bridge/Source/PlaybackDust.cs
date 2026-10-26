using System;
using System.Runtime.CompilerServices;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>Complete dormant dust silhouettes in both vanilla body and edge-mask passes.</summary>
internal static class PlaybackDust {
    private sealed record Cached(long Frame, Level Level, string Room, Vector2 Position, DustVisualPlan.Plan Plan);
    private static ConditionalWeakTable<DustGraphic, Cached> cache = new();
    public static int Builds { get; private set; }
    public static int Draws { get; private set; }
    public static int EyeDraws { get; private set; }

    public static void Clear() { cache = new(); Builds = 0; Draws = 0; EyeDraws = 0; }

    internal static bool NeedsFallback(bool showing, DustGraphic graphic) {
        if (!showing || graphic.Estableshed || graphic.nodes.Count != 0 || !graphic.InView) return false;
        // Unknown mod callbacks may define different establishment rules.
        Type type = graphic.Entity.GetType();
        return type == typeof(DustStaticSpinner) || type == typeof(DustRotateSpinner) || type == typeof(DustTrackSpinner);
    }

    public static void Render(On.Celeste.DustGraphic.orig_Render orig, DustGraphic graphic) {
        if (!NeedsFallback(Playback.ShowSpinnerVisuals, graphic) || graphic.Scene is not Level level) {
            orig(graphic);
            return;
        }
        var bases = GFX.Game.GetAtlasSubtextures("danger/dustcreature/base");
        var overlays = GFX.Game.GetAtlasSubtextures("danger/dustcreature/overlay");
        var plan = GetPlan(graphic, level, bases.Count, overlays.Count);
        Draws++;
        Vector2 position = graphic.RenderPosition + plan.Offset;
        foreach (var node in plan.Nodes) {
            if (!node.Enabled) continue;
            Vector2 p = position + node.Angle * graphic.Scale;
            bases[node.Base].DrawCentered(p, Color.White, graphic.Scale, node.Rotation);
            overlays[node.Overlay].DrawCentered(p, Color.White, graphic.Scale, -node.Rotation);
        }
        graphic.center.DrawCentered(position, Color.White, graphic.Scale, graphic.timer);
    }

    private static DustVisualPlan.Plan GetPlan(DustGraphic graphic, Level level, int baseCount, int overlayCount) {
        if (!cache.TryGetValue(graphic, out Cached? entry) || entry.Frame != Bridge.Frame
            || !ReferenceEquals(entry.Level, level) || entry.Room != level.Session.Level || entry.Position != graphic.Entity.Position) {
            var plan = DustVisualPlan.Build(graphic, level, baseCount, overlayCount);
            cache.Remove(graphic);
            entry = new Cached(Bridge.Frame, level, level.Session.Level, graphic.Entity.Position, plan);
            cache.Add(graphic, entry);
            Builds++;
        }
        return entry.Plan;
    }

    public static void CollectEyes(Level level) {
        foreach (Component component in level.Tracker.GetComponents<DustEdge>()) {
            if (component.Entity.Get<DustGraphic>() is not {} graphic
                || !NeedsFallback(Playback.ShowSpinnerVisuals, graphic)) continue;
            var plan = GetPlan(graphic, level, GFX.Game.GetAtlasSubtextures("danger/dustcreature/base").Count,
                                GFX.Game.GetAtlasSubtextures("danger/dustcreature/overlay").Count);
            if (DustVisualPlan.BuildEyes(graphic, plan) is not {} eyes) continue;
            // Vanilla Eyeballs does not inherit its owner's excluded tags.
            PlaybackVisualPass.Add(eyes.Depth, _ => DrawEyes(eyes));
        }
    }

    private static void DrawEyes(DustVisualPlan.Eyes eyes) {
        if (eyes.LeftVisible) eyes.Texture.DrawCentered(eyes.Left, eyes.Color, eyes.Scale);
        if (eyes.RightVisible) eyes.Texture.DrawCentered(eyes.Right, eyes.Color, eyes.Scale);
        if (eyes.LeftVisible || eyes.RightVisible) EyeDraws++;
    }
}
