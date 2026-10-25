using System;
using System.Collections.Generic;
using Celeste;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>冻结镜头回放的视觉代理：不调用 Update/CreateSprites，不更改实体或场景队列。</summary>
internal static class PlaybackSpinners {
    private readonly record struct Piece(MTexture Texture, Vector2 Offset, Vector2 Origin, float Rotation, Color Color);
    private static readonly Dictionary<CrystalStaticSpinner, List<Piece>> Cache = new();
    public static int PeakCount { get; private set; }

    public static void Clear() { Cache.Clear(); PeakCount = 0; }

    public static bool NeedsFallback(bool visible, bool expanded, Vector2 position, Vector2 camera) =>
        (!visible || !expanded) && position.X > camera.X - 32 && position.X < camera.X + 352
        && position.Y > camera.Y - 32 && position.Y < camera.Y + 212;

    public static void DrawMissing(Level level) {
        int count = 0;
        foreach (Entity entity in level.Tracker.GetEntities<CrystalStaticSpinner>()) {
            var spinner = (CrystalStaticSpinner)entity;
            if (!NeedsFallback(spinner.Visible, spinner.expanded, spinner.Position, level.Camera.Position)) continue;
            if (!Cache.TryGetValue(spinner, out var pieces)) Cache[spinner] = pieces = Build(spinner, level);
            if (pieces.Count > 0) count++;
            // 与原版一样给贴图加黑边；仅画代理，不设置 Visible/Collidable，也不添加 filler/border 实体。
            foreach (Piece piece in pieces) {
                Vector2 p = spinner.Position + piece.Offset;
                foreach (Vector2 d in Outline) piece.Texture.Draw(p + d, piece.Origin, Color.Black, 1f, piece.Rotation);
            }
            foreach (Piece piece in pieces)
                piece.Texture.Draw(spinner.Position + piece.Offset, piece.Origin, piece.Color, 1f, piece.Rotation);
        }
        PeakCount = Math.Max(PeakCount, count);
    }

    private static readonly Vector2[] Outline = {new(0,-1),new(0,1),new(-1,0),new(1,0)};

    private static List<Piece> Build(CrystalStaticSpinner spinner, Level level) {
        var pieces = new List<Piece>();
        if (spinner.expanded) {
            // 已经生成过贴图，只是被原镜头隐藏：复用它们的贴图与颜色，不重复画原有 filler。
            foreach (Component component in spinner.Components)
                if (component is Image image && image.Visible)
                    pieces.Add(new Piece(image.Texture, image.Position, image.Origin, image.Rotation, image.Color));
            return pieces;
        }
        // 采用原版资源、随机种子和四象限裁切，独立 Random 不消耗游戏的随机数。
        var random = new Random(spinner.randomSeed);
        var foreground = GFX.Game.GetAtlasSubtextures(CrystalStaticSpinner.fgTextureLookup[spinner.color]);
        MTexture texture = foreground[random.Next(foreground.Count)];
        Color color = spinner.color == CrystalColor.Rainbow ? spinner.GetHue(spinner.Position) : Color.White;
        void Quadrant(int x, int y, Vector2 origin, Vector2 check) {
            if (!spinner.SolidCheck(spinner.Position + check))
                pieces.Add(new Piece(texture.GetSubtexture(x,y,14,14), Vector2.Zero, origin, 0f, color));
        }
        Quadrant(0,0,new(12,12),new(-4,-4));
        Quadrant(10,0,new(2,12),new(4,-4));
        Quadrant(10,10,new(2,2),new(4,4));
        Quadrant(0,10,new(12,2),new(-4,4));
        var background = GFX.Game.GetAtlasSubtextures(CrystalStaticSpinner.bgTextureLookup[spinner.color]);
        foreach (Entity entity in level.Tracker.GetEntities<CrystalStaticSpinner>()) {
            var other = (CrystalStaticSpinner)entity;
            if (other.ID <= spinner.ID || other.AttachToSolid != spinner.AttachToSolid
                || Vector2.DistanceSquared(other.Position,spinner.Position) >= 576f) continue;
            MTexture connector = background[random.Next(background.Count)];
            Vector2 offset = (other.Position - spinner.Position) / 2;
            Color ink = spinner.color == CrystalColor.Rainbow ? spinner.GetHue(spinner.Position+offset) : Color.White;
            pieces.Insert(0,new Piece(connector,offset,new Vector2(connector.Width,connector.Height)/2,
                                      random.Next(4)*MathHelper.PiOver2,ink));
        }
        return pieces;
    }
}
