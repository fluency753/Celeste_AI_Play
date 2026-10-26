using System;
using System.Collections.Generic;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>观测保留潜在碰撞体；当前禁用与不存在是两种状态。</summary>
internal static class EntityGeometry {
    internal readonly record struct Rect(float X, float Y, float W, float H);

    public static bool Include(Entity entity) =>
        entity is not (Player or Trigger or SolidTiles) && entity.Collider != null;

    public static bool IsExact(Collider collider) => collider switch {
        Hitbox or Circle or Grid => true,
        ColliderList list => Array.TrueForAll(list.colliders, IsExact),
        _ => false,
    };

    /// <summary>Read occupied cells directly; visual overlays never affect collision terrain.</summary>
    public static List<Rect> GridRects(Grid grid, Rectangle? scope = null) {
        float ox = grid.AbsoluteLeft, oy = grid.AbsoluteTop;
        float cw = grid.CellWidth, ch = grid.CellHeight;
        int x0 = scope is {} s0 ? Math.Max(0, (int) MathF.Floor((s0.Left - ox) / cw)) : 0;
        int y0 = scope is {} s1 ? Math.Max(0, (int) MathF.Floor((s1.Top - oy) / ch)) : 0;
        int x1 = scope is {} s2 ? Math.Min(grid.CellsX, (int) MathF.Ceiling((s2.Right - ox) / cw)) : grid.CellsX;
        int y1 = scope is {} s3 ? Math.Min(grid.CellsY, (int) MathF.Ceiling((s3.Bottom - oy) / ch)) : grid.CellsY;
        var result = new List<Rect>();
        var active = new Dictionary<(int, int), Rect>();
        for (int y = y0; y <= y1; y++) {
            var runs = new HashSet<(int, int)>();
            if (y < y1) {
                for (int x = x0; x < x1;) {
                    if (!grid[x, y]) { x++; continue; }
                    int start = x++;
                    while (x < x1 && grid[x, y]) x++;
                    runs.Add((start, x));
                }
            }
            foreach (var key in new List<(int, int)>(active.Keys))
                if (!runs.Contains(key)) { result.Add(active[key]); active.Remove(key); }
            foreach (var key in runs) {
                if (active.TryGetValue(key, out Rect previous)) active[key] = previous with { H = previous.H + ch };
                else active[key] = new Rect(ox + key.Item1 * cw, oy + y * ch, (key.Item2 - key.Item1) * cw, ch);
            }
        }
        result.Sort((a, b) => a.Y != b.Y ? a.Y.CompareTo(b.Y) : a.X.CompareTo(b.X));
        return result;
    }

    // 所有坐标均为世界坐标，子碰撞体不能再叠加父位置。
    public static void Write(Utf8JsonWriter writer, Collider collider) {
        writer.WriteStartObject();
        writer.WriteString("shape", collider switch {
            Circle => "circle", Hitbox => "rect", ColliderList => "list", Grid => "grid", _ => "unknown"
        });
        writer.WriteNumber("x", collider.AbsoluteLeft);
        writer.WriteNumber("y", collider.AbsoluteTop);
        writer.WriteNumber("w", collider.Width);
        writer.WriteNumber("h", collider.Height);
        if (collider is Circle circle) {
            writer.WriteNumber("r", circle.Radius);
        }
        if (collider is ColliderList list) {
            writer.WriteStartArray("parts");
            foreach (Collider part in list.colliders) Write(writer, part);
            writer.WriteEndArray();
        }
        if (collider is Grid grid) {
            writer.WriteNumber("cellWidth", grid.CellWidth);
            writer.WriteNumber("cellHeight", grid.CellHeight);
            writer.WriteStartArray("parts");
            foreach (Rect rect in GridRects(grid)) WriteRect(writer, rect);
            writer.WriteEndArray();
        }
        writer.WriteEndObject();
    }

    public static void WriteRect(Utf8JsonWriter writer, Rect rect) {
        writer.WriteStartObject();
        writer.WriteString("shape", "rect");
        writer.WriteNumber("x", rect.X); writer.WriteNumber("y", rect.Y);
        writer.WriteNumber("w", rect.W); writer.WriteNumber("h", rect.H);
        writer.WriteEndObject();
    }
}
