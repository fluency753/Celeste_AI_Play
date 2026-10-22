using System;
using System.Collections.Generic;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

internal sealed class SnapshotOptions {
    public bool Entities = true;
    public bool Tiles = true;
    /// <summary>视野外额外包含的像素边距。</summary>
    public int Margin = 16;
}

/// <summary>
/// 从当前帧读出 Madeline 状态、关卡信息、视野内实体与地形。
/// 只报玩家能看见或能碰到的东西：不报触发器，也不报屏幕外的内容。
/// </summary>
internal static class StateExtractor {
    public const int ViewWidth = 320;
    public const int ViewHeight = 180;

    private static readonly string[] StateNames = {
        "Normal", "Climb", "Dash", "Swim", "Boost", "RedDash", "HitSquash", "Launch", "Pickup",
        "DreamDash", "SummitLaunch", "Dummy", "IntroWalk", "IntroJump", "IntroRespawn", "IntroWakeUp",
        "BirdDashTutorial", "Frozen", "ReflectionFall", "StarFly", "TempleFall", "CassetteFly", "Attract",
        "IntroMoonJump", "FlingBird", "IntroThinkForABit",
    };

    public static string StateName(int state) =>
        state >= 0 && state < StateNames.Length ? StateNames[state] : $"Custom{state}";

    public static Player? GetPlayer(Level level) => level.Tracker.GetEntity<Player>();

    /// <summary>Madeline 是否处于玩家可操作的状态（不在剧情、转场、死亡或入场动画中）。</summary>
    public static bool IsControllable(Level? level, Player? player) {
        if (level == null || player == null || player.Dead) return false;
        if (level.InCutscene || level.Paused || level.Transitioning || level.Frozen) return false;
        if (!player.InControl) return false;
        return player.StateMachine.State is not (Player.StDummy or Player.StIntroWalk or Player.StIntroJump
            or Player.StIntroRespawn or Player.StIntroWakeUp or Player.StIntroMoonJump or Player.StIntroThinkForABit);
    }

    public static Rectangle ViewRect(Level level, int margin) {
        Vector2 cam = level.Camera.Position;
        return new Rectangle((int) MathF.Floor(cam.X) - margin, (int) MathF.Floor(cam.Y) - margin,
            ViewWidth + margin * 2, ViewHeight + margin * 2);
    }

    public static void WriteSnapshot(Utf8JsonWriter w, SnapshotOptions opt) {
        w.WriteNumber("frame", Bridge.Frame);
        w.WriteString("scene", Engine.Scene?.GetType().Name ?? "none");

        if (SaveData.Instance is { } save) {
            w.WriteStartObject("save");
            w.WriteString("name", save.Name);
            w.WriteNumber("slot", save.FileSlot);
            w.WriteNumber("fileTimeTicks", save.Time);
            w.WriteEndObject();
        } else {
            w.WriteNull("save");
        }

        if (Engine.Scene is not Level level) {
            w.WriteNull("level");
            w.WriteNull("player");
            w.WriteBoolean("controllable", false);
            return;
        }

        Player? player = GetPlayer(level);
        WriteLevel(w, level);
        WritePlayer(w, level, player);
        w.WriteBoolean("controllable", IsControllable(level, player));

        if (opt.Entities || opt.Tiles) {
            List<EntityInfo> entities = CollectEntities(level, ViewRect(level, opt.Margin));
            if (opt.Entities) {
                WriteEntities(w, entities);
            }
            if (opt.Tiles) {
                WriteTiles(w, level, player, entities, opt.Margin);
            }
        }
    }

    private static void WriteLevel(Utf8JsonWriter w, Level level) {
        Session s = level.Session;
        w.WriteStartObject("level");
        w.WriteString("sid", s.Area.SID);
        w.WriteNumber("chapter", s.Area.ChapterIndex);
        w.WriteString("side", s.Area.Mode switch { AreaMode.Normal => "A", AreaMode.BSide => "B", _ => "C" });
        w.WriteString("room", s.Level);
        Rectangle b = level.Bounds;
        WriteRect(w, "bounds", b.X, b.Y, b.Width, b.Height);
        w.WriteStartArray("camera");
        w.WriteNumberValue(Round(level.Camera.X));
        w.WriteNumberValue(Round(level.Camera.Y));
        w.WriteEndArray();
        w.WriteBoolean("inCutscene", level.InCutscene);
        w.WriteBoolean("skippingCutscene", level.SkippingCutscene);
        w.WriteBoolean("paused", level.Paused);
        w.WriteBoolean("transitioning", level.Transitioning);
        w.WriteBoolean("frozen", level.Frozen);
        w.WriteBoolean("completed", level.Completed);
        w.WriteNumber("chapterTimeTicks", s.Time);
        w.WriteNumber("deaths", s.Deaths);
        w.WriteNumber("deathsInRoom", s.DeathsInCurrentLevel);
        w.WriteEndObject();
    }

