using System;
using System.Collections.Generic;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>A replacement snapshot of current collision geometry; never an entity-position cache.</summary>
internal static class CollisionSnapshot {
    public static string Kind(Entity entity) {
        if (entity is JumpThru) return "one_way";
        if (entity is Solid) return "solid";
        return EntityCatalog.Category(entity) switch {
            "hazard" => "hazard",
            "collectible" or "spring" or "refill" or "booster" or "feather" or "interactive" => "sensor",
            _ => "unknown",
        };
    }

    public static void Write(Utf8JsonWriter writer, long frame, string sid, string room, Rectangle scope,
                             bool fullRoom, SolidTiles? terrain, IEnumerable<Entity> entities, Player? player,
                             bool includeTerrain, bool includeEntities) {
        writer.WriteStartObject("collision");
        writer.WriteNumber("version", 1); writer.WriteNumber("frame", frame);
        writer.WriteString("sid", sid); writer.WriteString("room", room);
        writer.WriteString("scope", fullRoom ? "room" : "view");
        writer.WritePropertyName("bounds");
        WriteBounds(writer, scope.X, scope.Y, scope.Width, scope.Height);
        writer.WriteStartObject("coverage");
        writer.WriteBoolean("terrain", includeTerrain);
        writer.WriteBoolean("entities", includeEntities);
        writer.WriteEndObject();
        writer.WriteBoolean("complete", includeTerrain && includeEntities);
        bool exact = true;
        writer.WriteStartArray("objects");
        if (includeTerrain && terrain?.Collider is Grid grid) {
            int index = 0;
            foreach (EntityGeometry.Rect rect in EntityGeometry.GridRects(grid, scope)) {
                writer.WriteStartObject();
                writer.WriteString("id", "terrain:" + index++);
                writer.WriteString("source", "terrain"); writer.WriteString("type", "SolidTiles");
                writer.WriteString("kind", "solid"); writer.WriteBoolean("collidable", terrain.Collidable);
                writer.WriteBoolean("mutable", false);
                writer.WritePropertyName("geometry"); EntityGeometry.WriteRect(writer, rect);
                writer.WriteEndObject();
            }
        } else if (includeTerrain && terrain?.Collider is {} otherTerrain) {
            exact &= EntityGeometry.IsExact(otherTerrain);
            WriteEntity(writer, terrain, "terrain", mutable: false);
        }
        if (includeEntities) {
            foreach (Entity entity in entities) {
                if (!EntityGeometry.Include(entity)) continue;
                Collider collider = entity.Collider;
                if (collider.AbsoluteRight <= scope.Left || collider.AbsoluteLeft >= scope.Right ||
                    collider.AbsoluteBottom <= scope.Top || collider.AbsoluteTop >= scope.Bottom) continue;
                exact &= EntityGeometry.IsExact(collider);
                WriteEntity(writer, entity, "entity", mutable: true);
            }
        }
        writer.WriteEndArray();
        writer.WriteBoolean("geometryComplete", exact);
        writer.WriteStartObject("player");
        if (player?.Collider is {} body) {
            writer.WritePropertyName("hitbox");
            WriteBounds(writer, body.AbsoluteLeft, body.AbsoluteTop, body.Width, body.Height);
        } else writer.WriteNull("hitbox");
        writer.WriteEndObject();
        writer.WriteEndObject();
    }

    private static void WriteEntity(Utf8JsonWriter writer, Entity entity, string source, bool mutable) {
        writer.WriteStartObject();
        writer.WriteNumber("id", EntityState.IdOf(entity));
        writer.WriteString("source", source); writer.WriteString("type", entity.GetType().Name);
        writer.WriteString("category", EntityCatalog.Category(entity));
        writer.WriteString("kind", Kind(entity));
        writer.WriteBoolean("collidable", entity.Collidable);
        writer.WriteBoolean("mutable", mutable);
        writer.WritePropertyName("geometry"); EntityGeometry.Write(writer, entity.Collider);
        if (entity is JumpThru) writer.WriteString("direction", "up");
        EntityCatalog.WriteProps(writer, entity);
        writer.WriteEndObject();
    }

    private static void WriteBounds(Utf8JsonWriter writer, float x, float y, float width, float height) {
        writer.WriteStartArray(); writer.WriteNumberValue(x); writer.WriteNumberValue(y);
        writer.WriteNumberValue(width); writer.WriteNumberValue(height); writer.WriteEndArray();
    }
}
