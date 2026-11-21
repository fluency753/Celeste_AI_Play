using System;
using System.Collections.Generic;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 规划回放的镜头在别的房间时（智能体从检查点规划下一个房间），按地图数据补画那个房间的实体与贴花。
/// 游戏只载入当前房间的实体，地形是整张地图一起建的，所以别的房间原本只看得见地形。
/// 这里只画、不建：不创建实体、不碰场景与追踪器。要随机选的贴图（尖刺、平台、水晶）用单独的发生器，种子取房间名与实体编号；
/// 自动拼砖与精灵在 Calc.PushRandom 里生成；游戏的随机数一点不动。画的是房间刚载入时的样子（落块在原处、移动块在起点、
/// 机关不动），已经拿走的收集品（会话的 DoNotLoad）不画。认识的实体照游戏的贴图与摆法画，其余有大小的方块、机关画一个轮廓。
/// 与水晶补画走同一条按深度合并的绘制通道（PlaybackVisualPass），只在回放画面里出现，观测截图里没有。
/// </summary>
internal static class PlaybackRoomGhost {
    /// <summary>补画的一项：名字（实体类型或贴花路径）、深度、世界范围（检查用）与画法（第一次画时才取贴图）。</summary>
    internal sealed record Item(string Name, double Depth, Rectangle Bounds, Action Draw);

    /// <summary>生成补画清单要用到的房间以外的信息（游戏里取自当前关卡，原生检查里直接给）。</summary>
    internal sealed record Context(int AreaId, string Spike, string Jumpthru, string Crumble, bool HasCassette,
                                   Func<EntityID, bool> Skip, Func<Rectangle, bool> Solid,
                                   VirtualMap<char>? Solids, Rectangle TileBounds);

    private static readonly List<Item> Items = new();
    private static readonly List<Action<int>> Queued = new();   // 每一项排进绘制通道用的委托（建一次，免得每帧新建）
    private static readonly HashSet<Item> Broken = new();
    private static string? prepared;

    public static int Count => Items.Count;
    public static string? Room => prepared;

    public static void Clear() {
        Items.Clear();
        Queued.Clear();
        Broken.Clear();
        prepared = null;
    }

    /// <summary>这一轮回放的镜头房间（空矩形表示当前房间）：是别的房间时按地图数据准备补画清单，同一个房间不重建；否则清空。</summary>
    public static void Prepare(Level level, Rectangle room) {
        if (room.Width <= 0 || room.Height <= 0 || level.Session?.MapData?.Levels is not { } rooms) {
            Clear();
            return;
        }
        LevelData? data = null;
        foreach (LevelData candidate in rooms) {
            if (candidate.Bounds == room) {
                data = candidate;
                break;
            }
        }
        if (data == null) {
            Clear();
            return;
        }
        if (prepared == data.Name) return;
        Clear();
        try {
            Session session = level.Session;
            AreaData area = AreaData.Get(level);
            var context = new Context(session.Area.ID, area.Spike, area.Jumpthru, area.CrumbleBlock, session.Cassette,
                id => session.DoNotLoad.Contains(id), rect => level.CollideCheck<Solid>(rect),
                level.SolidsData, session.MapData.TileBounds);
            Items.AddRange(Plan(data, context));
            prepared = data.Name;
        } catch (Exception e) {
            Log.Info($"playback room ghost unavailable for {data.Name}: {e.Message}");
            Clear();
        }
    }

    /// <summary>每次渲染（PlaybackVisualPass.Begin）：按深度排进实体绘制。画出错的一项以后不再画。</summary>
    public static void Collect() {
        if (Queued.Count != Items.Count) {
            Queued.Clear();
            foreach (Item item in Items) Queued.Add(_ => DrawSafely(item));
        }
        for (int i = 0; i < Items.Count; i++) {
            if (!Broken.Contains(Items[i])) PlaybackVisualPass.Add(Items[i].Depth, Queued[i]);
        }
    }

    private static void DrawSafely(Item item) {
        if (Broken.Contains(item)) return;
        try {
            item.Draw();
        } catch (Exception e) {
            Broken.Add(item);
            Log.Info($"playback room ghost: {item.Name} not drawn: {e.Message}");
        }
    }

