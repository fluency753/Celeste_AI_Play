using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>观测保留潜在碰撞体；当前禁用与不存在是两种状态。</summary>
internal static class EntityGeometry {
    public static bool Include(Entity entity) =>
        entity is not (Player or Trigger or SolidTiles) && entity.Collider != null;

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
        writer.WriteEndObject();
    }
}
