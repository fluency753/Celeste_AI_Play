using System;
using System.Collections.Generic;
using Celeste;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>冻结镜头回放的视觉代理：不调用 Update/CreateSprites，不更改实体或场景队列。</summary>
internal static class PlaybackSpinners {
    private readonly record struct Piece(MTexture Texture, Vector2 Offset, Vector2 Origin, float Rotation, Color Color,
                                         int Layer, Vector2 Scale, SpriteEffects Effects, bool DrawColor = true);
    private readonly record struct Command(CrystalStaticSpinner Owner, Piece Piece, bool Border);
    private static readonly Dictionary<CrystalStaticSpinner, List<Piece>> Cache = new();
    private static readonly RenderDepthQueue<Command> Queue = new();
    private static EntityList? activeEntities;
    public static int PeakCount { get; private set; }

    public static void Clear() { EndPassScope(); Cache.Clear(); PeakCount = 0; }

    public static bool NeedsFallback(bool visible, bool expanded, Vector2 position, Vector2 camera) =>
        (!visible || !expanded) && position.X > camera.X - 32 && position.X < camera.X + 352
        && position.Y > camera.Y - 32 && position.Y < camera.Y + 212;

    public static void BeginPass(Level level) {
        EndPassScope();
        activeEntities = level.Entities;
        int count = 0;
        foreach (Entity entity in level.Tracker.GetEntities<CrystalStaticSpinner>()) {
            var spinner = (CrystalStaticSpinner)entity;
            if (!NeedsFallback(spinner.Visible, spinner.expanded, spinner.Position, level.Camera.Position)) continue;
            if (!Cache.TryGetValue(spinner, out var pieces)) Cache[spinner] = pieces = Build(spinner, level);
            if (pieces.Count > 0) count++;
            // 原版边框 Depth+2，连接装饰 Depth+1，主体 Depth；必须在所有实体中统一排序。
            foreach (Piece piece in pieces) {
                Queue.Add(spinner.actualDepth+2,new Command(spinner,piece,true));
                if (piece.DrawColor) Queue.Add(spinner.actualDepth+piece.Layer,new Command(spinner,piece,false));
            }
        }
        Queue.Sort();
        PeakCount = Math.Max(PeakCount, count);
    }

    public static void BeforeEntity(Entity entity, EntityList entities, int excludedTags) {
        if (ReferenceEquals(entities,activeEntities)) Queue.Before(entity.actualDepth,command => Draw(command,excludedTags));
    }

    public static void FinishPass(EntityList entities, int excludedTags) {
        if (ReferenceEquals(entities,activeEntities)) Queue.Finish(command => Draw(command,excludedTags));
    }

    public static void EndPassScope() { activeEntities = null; Queue.Clear(); }

    private static void Draw(Command command, int excludedTags) {
        if (command.Owner.TagCheck(excludedTags)) return;
        Piece piece = command.Piece;
        Vector2 p = command.Owner.Position+piece.Offset;
        if (command.Border) {
            foreach (Vector2 d in Outline)
                piece.Texture.Draw(p+d,piece.Origin,Color.Black,piece.Scale,piece.Rotation,piece.Effects);
        } else piece.Texture.Draw(p,piece.Origin,piece.Color,piece.Scale,piece.Rotation,piece.Effects);
    }

    private static readonly Vector2[] Outline = {new(0,-1),new(0,1),new(-1,0),new(1,0)};

    private static List<Piece> Build(CrystalStaticSpinner spinner, Level level) {
        var pieces = new List<Piece>();
        if (spinner.expanded) {
            // 已生成贴图但被原镜头隐藏：复用变换。原生 filler 若仍可见，只补缺失的边框，不重复画颜色。
            foreach (Component component in spinner.Components)
                if (component is Image image)
                    pieces.Add(new Piece(image.Texture,image.Position,image.Origin,image.Rotation,image.Color,
                                         0,image.Scale,image.Effects,image.Visible));
            if (spinner.filler is { } filler) {
                foreach (Component component in filler.Components)
                    if (component is Image image)
                        pieces.Add(new Piece(image.Texture,filler.Position-spinner.Position+image.Position,image.Origin,
                                             image.Rotation,image.Color,1,image.Scale,image.Effects,!filler.Visible && image.Visible));
            }
            return pieces;
        }
        // 采用原版资源、随机种子和四象限裁切，独立 Random 不消耗游戏的随机数。
        var random = new Random(spinner.randomSeed);
        var foreground = GFX.Game.GetAtlasSubtextures(CrystalStaticSpinner.fgTextureLookup[spinner.color]);
        MTexture texture = foreground[random.Next(foreground.Count)];
        Color color = spinner.color == CrystalColor.Rainbow ? spinner.GetHue(spinner.Position) : Color.White;
        void Quadrant(int x, int y, Vector2 origin, Vector2 check) {
            if (!spinner.SolidCheck(spinner.Position + check))
                pieces.Add(new Piece(texture.GetSubtexture(x,y,14,14), Vector2.Zero, origin, 0f, color,
                                     0,Vector2.One,SpriteEffects.None));
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
            pieces.Add(new Piece(connector,offset,new Vector2(connector.Width,connector.Height)/2,
                                 random.Next(4)*MathHelper.PiOver2,ink,1,Vector2.One,SpriteEffects.None));
        }
        return pieces;
    }
}