    // ---- 补画清单（纯数据：不取贴图、不碰随机数） ----

    internal static List<Item> Plan(LevelData room, Context ctx) {
        var items = new List<Item>();
        var offset = new Vector2(room.Bounds.Left, room.Bounds.Top);
        int salt = StableHash(room.Name);
        foreach (EntityData e in room.Entities) {
            if (ctx.Skip(new EntityID(room.Name, e.ID))) continue;
            var rng = new Random(unchecked(salt * 31 + e.ID * 7919 + 17));
            AddEntity(items, room, e, e.Position + offset, offset, ctx, rng);
        }
        foreach (DecalData d in room.BgDecals) AddDecal(items, d, offset, d.Depth ?? 9000);
        foreach (DecalData d in room.FgDecals) AddDecal(items, d, offset, d.Depth ?? -10500);
        return items;
    }

    private static void AddEntity(List<Item> items, LevelData room, EntityData e, Vector2 p, Vector2 offset, Context ctx, Random rng) {
        switch (e.Name) {
            case "spikesUp": Spikes(items, e, p, "up", ctx, rng); break;
            case "spikesDown": Spikes(items, e, p, "down", ctx, rng); break;
            case "spikesLeft": Spikes(items, e, p, "left", ctx, rng); break;
            case "spikesRight": Spikes(items, e, p, "right", ctx, rng); break;
            case "jumpThru": Jumpthru(items, e, p, ctx, rng); break;
            case "crumbleBlock": Crumble(items, e, p, ctx); break;
            case "fallingBlock": Tiles(items, e, p, e.Char("tiletype", '3'), false, e.Bool("behind") ? 5000 : 0, ctx, rng); break;
            case "dashBlock": {
                bool blend = e.Bool("blendin");
                Tiles(items, e, p, e.Char("tiletype", '3'), blend, blend ? -10501 : -12999, ctx, rng);
                break;
            }
            case "crumbleWallOnRumble": {
                bool blend = e.Bool("blendin");
                Tiles(items, e, p, e.Char("tiletype", 'm'), blend, blend ? -10501 : -12999, ctx, rng);
                break;
            }
            case "fakeWall": Tiles(items, e, p, e.Char("tiletype", '3'), true, -13000, ctx, rng); break;
            case "fakeBlock": Tiles(items, e, p, e.Char("tiletype", '3'), false, -13000, ctx, rng); break;
            case "coverupWall": Tiles(items, e, p, e.Char("tiletype", '3'), true, -13000, ctx, rng); break;
            case "exitBlock": Tiles(items, e, p, e.Char("tileType", '3'), true, -13000, ctx, rng); break;
            case "zipMover": Zip(items, e, p, offset); break;
            case "strawberry": Bank(items, e.Name, p, e.Bool("moon") ? "moonberry" : "strawberry", -100, rng); break;
            case "spring": Spring(items, e, p, 0f); break;
            case "wallSpringLeft": Spring(items, e, p, MathF.PI / 2f); break;
            case "wallSpringRight": Spring(items, e, p, -MathF.PI / 2f); break;
            case "refill": Refill(items, e, p); break;
            case "cassetteBlock": CassetteBlock(items, room, e, p, offset); break;
            case "cassette": if (!ctx.HasCassette) Bank(items, e.Name, p, "cassette", -100, rng); break;
            case "wire": Wire(items, e, p, offset); break;
            case "spinner": Spinner(items, room, e, p, ctx, rng); break;
            case "booster": Bank(items, e.Name, p, e.Bool("red") ? "boosterRed" : "booster", -8500, rng); break;
            case "feather": Bank(items, e.Name, p, "flyFeather", 0, rng); break;
            case "bumper": Bank(items, e.Name, p, "bumper", 0, rng); break;
            case "key": Bank(items, e.Name, p, "key", 0, rng); break;
            case "glider": Bank(items, e.Name, p, "glider", -5, rng); break;
            case "flutterbird": Bank(items, e.Name, p, "flutterbird", -9999, rng); break;
            case "bonfire": Bank(items, e.Name, p, "campfire", -5, rng); break;
            case "memorial": Texture(items, e.Name, p, "scenery/memorial/memorial", new Vector2(0.5f, 1f), 100); break;
            case "dreamBlock": Dream(items, e, p); break;
            default: Outline(items, e, p); break;
        }
    }

