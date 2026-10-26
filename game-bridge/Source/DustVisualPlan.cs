using System;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>Read-only counterpart of vanilla dust establishment, without scene or RNG side effects.</summary>
internal static class DustVisualPlan {
    internal readonly record struct Node(int Base, int Overlay, float Rotation, Vector2 Angle, bool Enabled);
    internal sealed record Plan(Node[] Nodes, Vector2 Offset, Vector2 EyeDirection);
    internal sealed record Eyes(MTexture Texture, Color Color, Vector2 Left, Vector2 Right,
                                bool LeftVisible, bool RightVisible, float Scale, double Depth);

    // Eyeballs are an independent entity in vanilla: this plan is consumed only
    // by the gameplay depth pass, never by the DustEdges silhouette callback.
    public static Eyes? BuildEyes(DustGraphic graphic, Plan plan) {
        if (!graphic.eyesExist || !graphic.Visible || !graphic.Entity.Visible) return null;
        var style = DustStyles.Get(graphic.Scene);
        var textures = GFX.Game.GetAtlasSubtextures(style.EyeTextures);
        Vector2 perpendicular = new Vector2(-plan.EyeDirection.Y, plan.EyeDirection.X).SafeNormalize();
        Vector2 center = graphic.RenderPosition + plan.Offset;
        return new Eyes(textures[graphic.eyeTextureIndex % textures.Count], style.EyeColor,
            center + (plan.EyeDirection * 5 + perpendicular * 3) * graphic.Scale,
            center + (plan.EyeDirection * 5 - perpendicular * 3) * graphic.Scale,
            graphic.leftEyeVisible, graphic.rightEyeVisible, graphic.Scale, graphic.Entity.actualDepth - 1);
    }

    public static Plan Build(DustGraphic graphic, Level level, int baseCount, int overlayCount) {
        Entity owner = graphic.Entity;
        var random = new Random(graphic.randomSeed);
        var nodes = new Node[4];
        Vector2 diagonal = new Vector2(1, 1).SafeNormalize();
        int x = (int)owner.X, y = (int)owner.Y;
        bool Solid(Rectangle area) => level.CollideCheck<Solid>(area);
        bool Neighbour(Rectangle area) {
            if (Solid(area)) return true;
            foreach (Entity other in level.Tracker.GetEntities<DustStaticSpinner>())
                if (!ReferenceEquals(owner, other) && other.Collidable && Collide.CheckRect(other, area)) return true;
            return false;
        }
        for (int i = 0; i < 4; i++) {
            int sx = i % 2 == 0 ? -1 : 1, sy = i < 2 ? -1 : 1;
            bool enabled = graphic.ignoreSolids || !Solid(new Rectangle(x + (sx < 0 ? -8 : 0), y + (sy < 0 ? -8 : 0), 8, 8));
            Vector2 stretch = Vector2.One;
            if (graphic.autoExpandDust) {
                if (Neighbour(new Rectangle((int)(owner.X - 4 + sx * 16), (int)(owner.Y - 4 + sy * 4), 8, 8))) stretch.X = 5;
                if (Neighbour(new Rectangle((int)(owner.X - 4 + sx * 4), (int)(owner.Y - 4 + sy * 16), 8, 8))) stretch.Y = 5;
            }
            nodes[i] = new Node(random.Next(baseCount), random.Next(overlayCount),
                (float)random.NextDouble() * (MathF.PI * 2), diagonal * new Vector2(sx, sy) * stretch, enabled);
        }
        // Establishment first shifts the graphic using the original quadrant
        // mask; the track-specific clipping below must not recompute this shift.
        Vector2 offset = new(
            (nodes[1].Enabled || nodes[3].Enabled ? 1 : 0) - (nodes[0].Enabled || nodes[2].Enabled ? 1 : 0),
            (nodes[2].Enabled || nodes[3].Enabled ? 1 : 0) - (nodes[0].Enabled || nodes[1].Enabled ? 1 : 0));
        Vector2 eyeDirection = graphic.EyeDirection;
        int enabledCount = 0;
        Vector2 eyeRange = Vector2.Zero;
        for (int i = 0; i < nodes.Length; i++) if (nodes[i].Enabled) {
            enabledCount++;
            eyeRange += diagonal * new Vector2(i % 2 == 0 ? -1 : 1, i < 2 ? -1 : 1);
        }
        if (graphic.autoControlEyes && graphic.eyesExist && enabledCount < 4) {
            if (enabledCount > 0 && eyeRange.Length() > 0) eyeRange = (eyeRange / enabledCount).SafeNormalize();
            eyeDirection = eyeRange;
        }
        if (owner is DustTrackSpinner track) {
            Vector2 along = (track.End - track.Start).SafeNormalize();
            Vector2 normal = new(-along.Y, along.X);
            bool Wall(Vector2 at) => Solid(new Rectangle((int)(owner.X + at.X) - 2, (int)(owner.Y + at.Y) - 2, 4, 4));
            bool continuous = Wall(normal * 4);
            if (!continuous) { normal = -normal; continuous = Wall(normal * 4); }
            for (int step = 8; step < (track.End - track.Start).Length() && continuous; step += 8)
                continuous = Wall(normal * 4 + along * step);
            if (continuous) {
                for (int i = 0; i < nodes.Length; i++) {
                    bool wallSide = normal.X < 0 ? i % 2 == 0 : normal.X > 0 ? i % 2 == 1
                        : normal.Y < 0 ? i < 2 : normal.Y > 0 && i >= 2;
                    if (wallSide) nodes[i] = nodes[i] with { Enabled = false };
                }
                offset -= normal;
                Vector2 outward = -normal;
                eyeDirection = Calc.AngleToVector(Calc.AngleLerp(outward.Angle(),
                    track.Up ? track.Angle + MathF.PI : track.Angle, 0.3f), 1);
            }
        }
        return new Plan(nodes, offset, eyeDirection);
    }
}