    private static void WritePlayer(Utf8JsonWriter w, Level level, Player? p) {
        if (p == null) {
            w.WriteNull("player");
            return;
        }

        w.WriteStartObject("player");
        Vector2 exact = p.Position + p.PositionRemainder;
        w.WriteStartArray("pos");
        w.WriteNumberValue(Round(exact.X, 4));
        w.WriteNumberValue(Round(exact.Y, 4));
        w.WriteEndArray();
        w.WriteStartArray("speed");
        w.WriteNumberValue(Round(p.Speed.X, 3));
        w.WriteNumberValue(Round(p.Speed.Y, 3));
        w.WriteEndArray();
        int st = p.StateMachine.State;
        w.WriteString("state", StateName(st));
        w.WriteNumber("stateId", st);
        w.WriteNumber("dashes", p.Dashes);
        w.WriteNumber("maxDashes", p.MaxDashes);
        w.WriteNumber("stamina", Round(p.Stamina, 2));
        w.WriteBoolean("onGround", p.onGround);
        w.WriteString("facing", p.Facing == Facings.Right ? "R" : "L");
        w.WriteBoolean("ducking", p.Ducking);
        w.WriteBoolean("canDash", p.CanDash);
        w.WriteBoolean("dead", p.Dead);
        w.WriteBoolean("inControl", p.InControl);
        w.WriteString("holding", p.Holding?.Entity?.GetType().Name);
        w.WriteBoolean("wallLeft", p.CollideCheck<Solid>(p.Position - Vector2.UnitX));
        w.WriteBoolean("wallRight", p.CollideCheck<Solid>(p.Position + Vector2.UnitX));
        Collider c = p.Collider;
        WriteRect(w, "hitbox", Round(c.AbsoluteLeft), Round(c.AbsoluteTop), Round(c.Width), Round(c.Height));
        w.WriteStartObject("timers");
        w.WriteNumber("jumpGrace", Frames(p.jumpGraceTimer));
        w.WriteNumber("varJump", Frames(p.varJumpTimer));
        w.WriteNumber("dashCooldown", Frames(p.dashCooldownTimer));
        w.WriteEndObject();
        w.WriteEndObject();
    }

    internal readonly record struct EntityInfo(Entity Entity, string Type, string Category, float X, float Y, float W, float H, string Shape, float Radius);

    private static List<EntityInfo> CollectEntities(Level level, Rectangle view) {
        var list = new List<EntityInfo>();
        foreach (Entity e in level.Entities) {
            if (e is Player or Trigger or SolidTiles or Killbox || !e.Collidable || e.Collider == null) {
                continue;
            }
            Collider c = e.Collider;
            var rect = new Rectangle((int) MathF.Floor(c.AbsoluteLeft), (int) MathF.Floor(c.AbsoluteTop),
                (int) MathF.Ceiling(c.Width), (int) MathF.Ceiling(c.Height));
            if (!rect.Intersects(view)) {
                continue;
            }
            string shape = c switch { Circle => "circle", ColliderList => "list", Grid => "grid", _ => "rect" };
            float radius = c is Circle circle ? circle.Radius : 0f;
            list.Add(new EntityInfo(e, e.GetType().Name, EntityCatalog.Category(e),
                c.AbsoluteLeft, c.AbsoluteTop, c.Width, c.Height, shape, radius));
        }
        return list;
    }

    private static void WriteEntities(Utf8JsonWriter w, List<EntityInfo> entities) {
        w.WriteStartArray("entities");
        foreach (EntityInfo info in entities) {
            w.WriteStartObject();
            w.WriteString("type", info.Type);
            w.WriteString("cat", info.Category);
            w.WriteNumber("x", Round(info.X));
            w.WriteNumber("y", Round(info.Y));
            w.WriteNumber("w", Round(info.W));
            w.WriteNumber("h", Round(info.H));
            if (info.Shape != "rect") {
                w.WriteString("shape", info.Shape);
            }
            if (info.Radius > 0) {
                w.WriteNumber("r", Round(info.Radius));
            }
            if (!info.Entity.Visible) {
                w.WriteBoolean("visible", false);
            }
            EntityCatalog.WriteProps(w, info.Entity);
            w.WriteEndObject();
        }
        w.WriteEndArray();
    }