    private static void Spikes(List<Item> items, EntityData e, Vector2 p, string dir, Context ctx, Random rng) {
        bool vertical = dir is "left" or "right";
        int size = vertical ? e.Height : e.Width;
        string type = e.Attr("type", "default");
        if (string.IsNullOrEmpty(type) || type == "default") type = ctx.Spike;
        if (type == "tentacles" || size < 8) {
            Outline(items, e, p);
            return;
        }
        string path = "danger/spikes/" + type + "_" + dir;
        int n = size / 8;
        var picks = new int[n];
        for (int i = 0; i < n; i++) picks[i] = rng.Next(1 << 20);
        Rectangle bounds = dir switch {
            "up" => new Rectangle((int)p.X, (int)p.Y - 3, size, 3),
            "down" => new Rectangle((int)p.X, (int)p.Y, size, 3),
            "left" => new Rectangle((int)p.X - 3, (int)p.Y, 3, size),
            _ => new Rectangle((int)p.X, (int)p.Y, 3, size),
        };
        List<MTexture>? textures = null;
        items.Add(new Item(e.Name, -1, bounds, () => {
            textures ??= GFX.Game.GetAtlasSubtextures(path);
            if (textures.Count == 0) return;
            for (int j = 0; j < n; j++) {
                MTexture t = textures[picks[j] % textures.Count];
                float c = (j + 0.5f) * 8f;
                switch (dir) {
                    case "up": t.DrawJustified(p + new Vector2(c, 1f), new Vector2(0.5f, 1f)); break;
                    case "down": t.DrawJustified(p + new Vector2(c, -1f), new Vector2(0.5f, 0f)); break;
                    case "right": t.DrawJustified(p + new Vector2(-1f, c), new Vector2(0f, 0.5f)); break;
                    default: t.DrawJustified(p + new Vector2(1f, c), new Vector2(1f, 0.5f)); break;
                }
            }
        }));
    }

    private static void Jumpthru(List<Item> items, EntityData e, Vector2 p, Context ctx, Random rng) {
        int columns = e.Width / 8;
        if (columns <= 0) return;
        string texture = e.Attr("texture", "default");
        if (string.IsNullOrEmpty(texture) || texture == "default") texture = ctx.Jumpthru;
        // 两端照游戏：挨着实心的一端用贴图的第一行，否则第二行；中间各列随机
        bool leftSolid = ctx.Solid(new Rectangle((int)p.X - 1, (int)p.Y, e.Width, 5));
        bool rightSolid = ctx.Solid(new Rectangle((int)p.X + 1, (int)p.Y, e.Width, 5));
        var across = new int[columns];
        var row = new int[columns];
        for (int i = 0; i < columns; i++) {
            across[i] = rng.Next(1 << 20);
            row[i] = rng.Next(2);
        }
        MTexture[]? pieces = null;
        items.Add(new Item(e.Name, -60, new Rectangle((int)p.X, (int)p.Y, e.Width, 8), () => {
            if (pieces == null) {
                MTexture sheet = GFX.Game["objects/jumpthru/" + texture];
                int num = Math.Max(1, sheet.Width / 8);
                pieces = new MTexture[columns];
                for (int i = 0; i < columns; i++) {
                    int x, y;
                    if (i == 0) {
                        x = 0;
                        y = leftSolid ? 0 : 1;
                    } else if (i == columns - 1) {
                        x = num - 1;
                        y = rightSolid ? 0 : 1;
                    } else {
                        x = num > 2 ? 1 + across[i] % (num - 2) : Math.Min(1, num - 1);
                        y = row[i];
                    }
                    pieces[i] = sheet.GetSubtexture(x * 8, y * 8, 8, 8);
                }
            }
            for (int i = 0; i < pieces.Length; i++) pieces[i].Draw(p + new Vector2(i * 8, 0f));
        }));
    }

