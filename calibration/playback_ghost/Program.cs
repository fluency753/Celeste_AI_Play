using System.Collections;
using System.Reflection;
using Celeste;
using CelesteAI.Bridge;
using Microsoft.Xna.Framework;
using Monocle;

// 跨房间规划回放的补画清单：按地图数据列出别的房间的实体与贴花，不取贴图、不消耗游戏的随机数。
// dotnet run --project calibration/playback_ghost "-p:CelestePrefix=<游戏目录>"
void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
BinaryPacker.Element El(string name, Dictionary<string, object> attrs, params BinaryPacker.Element[] children) =>
    new() { Name = name, Attributes = attrs, Children = children.ToList() };

var entities = El("entities", new(),
    El("spikesUp", new() { ["id"] = 1, ["x"] = 16, ["y"] = 160, ["width"] = 24, ["type"] = "default" }),
    El("strawberry", new() { ["id"] = 2, ["x"] = 100, ["y"] = 50 }),
    El("strawberry", new() { ["id"] = 3, ["x"] = 120, ["y"] = 50 }),
    El("zipMover", new() { ["id"] = 4, ["x"] = 40, ["y"] = 80, ["width"] = 24, ["height"] = 16 }, El("node", new() { ["x"] = 40, ["y"] = 20 })),
    El("fallingBlock", new() { ["id"] = 5, ["x"] = 200, ["y"] = 96, ["width"] = 16, ["height"] = 24, ["tiletype"] = "3" }),
    El("dashBlock", new() { ["id"] = 6, ["x"] = 232, ["y"] = 96, ["width"] = 16, ["height"] = 16, ["blendin"] = true }),
    El("jumpThru", new() { ["id"] = 7, ["x"] = 0, ["y"] = 120, ["width"] = 32 }),
    El("cassetteBlock", new() { ["id"] = 8, ["x"] = 256, ["y"] = 40, ["width"] = 24, ["height"] = 24, ["index"] = 1 }),
    El("refill", new() { ["id"] = 9, ["x"] = 160, ["y"] = 100 }),
    El("wire", new() { ["id"] = 10, ["x"] = 0, ["y"] = 10, ["above"] = false }, El("node", new() { ["x"] = 80, ["y"] = 10 })),
    El("swapBlock", new() { ["id"] = 11, ["x"] = 280, ["y"] = 120, ["width"] = 16, ["height"] = 16 }),
    El("npc", new() { ["id"] = 12, ["x"] = 10, ["y"] = 10 }),
    El("crumbleBlock", new() { ["id"] = 13, ["x"] = 64, ["y"] = 144, ["width"] = 16 }));
var fg = El("fgdecals", new(), El("decal", new() { ["x"] = 50f, ["y"] = 60f, ["scaleX"] = 1f, ["scaleY"] = 1f, ["texture"] = "1-forsakencity/sign00.png" }));
var bg = El("bgdecals", new(), El("decal", new() { ["x"] = 70f, ["y"] = 60f, ["scaleX"] = -1f, ["scaleY"] = 1f, ["texture"] = "generic\\grass_a.png", ["depth"] = 123 }));
var room = new LevelData(El("level", new() { ["name"] = "lvl_b", ["x"] = 320, ["y"] = -184, ["width"] = 320, ["height"] = 184 }, entities, fg, bg));

var ctx = new PlaybackRoomGhost.Context(1, "default", "wood", "default", false, id => id.Level == "b" && id.ID == 2,
    rect => rect.Intersects(new Rectangle(312, -64, 8, 8)), null, Rectangle.Empty);
Calc.Random = new Random(42);
var plan = PlaybackRoomGhost.Plan(room, ctx);
Check(Calc.Random.Next() == new Random(42).Next(), "planning consumed the game's random numbers");
string Describe(List<PlaybackRoomGhost.Item> items) => string.Join("; ", items.Select(i => $"{i.Name}@{i.Depth}:{i.Bounds}"));
Check(Describe(plan) == Describe(PlaybackRoomGhost.Plan(room, ctx)), "planning the same room twice differs");

var byName = plan.GroupBy(i => i.Name).ToDictionary(g => g.Key, g => g.ToList());
void Expect(string name, double depth, int count = 1) {
    Check(byName.TryGetValue(name, out var found) && found.Count == count && found.All(i => i.Depth == depth),
          $"{name}: expected {count} at depth {depth}, got {(found == null ? "none" : string.Join(",", found.Select(i => i.Depth)))}");
}
Expect("spikesUp", -1);
Expect("strawberry", -100);            // id 2 已经拿走（DoNotLoad），只剩 id 3
Expect("zipMover:path", 5000);
Expect("zipMover", -9999);
Expect("fallingBlock", 0);
Expect("dashBlock", -10501);           // blendin：与周围地形拼在一起，深度与游戏相同
Expect("jumpThru", -60);
Expect("cassetteBlock", -10);
Expect("refill", -100);
Expect("wire", 2000);
Expect("swapBlock:outline", -1);
Expect("crumbleBlock", 0);
Expect("decals/1-forsakencity/sign", -10500);
Expect("decals/generic/grass_a", 123);
Check(!byName.ContainsKey("npc") && plan.Count == 14, $"unexpected items: {Describe(plan)}");
// 世界坐标 = 房间左上角 + 实体在房间里的位置
Check(byName["spikesUp"][0].Bounds == new Rectangle(336, -27, 24, 3), $"spike bounds {byName["spikesUp"][0].Bounds}");
Check(byName["strawberry"][0].Bounds == new Rectangle(432, -142, 16, 16), $"berry bounds {byName["strawberry"][0].Bounds}");
Check(byName["zipMover"][0].Bounds == new Rectangle(360, -104, 24, 16), $"zip bounds {byName["zipMover"][0].Bounds}");
Check(byName["zipMover:path"][0].Bounds.Contains(372, -156) && byName["zipMover:path"][0].Bounds.Contains(372, -96), "zip path does not span both ends");

// 每次渲染把每一项按深度排进绘制通道；清空后不再排
var items = (IList)typeof(PlaybackRoomGhost).GetField("Items", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
foreach (var item in plan) items.Add(item);
PlaybackRoomGhost.Collect();
Check(PlaybackVisualPass.Depths.Count == plan.Count && PlaybackVisualPass.Depths.SequenceEqual(plan.Select(i => i.Depth)), "collect did not queue every item");
PlaybackRoomGhost.Clear();
PlaybackVisualPass.Depths.Clear();
PlaybackRoomGhost.Collect();
Check(PlaybackRoomGhost.Count == 0 && PlaybackVisualPass.Depths.Count == 0, "clear kept items");
// 没有地图数据（例如还没进关卡）时不准备补画，也不出错
var bare = (Level)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Level));
PlaybackRoomGhost.Prepare(bare, new Rectangle(320, -184, 320, 184));
Check(PlaybackRoomGhost.Count == 0 && PlaybackRoomGhost.Room == null, "prepared without map data");
Console.WriteLine($"PASS: room ghost plan lists {plan.Count} items with game depths and world positions, skips collected entities, leaves Calc.Random untouched, queues by depth and clears");
