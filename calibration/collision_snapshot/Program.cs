using System.Runtime.CompilerServices;
using System.Text.Json;
using Celeste;
using CelesteAI.Bridge;
using Microsoft.Xna.Framework;
using Monocle;

void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
T Make<T>(Vector2 position, Collider collider) where T : Entity {
    var entity = (T)RuntimeHelpers.GetUninitializedObject(typeof(T));
    entity.Position = position; entity.Collidable = true; entity.Visible = true; entity.Collider = collider;
    return entity;
}
JsonDocument Snapshot(long frame, IEnumerable<Entity> entities, SolidTiles? terrain = null,
                      bool includeTerrain = true, bool includeEntities = true, bool fullRoom = true,
                      Rectangle? bounds = null) {
    using var bytes = new MemoryStream();
    using (var writer = new Utf8JsonWriter(bytes)) {
        writer.WriteStartObject();
        CollisionSnapshot.Write(writer, frame, "Celeste/3-CelestialResort", "s3", bounds ?? new Rectangle(0, 0, 160, 160),
            fullRoom, terrain, entities, null, includeTerrain, includeEntities);
        writer.WriteEndObject();
    }
    return JsonDocument.Parse(bytes.ToArray());
}
JsonElement[] Objects(JsonDocument document) => document.RootElement.GetProperty("collision").GetProperty("objects").EnumerateArray().ToArray();

var grid = new Grid(32, 32, 8, 8);
grid[0, 0] = true; grid[1, 0] = true; grid[0, 1] = true; grid[1, 1] = true;
grid[3, 3] = true;
var terrain = Make<SolidTiles>(Vector2.Zero, grid);
var brick = Make<Solid>(new Vector2(16, 24), new Hitbox(8, 8));
var key = Make<Key>(new Vector2(20, 20), new Hitbox(12, 12));
var spinner = Make<CrystalStaticSpinner>(new Vector2(44, 48), new ColliderList(new Circle(6), new Hitbox(16, 4, -8, -3)));
spinner.Collidable = false; spinner.Visible = false;
var platform = Make<JumpThru>(new Vector2(64, 80), new Hitbox(24, 4));
using (var snapshot = Snapshot(1, new Entity[] { terrain, brick, key, spinner, platform }, terrain)) {
    var collision = snapshot.RootElement.GetProperty("collision"); var objects = Objects(snapshot);
    Check(collision.GetProperty("frame").GetInt64() == 1 && collision.GetProperty("complete").GetBoolean(), "Missing frame or coverage");
    var tiles = objects.Where(o => o.GetProperty("source").GetString() == "terrain").ToArray();
    Check(tiles.Length == 2, "Terrain grid was not merged without duplication");
    Check(tiles[0].GetProperty("geometry").GetProperty("w").GetSingle() == 16 &&
          tiles[0].GetProperty("geometry").GetProperty("h").GetSingle() == 16, "Merged terrain rectangle changed occupancy");
    Check(objects.Count(o => o.GetProperty("source").GetString() == "entity") == 4, "SolidTiles appeared again as entity");
    Check(objects.Single(o => o.GetProperty("type").GetString() == "Key").GetProperty("kind").GetString() == "sensor", "Key trigger became a wall");
    var obstacle = objects.Single(o => o.GetProperty("type").GetString() == "Solid");
    Check(obstacle.GetProperty("kind").GetString() == "solid" && obstacle.GetProperty("mutable").GetBoolean(), "Stationary entity was frozen into static terrain");
    var hazard = objects.Single(o => o.GetProperty("type").GetString() == "CrystalStaticSpinner");
    Check(!hazard.GetProperty("collidable").GetBoolean(), "Disabled hazard vanished or became enabled");
    var parts = hazard.GetProperty("geometry").GetProperty("parts");
    Check(parts.GetArrayLength() == 2 && parts[0].GetProperty("shape").GetString() == "circle", "Combination lost circle");
    Check(parts[0].GetProperty("x").GetSingle() == 38 && parts[0].GetProperty("y").GetSingle() == 42, "Collider list child double-offset");
    Check(objects.Single(o => o.GetProperty("type").GetString() == "JumpThru").GetProperty("kind").GetString() == "one_way", "One-way platform became two-way wall");
}
Console.WriteLine("PASS: native terrain/solid/key/one-way and dormant composite circle geometry share one current snapshot");

int id = EntityState.IdOf(brick);
brick.Position = new Vector2(40, 16); brick.Collidable = false;
using (var moved = Snapshot(2, new Entity[] { brick })) {
    var item = Objects(moved).Single();
    Check(item.GetProperty("id").GetInt32() == id && item.GetProperty("geometry").GetProperty("x").GetSingle() == 40,
          "Entity identity or moved position became stale");
    Check(!item.GetProperty("collidable").GetBoolean(), "Collision disable not updated");
}
using (var absent = Snapshot(3, Array.Empty<Entity>())) Check(Objects(absent).Length == 0, "Removed entity retained");
using (var partial = Snapshot(4, new Entity[] { brick }, terrain, includeTerrain: false, fullRoom: false, bounds: new Rectangle(0, 0, 32, 32))) {
    var c = partial.RootElement.GetProperty("collision");
    Check(c.GetProperty("scope").GetString() == "view" && !c.GetProperty("complete").GetBoolean(), "Partial observation claimed complete room");
    Check(!c.GetProperty("coverage").GetProperty("terrain").GetBoolean() && Objects(partial).Length == 0, "Missing source or off-view entity not respected");
}
Console.WriteLine("PASS: same entity updates position/state each frame; removal and partial coverage never inherit old geometry");

var entityGrid = new Grid(16, 16, 8, 8); entityGrid[0, 0] = true; entityGrid[1, 1] = true;
var gridded = Make<Solid>(new Vector2(100, 100), entityGrid);
using (var snapshot = Snapshot(5, new[] { gridded })) {
    var geometry = Objects(snapshot).Single().GetProperty("geometry");
    Check(geometry.GetProperty("shape").GetString() == "grid" && geometry.GetProperty("parts").GetArrayLength() == 2,
          "Sparse grid was replaced by solid AABB");
}
Console.WriteLine("PASS: entity grid retains occupied-cell geometry rather than its bounding rectangle");