    private static void Crumble(List<Item> items, EntityData e, Vector2 p, Context ctx) {
        if (e.Width <= 0) return;
        string texture = e.Attr("texture", "default");
        if (string.IsNullOrEmpty(texture) || texture == "default") texture = ctx.Crumble;
        int width = e.Width;
        MTexture[]? pieces = null;
        items.Add(new Item(e.Name, 0, new Rectangle((int)p.X, (int)p.Y, width, 8), () => {
            if (pieces == null) {
                MTexture sheet = GFX.Game["objects/crumbleBlock/" + texture];
                var list = new List<MTexture>();
                for (int j = 0; j < width; j += 8) list.Add(sheet.GetSubtexture((int)((Math.Abs(p.X) + j) / 8f) % 4 * 8, 0, 8, 8));
                pieces = list.ToArray();
            }
            for (int i = 0; i < pieces.Length; i++) pieces[i].DrawCentered(p + new Vector2(4 + i * 8, 4f));
        }));
    }

    /// <summary>用自动拼砖画的方块（落块、冲刺砖、假墙等）：blend 时与周围地形拼在一起（游戏的 GenerateOverlay）。</summary>
    private static void Tiles(List<Item> items, EntityData e, Vector2 p, char tile, bool blend, double depth, Context ctx, Random rng) {
        int tw = e.Width / 8, th = e.Height / 8;
        if (tw <= 0 || th <= 0) return;
        int seed = rng.Next();
        VirtualMap<char>? solids = ctx.Solids;
        Rectangle tileBounds = ctx.TileBounds;
        VirtualMap<MTexture>? grid = null;
        items.Add(new Item(e.Name, depth, new Rectangle((int)p.X, (int)p.Y, e.Width, e.Height), () => {
            if (grid == null) {
                Calc.PushRandom(seed);
                try {
                    grid = blend && solids != null
                        ? GFX.FGAutotiler.GenerateOverlay(tile, (int)(p.X / 8f) - tileBounds.Left, (int)(p.Y / 8f) - tileBounds.Top, tw, th, solids).TileGrid.Tiles
                        : GFX.FGAutotiler.GenerateBox(tile, tw, th).TileGrid.Tiles;
                } finally {
                    Calc.PopRandom();
                }
            }
            for (int x = 0; x < tw; x++)
                for (int y = 0; y < th; y++)
                    grid[x, y]?.Draw(p + new Vector2(x * 8, y * 8));
        }));
    }

    private static readonly Color RopeColor = Calc.HexToColor("663931");
    private static readonly Color RopeLightColor = Calc.HexToColor("9b6157");