    /// <summary>视野内的字符地图，8 像素一格；地形之外叠加主要实体与 Madeline。</summary>
    private static void WriteTiles(Utf8JsonWriter w, Level level, Player? player, List<EntityInfo> entities, int margin) {
        Rectangle view = ViewRect(level, margin);
        const int cell = 8;
        int originX = (int) MathF.Floor(view.Left / (float) cell) * cell;
        int originY = (int) MathF.Floor(view.Top / (float) cell) * cell;
        int cols = (int) MathF.Ceiling((view.Right - originX) / (float) cell);
        int rows = (int) MathF.Ceiling((view.Bottom - originY) / (float) cell);
        var map = new char[rows, cols];

        Grid? grid = level.SolidTiles?.Collider as Grid;
        for (int r = 0; r < rows; r++) {
            for (int col = 0; col < cols; col++) {
                map[r, col] = '.';
                if (grid == null) continue;
                int gx = (int) MathF.Floor((originX + col * cell + 4 - grid.AbsoluteLeft) / grid.CellWidth);
                int gy = (int) MathF.Floor((originY + r * cell + 4 - grid.AbsoluteTop) / grid.CellHeight);
                if (gx >= 0 && gy >= 0 && gx < grid.CellsX && gy < grid.CellsY && grid[gx, gy]) {
                    map[r, col] = '#';
                }
            }
        }

        foreach (EntityInfo info in entities) {
            char ch = EntityCatalog.MapChar(info.Entity, info.Category);
            if (ch == '\0') continue;
            Paint(map, originX, originY, cell, rows, cols, info.X, info.Y, info.W, info.H, ch, overwriteTiles: false);
        }
        if (player is { Collider: { } pc }) {
            Paint(map, originX, originY, cell, rows, cols, pc.AbsoluteLeft, pc.AbsoluteTop, pc.Width, pc.Height, 'M', overwriteTiles: true);
        }

        w.WriteStartObject("tiles");
        w.WriteStartArray("origin");
        w.WriteNumberValue(originX);
        w.WriteNumberValue(originY);
        w.WriteEndArray();
        w.WriteNumber("cell", cell);
        w.WriteStartArray("rows");
        var line = new char[cols];
        for (int r = 0; r < rows; r++) {
            for (int col = 0; col < cols; col++) line[col] = map[r, col];
            w.WriteStringValue(new string(line));
        }
        w.WriteEndArray();
        w.WriteStartObject("legend");
        foreach (var (ch, meaning) in EntityCatalog.Legend) {
            w.WriteString(ch.ToString(), meaning);
        }
        w.WriteEndObject();
        w.WriteEndObject();
    }

    private static void Paint(char[,] map, int ox, int oy, int cell, int rows, int cols,
        float x, float y, float width, float height, char ch, bool overwriteTiles) {
        int c0 = (int) MathF.Floor((x - ox) / cell);
        int r0 = (int) MathF.Floor((y - oy) / cell);
        int c1 = (int) MathF.Ceiling((x + Math.Max(width, 1f) - ox) / cell) - 1;
        int r1 = (int) MathF.Ceiling((y + Math.Max(height, 1f) - oy) / cell) - 1;
        for (int r = Math.Max(r0, 0); r <= Math.Min(r1, rows - 1); r++) {
            for (int c = Math.Max(c0, 0); c <= Math.Min(c1, cols - 1); c++) {
                if (overwriteTiles || map[r, c] == '.') {
                    map[r, c] = ch;
                }
            }
        }
    }

    /// <summary>逐帧轨迹里的一行：帧号、坐标、速度、状态、冲刺数、是否着地。</summary>
    public static void WriteTrajectoryRow(Utf8JsonWriter w, long frame, Player? p) {
        w.WriteStartArray();
        w.WriteNumberValue(frame);
        if (p == null) {
            w.WriteNullValue();
        } else {
            Vector2 exact = p.Position + p.PositionRemainder;
            w.WriteNumberValue(Round(exact.X, 2));
            w.WriteNumberValue(Round(exact.Y, 2));
            w.WriteNumberValue(Round(p.Speed.X, 1));
            w.WriteNumberValue(Round(p.Speed.Y, 1));
            w.WriteStringValue(StateName(p.StateMachine.State));
            w.WriteNumberValue(p.Dashes);
            w.WriteNumberValue(p.onGround ? 1 : 0);
        }
        w.WriteEndArray();
    }

    private static void WriteRect(Utf8JsonWriter w, string name, double x, double y, double width, double height) {
        w.WriteStartArray(name);
        w.WriteNumberValue(x);
        w.WriteNumberValue(y);
        w.WriteNumberValue(width);
        w.WriteNumberValue(height);
        w.WriteEndArray();
    }

    private static int Frames(float seconds) => seconds <= 0f ? 0 : (int) MathF.Ceiling(seconds * 60f - 0.0001f);

    private static double Round(float v, int digits = 2) => Math.Round(v, digits);
}
