using System.Collections.Generic;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>把实体类型归成智能体易懂的几类，并给出字符地图里的符号。</summary>
internal static class EntityCatalog {
    public static readonly (char ch, string meaning)[] Legend = {
        ('#', "地形（实心）"),
        ('.', "空"),
        ('M', "Madeline"),
        ('=', "其他实心块（可能移动、坠落或可撞碎）"),
        ('-', "单向平台（可从下方穿过）"),
        ('^', "尖刺朝上"), ('v', "尖刺朝下"), ('<', "尖刺朝左"), ('>', "尖刺朝右"),
        ('*', "其他致死物（刺球、电网等）"),
        ('S', "弹簧"),
        ('o', "冲刺水晶"),
        ('O', "泡泡（助推）"),
        ('F', "羽毛"),
        ('~', "梦境块（冲刺可穿过）"),
        ('$', "收集品"),
        ('?', "其他可交互物"),
    };

    public static string Category(Entity e) => e switch {
        Spikes or TriggerSpikes or CrystalStaticSpinner or DustStaticSpinner or RotateSpinner or TrackSpinner
            or Lightning or FireBall or Seeker or BadelineOldsite or AngryOshiro or FinalBoss or Snowball
            or SandwichLava or RisingLava => "hazard",
        Spring => "spring",
        Refill => "refill",
        Booster => "booster",
        FlyFeather => "feather",
        Strawberry or Key or HeartGem or Cassette or SummitGem => "collectible",
        DreamBlock => "dream_block",
        DashBlock or FallingBlock or ZipMover or MoveBlock or SwapBlock or CrumblePlatform or BounceBlock
            or StarJumpBlock or CassetteBlock or DashSwitch or FloatySpaceBlock or TempleGate => "dynamic_solid",
        JumpThru => "jumpthru",
        Solid => "solid",
        Bumper or Water or TheoCrystal or Glider or TouchSwitch or Puffer or Lookout or Payphone or NPC => "interactive",
        _ => "other",
    };

    public static char MapChar(Entity e, string category) => category switch {
        "hazard" when e is Spikes spikes => spikes.Direction switch {
            Spikes.Directions.Up => '^',
            Spikes.Directions.Down => 'v',
            Spikes.Directions.Left => '<',
            _ => '>',
        },
        "hazard" => '*',
        "spring" => 'S',
        "refill" => 'o',
        "booster" => 'O',
        "feather" => 'F',
        "collectible" => '$',
        "dream_block" => '~',
        "dynamic_solid" or "solid" => '=',
        "jumpthru" => '-',
        "interactive" => '?',
        _ => '\0',
    };

    public static void WriteProps(Utf8JsonWriter w, Entity e) {
        void Vec(string name, Vector2 v) {
            w.WriteStartArray(name);
            w.WriteNumberValue(v.X);
            w.WriteNumberValue(v.Y);
            w.WriteEndArray();
        }
        switch (e) {
            case Spikes spikes:
                w.WriteString("dir", spikes.Direction.ToString());
                break;
            case Spring spring:
                w.WriteString("orient", spring.Orientation.ToString());
                break;
            case Refill refill:
                w.WriteBoolean("twoDashes", refill.twoDashes);
                if (!refill.Collidable) {
                    w.WriteBoolean("collected", true);
                    w.WriteNumber("respawnTimer", refill.respawnTimer);
                }
                break;
            case Booster booster:
                w.WriteBoolean("red", booster.red);
                break;
            case ZipMover zip:
                // 轨道两端（实体左上角位置），以及当前进度 0~1；画面上轨道本来就画着
                Vec("start", zip.start);
                Vec("target", zip.target);
                w.WriteNumber("percent", zip.percent);
                break;
            case FallingBlock falling:
                w.WriteBoolean("triggered", falling.Triggered);
                w.WriteBoolean("falling", falling.HasStartedFalling);
                break;
            case DashBlock dashBlock:
                w.WriteBoolean("canDash", dashBlock.canDash);
                break;
            case DreamBlock dream:
                w.WriteBoolean("active", dream.playerHasDreamDash);
                if (dream.node is { } node) {
                    Vec("node", node);
                }
                break;
            case SwitchGate gate:
                Vec("node", gate.node);
                break;
            case TouchSwitch touch:
                w.WriteBoolean("activated", touch.Switch?.Activated ?? false);
                break;
            case BridgeTile tile:
                w.WriteBoolean("fallen", tile.Fallen);
                break;
        }
    }
}