    /// <summary>移动块（ZipMover）停在起点时的样子：轨道与两端齿轮（深度 5000），方块本身（-9999）。</summary>
    private static void Zip(List<Item> items, EntityData e, Vector2 p, Vector2 offset) {
        if (e.Nodes == null || e.Nodes.Length == 0 || e.Width <= 0 || e.Height <= 0) return;
        int w = e.Width, h = e.Height;
        bool moon = string.Equals(e.Attr("theme", "Normal"), "Moon", StringComparison.OrdinalIgnoreCase);
        string dir = moon ? "objects/zipmover/moon/" : "objects/zipmover/";
        var half = new Vector2(w / 2f, h / 2f);
        Vector2 from = p + half, to = e.Nodes[0] + offset + half;
        var span = Rectangle.Union(new Rectangle((int)from.X, (int)from.Y, 1, 1), new Rectangle((int)to.X, (int)to.Y, 1, 1));
        MTexture? cog = null;
        items.Add(new Item(e.Name + ":path", 5000, span, () => {
            cog ??= GFX.Game[dir + "cog"];
            ZipRope(from, to, cog, Vector2.UnitY, Color.Black);
            ZipRope(from, to, cog, Vector2.Zero, null);
            if (!moon) Draw.Rect(new Rectangle((int)p.X - 1, (int)p.Y - 1, w + 2, h + 2), Color.Black);
        }));
        List<(MTexture Texture, Vector2 At, Color Color)>? cogs = null;
        List<(MTexture Texture, Vector2 At)>? edges = null;
        MTexture? light = null;
        items.Add(new Item(e.Name, -9999, new Rectangle((int)p.X, (int)p.Y, w, h), () => {
            if (cogs == null || edges == null) {
                List<MTexture> inner = GFX.Game.GetAtlasSubtextures(dir + "innercog");
                var c = new List<(MTexture, Vector2, Color)>();
                int sign = 1;
                float turn = 0f;
                for (int i = 4; i <= h - 4; i += 8) {
                    int rowStart = sign;
                    for (int j = 4; j <= w - 4; j += 8) {
                        if (inner.Count > 0) {
                            MTexture t = inner[(int)(Mod(turn / (MathF.PI / 2f), 1f) * inner.Count) % inner.Count];
                            var r = new Rectangle(0, 0, t.Width, t.Height);
                            Vector2 z = Vector2.Zero;
                            if (j <= 4) { z.X = 2f; r.X = 2; r.Width -= 2; } else if (j >= w - 4) { z.X = -2f; r.Width -= 2; }
                            if (i <= 4) { z.Y = 2f; r.Y = 2; r.Height -= 2; } else if (i >= h - 4) { z.Y = -2f; r.Height -= 2; }
                            c.Add((t.GetSubtexture(r.X, r.Y, r.Width, r.Height), new Vector2(j, i) + z, Color.White * (sign < 0 ? 0.5f : 1f)));
                        }
                        sign = -sign;
                        turn += MathF.PI / 3f;
                    }
                    if (rowStart == sign) sign = -sign;
                }
                MTexture block = GFX.Game[dir + "block"];
                var ed = new List<(MTexture, Vector2)>();
                for (int k = 0; k < w / 8; k++) {
                    for (int l = 0; l < h / 8; l++) {
                        int a = k == 0 ? 0 : k == w / 8 - 1 ? 2 : 1;
                        int b = l == 0 ? 0 : l == h / 8 - 1 ? 2 : 1;
                        if (a != 1 || b != 1) ed.Add((block.GetSubtexture(a * 8, b * 8, 8, 8), new Vector2(k * 8, l * 8)));
                    }
                }
                List<MTexture> lights = GFX.Game.GetAtlasSubtextures(dir + "light");
                light = lights.Count > 1 ? lights[1] : lights.Count > 0 ? lights[0] : null;
                cogs = c;
                edges = ed;
            }
            Draw.Rect(p.X + 1f, p.Y + 1f, w - 2f, h - 2f, Color.Black);
            foreach (var (t, at, color) in cogs) t.DrawCentered(p + at, color);
            foreach (var (t, at) in edges) t.Draw(p + at);
            light?.Draw(p + new Vector2(w / 2f - light.Width / 2f, 0f));
        }));
    }

    private static void ZipRope(Vector2 from, Vector2 to, MTexture cog, Vector2 offset, Color? over) {
        Vector2 v = (to - from).SafeNormalize();
        Vector2 a = v.Perpendicular() * 3f, b = -v.Perpendicular() * 4f;
        Color rope = over ?? RopeColor, ropeLight = over ?? RopeLightColor;
        Draw.Line(from + a + offset, to + a + offset, rope);
        Draw.Line(from + b + offset, to + b + offset, rope);
        float length = (to - from).Length();
        for (float n = 4f; n < length; n += 4f) {
            Vector2 c = from + a + v.Perpendicular() + v * n, d = to + b - v * n;
            Draw.Line(c + offset, c + v * 2f + offset, ropeLight);
            Draw.Line(d + offset, d - v * 2f + offset, ropeLight);
        }
        cog.DrawCentered(from + offset, over ?? Color.White, 1f, 0f);
        cog.DrawCentered(to + offset, over ?? Color.White, 1f, 0f);
    }

    private static float Mod(float x, float m) => (x % m + m) % m;

    /// <summary>精灵库里的精灵（草莓、助推泡泡、羽毛等）：取默认动画的第一帧，照精灵自己的原点与缩放画。</summary>
    private static void Bank(List<Item> items, string name, Vector2 p, string id, double depth, Random rng) {
        int seed = rng.Next();
        bool resolved = false;
        MTexture? texture = null;
        Vector2 origin = Vector2.Zero, scale = Vector2.One, at = Vector2.Zero;
        float rotation = 0f;
        items.Add(new Item(name, depth, new Rectangle((int)p.X - 8, (int)p.Y - 8, 16, 16), () => {
            if (!resolved) {
                resolved = true;
                Calc.PushRandom(seed);
                try {
                    Sprite sprite = GFX.SpriteBank.Create(id);
                    texture = sprite.Texture;
                    if (texture == null) {
                        foreach (Sprite.Animation animation in sprite.Animations.Values) {
                            if (animation.Frames.Length > 0) {
                                texture = animation.Frames[0];
                                break;
                            }
                        }
                    }
                    origin = sprite.Origin;
                    scale = sprite.Scale;
                    rotation = sprite.Rotation;
                    at = sprite.Position;
                } finally {
                    Calc.PopRandom();
                }
            }
            texture?.Draw(p + at, origin, Color.White, scale, rotation);
        }));
    }

    private static void Texture(List<Item> items, string name, Vector2 p, string path, Vector2 justify, double depth) {
        MTexture? texture = null;
        items.Add(new Item(name, depth, new Rectangle((int)p.X - 8, (int)p.Y - 16, 16, 16), () => {
            texture ??= GFX.Game[path];
            texture.DrawJustified(p, justify);
        }));
    }

    private static void Spring(List<Item> items, EntityData e, Vector2 p, float rotation) {
        MTexture? texture = null;
        bool resolved = false;
        items.Add(new Item(e.Name, -8501, new Rectangle((int)p.X - 8, (int)p.Y - 8, 16, 16), () => {
            if (!resolved) {
                resolved = true;
                List<MTexture> frames = GFX.Game.GetAtlasSubtextures("objects/spring/");
                texture = frames.Count > 0 ? frames[0] : null;
            }
            texture?.Draw(p, new Vector2(texture.Width / 2f, texture.Height), Color.White, Vector2.One, rotation);
        }));
    }

    private static void Refill(List<Item> items, EntityData e, Vector2 p) {
        string path = e.Bool("twoDash") ? "objects/refillTwo/" : "objects/refill/";
        MTexture? texture = null;
        bool resolved = false;
        items.Add(new Item(e.Name, -100, new Rectangle((int)p.X - 8, (int)p.Y - 8, 16, 16), () => {
            if (!resolved) {
                resolved = true;
                List<MTexture> frames = GFX.Game.GetAtlasSubtextures(path + "idle");
                texture = frames.Count > 0 ? frames[0] : null;
            }
            texture?.DrawCentered(p);
        }));
    }

    private static readonly string[] CassetteColors = { "49aaf0", "f049be", "fcdc3a", "38e04e" };

    /// <summary>节奏块：同色相连的方块拼成一整块（与游戏选贴图的规则相同），画成亮起的样子。</summary>
    private static void CassetteBlock(List<Item> items, LevelData room, EntityData e, Vector2 p, Vector2 offset) {
        if (e.Width <= 0 || e.Height <= 0) return;
        int index = e.Int("index");
        var same = new List<Rectangle>();
        foreach (EntityData other in room.Entities) {
            if (other.Name == "cassetteBlock" && other.Int("index") == index) {
                Vector2 q = other.Position + offset;
                same.Add(new Rectangle((int)q.X, (int)q.Y, other.Width, other.Height));
            }
        }
        bool Same(float x, float y) {
            var cell = new Rectangle((int)x, (int)y, 8, 8);
            foreach (Rectangle r in same) if (r.Intersects(cell)) return true;
            return false;
        }
        var cells = new List<(Vector2 At, int Tx, int Ty)>();
        for (float x = p.X; x < p.X + e.Width; x += 8f) {
            for (float y = p.Y; y < p.Y + e.Height; y += 8f) {
                bool l = Same(x - 8f, y), r = Same(x + 8f, y), u = Same(x, y - 8f), d = Same(x, y + 8f);
                (int, int)? pick = (l, r, u, d) switch {
                    (true, true, true, true) => !Same(x + 8f, y - 8f) ? (3, 0) : !Same(x - 8f, y - 8f) ? (3, 1)
                        : !Same(x + 8f, y + 8f) ? (3, 2) : !Same(x - 8f, y + 8f) ? (3, 3) : (1, 1),
                    (true, true, false, true) => (1, 0),
                    (true, true, true, false) => (1, 2),
                    (true, false, true, true) => (2, 1),
                    (false, true, true, true) => (0, 1),
                    (true, false, false, true) => (2, 0),
                    (false, true, false, true) => (0, 0),
                    (true, false, true, false) => (2, 2),
                    (false, true, true, false) => (0, 2),
                    _ => null,
                };
                if (pick is { } t) cells.Add((new Vector2(x, y), t.Item1, t.Item2));
            }
        }
        Color color = Calc.HexToColor(index >= 0 && index < CassetteColors.Length ? CassetteColors[index] : CassetteColors[0]);
        MTexture[]? pieces = null;
        items.Add(new Item(e.Name, -10, new Rectangle((int)p.X, (int)p.Y, e.Width, e.Height), () => {
            if (pieces == null) {
                MTexture sheet = GFX.Game["objects/cassetteblock/solid"];
                pieces = new MTexture[cells.Count];
                for (int i = 0; i < cells.Count; i++) pieces[i] = sheet.GetSubtexture(cells[i].Tx * 8, cells[i].Ty * 8, 8, 8);
            }
            for (int i = 0; i < pieces.Length; i++) pieces[i].Draw(cells[i].At, Vector2.Zero, color);
        }));
    }

    private static void Wire(List<Item> items, EntityData e, Vector2 p, Vector2 offset) {
        if (e.Nodes == null || e.Nodes.Length == 0) return;
        Vector2 to = e.Nodes[0] + offset;
        Color color = e.HexColor("color", Calc.HexToColor("595866"));
        var curve = new SimpleCurve(p, to, (p + to) / 2f + new Vector2(0f, 24f));
        var span = Rectangle.Union(new Rectangle((int)p.X, (int)p.Y, 1, 1), new Rectangle((int)to.X, (int)to.Y, 25, 25));
        items.Add(new Item(e.Name, e.Bool("above") ? -8500 : 2000, span, () => {
            Vector2 start = curve.Begin;
            for (int i = 1; i <= 16; i++) {
                Vector2 point = curve.GetPoint(i / 16f);
                Draw.Line(start, point, color);
                start = point;
            }
        }));
    }

    /// <summary>刺球：第 3 章（以及第 7 章 d- 开头的房间）是灰尘，其余是水晶，颜色按章节（与游戏载入时相同）。</summary>
    private static void Spinner(List<Item> items, LevelData room, EntityData e, Vector2 p, Context ctx, Random rng) {
        var bounds = new Rectangle((int)p.X - 8, (int)p.Y - 8, 16, 16);
        if (ctx.AreaId == 3 || (ctx.AreaId == 7 && room.Name.StartsWith("d-"))) {
            MTexture? body = null, center = null;
            bool resolved = false;
            items.Add(new Item(e.Name, -50, bounds, () => {
                if (!resolved) {
                    resolved = true;
                    List<MTexture> bases = GFX.Game.GetAtlasSubtextures("danger/dustcreature/base");
                    List<MTexture> centers = GFX.Game.GetAtlasSubtextures("danger/dustcreature/center");
                    body = bases.Count > 0 ? bases[0] : null;
                    center = centers.Count > 0 ? centers[0] : null;
                }
                body?.DrawCentered(p);
                center?.DrawCentered(p);
            }));
            return;
        }
        CrystalColor color = ctx.AreaId switch {
            5 => CrystalColor.Red,
            6 => CrystalColor.Purple,
            10 => CrystalColor.Rainbow,
            _ => CrystalColor.Blue,
        };
        int pick = rng.Next(1 << 20);
        MTexture? texture = null;
        bool ready = false;
        items.Add(new Item(e.Name, -8500, bounds, () => {
            if (!ready) {
                ready = true;
                List<MTexture> textures = GFX.Game.GetAtlasSubtextures(CrystalStaticSpinner.fgTextureLookup[color]);
                texture = textures.Count > 0 ? textures[pick % textures.Count] : null;
            }
            if (texture == null) return;
            foreach (Vector2 d in Around) texture.DrawCentered(p + d, Color.Black);
            texture.DrawCentered(p);
        }));
    }

    private static readonly Vector2[] Around = { new(0, -1), new(0, 1), new(-1, 0), new(1, 0) };

    private static void Dream(List<Item> items, EntityData e, Vector2 p) {
        if (e.Width <= 0 || e.Height <= 0) return;
        var rect = new Rectangle((int)p.X, (int)p.Y, e.Width, e.Height);
        items.Add(new Item(e.Name, -11000, rect, () => {
            Draw.Rect(rect, Color.Black);
            Draw.HollowRect(rect, Color.White);
        }));
    }

    /// <summary>其余有大小的方块、机关与致死物：画一个轮廓（致死物红色，其他浅灰），看得出那里有东西。</summary>
    private static void Outline(List<Item> items, EntityData e, Vector2 p) {
        if (e.Width <= 0 || e.Height <= 0) return;
        string name = e.Name;
        bool hazard = name.Contains("spike", StringComparison.OrdinalIgnoreCase) || name.Contains("lightning", StringComparison.OrdinalIgnoreCase)
            || name.Contains("spinner", StringComparison.OrdinalIgnoreCase) || name.Contains("lava", StringComparison.OrdinalIgnoreCase);
        bool solid = hazard || name.Contains("block", StringComparison.OrdinalIgnoreCase) || name.Contains("wall", StringComparison.OrdinalIgnoreCase)
            || name.Contains("gate", StringComparison.OrdinalIgnoreCase) || name.Contains("platform", StringComparison.OrdinalIgnoreCase)
            || name.Contains("switch", StringComparison.OrdinalIgnoreCase);
        if (!solid) return;
        var rect = new Rectangle((int)p.X, (int)p.Y, e.Width, e.Height);
        Color color = hazard ? new Color(0xE0, 0x47, 0x5B) * 0.85f : new Color(0xC8, 0xCC, 0xD6) * 0.7f;
        items.Add(new Item(name + ":outline", -1, rect, () => Draw.HollowRect(rect, color)));
    }

    private static void AddDecal(List<Item> items, DecalData d, Vector2 offset, int depth) {
        string texture = d.Texture ?? "";
        string extension = System.IO.Path.GetExtension(texture);
        if (extension.Length > 0) texture = texture.Replace(extension, "");
        // 与游戏的 Decal 相同：去掉扩展名、加 decals/ 前缀，去掉末尾的帧号，取这组贴图的第一帧
        string path = System.Text.RegularExpressions.Regex.Replace(("decals/" + texture).Replace('\\', '/'), "\\d+$", string.Empty);
        Vector2 p = offset + d.Position;
        Vector2 scale = d.Scale;
        float rotation = MathHelper.ToRadians(d.Rotation);
        string? hex = d.ColorHex;
        bool resolved = false;
        MTexture? frame = null;
        Color color = Color.White;
        items.Add(new Item(path, depth, new Rectangle((int)p.X, (int)p.Y, 1, 1), () => {
            if (!resolved) {
                resolved = true;
                List<MTexture> textures = GFX.Game.GetAtlasSubtextures(path);
                frame = textures.Count > 0 ? textures[0] : null;
                color = string.IsNullOrEmpty(hex) ? Color.White : Calc.HexToColorWithAlpha(hex);
            }
            frame?.DrawCentered(p, color, scale, rotation);
        }));
    }

    private static int StableHash(string text) {
        unchecked {
            int h = (int)2166136261;
            foreach (char c in text) h = (h ^ c) * 16777619;
            return h;
        }
    }
}
