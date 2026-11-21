using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

#pragma warning disable CS0618 // 读取 Engine.TimeRate 等已标记过时的字段，只读不写

namespace CelesteAI.Bridge;

/// <summary>
/// 模拟器的起始状态（sim_state 指令）：用反射把当前房间的场景按帧完整写出，
/// 包括每个实体与组件的全部字段、协程的执行进度、实体列表与追踪器里的顺序、
/// 引擎计时、虚拟按键的缓冲状态与当前按住的键。模拟器据此重建同一帧再往后推演。
/// 这里只写数据（字段名与取值），不含游戏代码。
/// </summary>
internal static class SimState {
    public const string Format = "celeste-sim-state/1";
    private const int MaxDepth = 12;
    private const int MaxCollection = 4096;

    private sealed class Ctx {
        public readonly Dictionary<Entity, int> EntityIndex = new(ReferenceEqualityComparer.Instance);
        public readonly Dictionary<object, int> ObjectIds = new(ReferenceEqualityComparer.Instance);
        public int NextObjectId = 1;
        public readonly HashSet<string> Skipped = new();
        // 精灵动画对象（同一套动画在克隆出来的精灵之间共用）：快照里按编号引用，末尾统一写出
        public readonly Dictionary<Sprite.Animation, int> AnimIds = new(ReferenceEqualityComparer.Instance);
        public readonly List<Sprite.Animation> Anims = new();
    }

    // 实体的跨快照标识：同一个实体对象在各次快照里编号相同（运行中新建的实体也一样），模拟器据此对应前后两次快照
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Entity, object> Uids = new();
    private static long nextUid;

    private static long Uid(Entity e) => (long)Uids.GetValue(e, _ => ++nextUid);

    private static readonly Dictionary<Type, (string key, FieldInfo field)[]> FieldCache = new();

    // 画面、声音、资源与存档类对象不写出：模拟器不需要它们，而且体积大或含有外部句柄
    private static readonly string[] SkipTypeNames = {
        "Monocle.MTexture", "Monocle.Atlas", "Monocle.SpriteBank", "Monocle.SpriteData",
        "Monocle.VirtualAsset", "Monocle.TileGrid", "Monocle.AnimatedTiles", "Monocle.Renderer", "Monocle.Pooler",
        "Monocle.ParticleType", "Celeste.ParticleType", "Celeste.ParticleSystem", "Monocle.ParticleSystem", "Celeste.PlayerHair", "Celeste.SoundSource",
        "Celeste.BloomPoint", "Celeste.VertexLight", "Celeste.LightOcclude", "Celeste.EffectCutout", "Celeste.MirrorReflection",
        "Celeste.Session", "Celeste.MapData", "Celeste.AreaData", "Celeste.LevelData", "Celeste.SaveData", "Celeste.Settings",
        "Celeste.EntityData", "Celeste.Backdrop", "Celeste.TextMenu", "Celeste.DisplacementRenderer",
        "Celeste.Autotiler", "Celeste.Dialog", "Celeste.Language", "Celeste.FancyText", "Monocle.VirtualMap`1",
        // 只负责画面或界面的实体：只写类型，不写字段
        "Celeste.Decal", "Celeste.BackgroundTiles", "Celeste.LightningRenderer", "Celeste.GlassBlockBg",
        "Celeste.SeekerBarrierRenderer", "Celeste.DustEdges", "Celeste.MirrorSurfaces", "Celeste.SpeedrunTimerDisplay",
        "Celeste.TotalStrawberriesDisplay", "Celeste.GameplayStats", "Celeste.GrabbyIcon", "Celeste.FormationBackdrop",
        "Celeste.HiresSnow", "Celeste.ScreenWipe", "Celeste.TrailManager", "Celeste.StrawberriesCounter",
    };
    private static readonly Dictionary<Type, bool> SkipCache = new();

    public static void Write(Utf8JsonWriter w, Level level, int marginTiles) {
        var c = new Ctx();
        EntityList list = level.Entities;
        var all = new List<Entity>();
        foreach (Entity e in list.entities) Add(all, c, e);
        foreach (Entity e in list.toAdd) Add(all, c, e);

        w.WriteString("format", Format);
        w.WriteNumber("frame", Bridge.Frame);
        WriteEngine(w);
        WriteInput(w);
        WriteLevel(w, level, c);
        WriteRooms(w, level);
        WriteSolids(w, level, marginTiles);

        w.WriteStartArray("entities");
        for (int i = 0; i < all.Count; i++) {
            WriteEntity(w, all[i], i, c);
        }
        w.WriteEndArray();

        w.WriteStartObject("lists");
        WriteIndexList(w, "entities", list.entities, c);
        WriteIndexList(w, "toAdd", list.toAdd, c);
        WriteIndexList(w, "toAwake", list.toAwake, c);
        WriteIndexList(w, "toRemove", list.toRemove, c);
        w.WriteBoolean("unsorted", list.unsorted);
        w.WriteEndObject();

        w.WriteStartArray("depthLookup");
        foreach (KeyValuePair<int, double> kv in level.actualDepthLookup.OrderBy(kv => kv.Key)) {
            w.WriteStartArray();
            w.WriteNumberValue(kv.Key);
            w.WriteNumberValue(kv.Value);
            w.WriteEndArray();
        }
        w.WriteEndArray();

        WriteTracker(w, level.Tracker, c);

        // 实体标签的位编号按创建顺序分配（Mod 也可能新增），模拟器按名字对应
        w.WriteStartArray("bitTags");
        for (int i = 0; i < BitTag.TotalTags; i++) w.WriteStringValue(BitTag.byID[i]?.Name);
        w.WriteEndArray();

        w.WriteStartArray("skippedTypes");
        foreach (string s in c.Skipped.OrderBy(s => s)) w.WriteStringValue(s);
        w.WriteEndArray();

        // 精灵动画（上面按编号引用；写出过程中可能继续编号，所以放在最后）
        w.WriteStartArray("anims");
        for (int i = 0; i < c.Anims.Count; i++) WriteAnimation(w, c.Anims[i]);
        w.WriteEndArray();
    }

    private static void Add(List<Entity> all, Ctx c, Entity e) {
        if (c.EntityIndex.ContainsKey(e)) return;
        c.EntityIndex[e] = all.Count;
        all.Add(e);
    }

    // ---- 引擎与输入 ----

    private static void WriteEngine(Utf8JsonWriter w) {
        w.WriteStartObject("engine");
        Num(w, "freezeTimer", Engine.FreezeTimer);
        Num(w, "timeRate", Engine.TimeRate);
        Num(w, "timeRateB", Engine.TimeRateB);
        // 较新的 Everest 才有 EffectiveTimeRate（计时修饰组件），没有时等于两者之积
        Num(w, "effectiveTimeRate", typeof(Engine).GetField("EffectiveTimeRate", BindingFlags.Public | BindingFlags.Static)?.GetValue(null) is float etr ? etr : Engine.TimeRate * Engine.TimeRateB);
        Num(w, "deltaTime", Engine.DeltaTime);
        Num(w, "rawDeltaTime", Engine.RawDeltaTime);
        w.WriteBoolean("dashAssistFreeze", Engine.DashAssistFreeze);
        w.WriteBoolean("dashAssistFreezePress", Engine.DashAssistFreezePress);
        w.WriteNumber("frameCounter", Engine.FrameCounter);
        w.WriteEndObject();
    }

    private static void WriteInput(Utf8JsonWriter w) {
        w.WriteStartObject("input");
        // 上一帧写入的按键：模拟器的第一帧据此判断"新按下"
        w.WriteString("held", Bridge.CurrentInput.ToSimulatorString());
        w.WriteString("input_format", Bridge.CurrentInput.Tas ? "celeste_tas" : "celeste_ai");
        // A:角度 的摇杆向量求值方式（见 StickTrig）；模拟器据此区分旧版桥接（平台 MathF）写出的状态
        w.WriteString("angleMath", "fdlibm");
        w.WriteStartObject("virtual");
        var c = new Ctx();
        foreach ((string name, VirtualInput? input) in new (string, VirtualInput?)[] {
                     ("MoveX", Input.MoveX), ("MoveY", Input.MoveY), ("GliderMoveY", Input.GliderMoveY),
                     ("Aim", Input.Aim), ("Feather", Input.Feather), ("Jump", Input.Jump), ("Dash", Input.Dash),
                     ("Talk", Input.Talk), ("Grab", Input.Grab), ("CrouchDash", Input.CrouchDash),
                     // 速通模式能按暂停、返回、日志键：暂停键有 0.1 秒缓冲，模拟器据此判断这一帧会不会打开暂停菜单
                     ("Pause", Input.Pause), ("MenuCancel", Input.MenuCancel), ("MenuJournal", Input.MenuJournal),
                 }) {
            if (input == null) continue;
            w.WritePropertyName(name);
            WritePlainFields(w, input, c);
        }
        w.WriteEndObject();
        w.WritePropertyName("lastAim");
        Vec(w, Input.LastAim);
        w.WriteBoolean("grabToggle", Input.grabToggle);
        w.WriteEndObject();
    }

    /// <summary>只写数值、布尔与向量字段（虚拟按键的绑定对象不写）。</summary>
    private static void WritePlainFields(Utf8JsonWriter w, object obj, Ctx c) {
        w.WriteStartObject();
        foreach ((string key, FieldInfo f) in FieldsOf(obj.GetType())) {
            Type t = f.FieldType;
            if (!(t.IsPrimitive || t.IsEnum || t == typeof(Vector2))) continue;
            w.WritePropertyName(key);
            Value(w, f.GetValue(obj), c, 0);
        }
        w.WriteEndObject();
    }

    // ---- 关卡、房间与地形 ----

    private static void WriteLevel(Utf8JsonWriter w, Level level, Ctx c) {
        w.WriteStartObject("level");
        w.WriteString("room", level.Session.Level);
        w.WritePropertyName("bounds");
        Rect(w, level.Bounds);
        w.WritePropertyName("camera");
        Vec(w, level.Camera.Position);
        w.WritePropertyName("cameraOffset");
        Vec(w, level.CameraOffset);
        Num(w, "cameraUpwardMaxY", level.CameraUpwardMaxY);
        w.WriteNumber("cameraLockMode", (int) level.CameraLockMode);
        w.WriteBoolean("inCutscene", level.InCutscene);
        w.WriteBoolean("skippingCutscene", level.SkippingCutscene);
        w.WriteBoolean("frozen", level.Frozen);
        w.WriteBoolean("paused", level.Paused);
        w.WriteBoolean("transitioning", level.Transitioning);
        w.WriteBoolean("canRetry", level.CanRetry);
        w.WriteBoolean("pauseLock", level.PauseLock);
        w.WriteBoolean("inSpace", level.InSpace);
        w.WriteBoolean("darkRoom", level.DarkRoom);
        w.WritePropertyName("wind");
        Vec(w, level.Wind);
        Num(w, "timeActive", level.TimeActive);
        Num(w, "rawTimeActive", level.RawTimeActive);
        Num(w, "wasPausedTimer", level.wasPausedTimer);
        // 刚关掉暂停菜单：之后 0.15 秒关卡不推进（unpauseTimer），这一帧内不能再暂停（wasPaused）
        Num(w, "unpauseTimer", level.unpauseTimer);
        w.WriteBoolean("wasPaused", level.wasPaused);
        // 章节计时（Session.Time，刻度）与计时器状态：模拟器据此给出推演结束时的章节计时，并照游戏判断计时何时开始、停止
        w.WriteNumber("chapterTimeTicks", level.Session.Time);
        w.WriteBoolean("timerStarted", level.TimerStarted);
        w.WriteBoolean("timerStopped", level.TimerStopped);
        w.WriteBoolean("completed", level.Completed);
        w.WriteBoolean("inCredits", level.InCredits);
        // 进行中的转场遮罩（Level.Wipe）：遮罩没走完时不能暂停；死亡后的遮罩走完才重载房间（回调写成方法名）
        w.WritePropertyName("wipe");
        if (level.Wipe is { } wipe) {
            w.WriteStartObject();
            w.WriteString("type", TypeName(wipe.GetType()));
            w.WriteBoolean("wipeIn", wipe.WipeIn);
            Num(w, "percent", wipe.Percent);
            Num(w, "duration", wipe.Duration);
            Num(w, "endTimer", wipe.EndTimer);
            w.WriteBoolean("completed", wipe.Completed);
            w.WriteBoolean("rendering", level.RendererList.Renderers.Contains(wipe));
            w.WritePropertyName("onComplete");
            Value(w, wipe.OnComplete, c, 0);
            w.WriteEndObject();
        } else {
            w.WriteNullValue();
        }
        // Madeline 此刻站在哪个交互范围里（TalkComponent.PlayerOver，[实体编号, 组件序号]）：这一帧按交互键时起跳与冲刺会被吞掉
        w.WritePropertyName("talkPlayerOver");
        if (TalkComponent.PlayerOver is { Entity: { } talkOwner } over && c.EntityIndex.TryGetValue(talkOwner, out int talkIndex)
            && talkOwner.Components.components.IndexOf(over) is int talkK and >= 0) {
            w.WriteStartArray();
            w.WriteNumberValue(talkIndex);
            w.WriteNumberValue(talkK);
            w.WriteEndArray();
        } else {
            w.WriteNullValue();
        }
        w.WriteNumber("coreMode", (int) level.CoreMode);
        // 光照强度（镜子传送门等按它计时）与存档里影响关卡逻辑的值（心门按心数开、作弊模式）
        if (level.Lighting != null) Num(w, "lightingAlpha", level.Lighting.Alpha);
        w.WriteNumber("totalHeartGems", SaveData.Instance?.TotalHeartGems ?? 0);
        w.WriteBoolean("cheatMode", SaveData.Instance?.CheatMode ?? false);

        Session s = level.Session;
        w.WriteStartObject("session");
        w.WriteString("area", s.Area.SID ?? s.Area.ToString());
        w.WriteNumber("areaId", s.Area.ID);
        w.WriteNumber("mode", (int) s.Area.Mode);
        w.WriteStartObject("inventory");
        w.WriteNumber("Dashes", s.Inventory.Dashes);
        w.WriteBoolean("DreamDash", s.Inventory.DreamDash);
        w.WriteBoolean("Backpack", s.Inventory.Backpack);
        w.WriteBoolean("NoRefills", s.Inventory.NoRefills);
        w.WriteEndObject();
        w.WriteNumber("coreMode", (int) s.CoreMode);
        w.WriteBoolean("dreaming", s.Dreaming);
        w.WriteBoolean("disableDownTransition", s.LevelData?.DisableDownTransition ?? false);
        w.WriteNumber("deaths", s.Deaths);
        w.WriteNumber("deathsInCurrentLevel", s.DeathsInCurrentLevel);
        w.WriteNumber("dashes", s.Dashes);
        w.WritePropertyName("respawnPoint");
        if (s.RespawnPoint is { } rp) Vec(w, rp); else w.WriteNullValue();
        w.WriteStartArray("flags");
        foreach (string f in s.Flags.OrderBy(x => x, StringComparer.Ordinal)) w.WriteStringValue(f);
        w.WriteEndArray();
        w.WriteStartObject("counters");
        foreach (Session.Counter counter in s.Counters) w.WriteNumber(counter.Key, counter.Value);
        w.WriteEndObject();
        WriteIds(w, "doNotLoad", s.DoNotLoad);
        // 新房间里按进度决定的实体（山脊石门、无冲刺草莓、心、磁带、5A 的 Badeline）与死亡重生要用的会话字段
        w.WriteStartArray("levelFlags");
        foreach (string f in s.LevelFlags.OrderBy(x => x, StringComparer.Ordinal)) w.WriteStringValue(f);
        w.WriteEndArray();
        WriteIds(w, "strawberries", s.Strawberries);
        WriteIds(w, "keys", s.Keys);
        w.WriteBoolean("startedFromBeginning", s.StartedFromBeginning);
        w.WriteBoolean("restartedFromGolden", s.RestartedFromGolden);
        w.WriteBoolean("heartGem", s.HeartGem);
        w.WriteBoolean("cassette", s.Cassette);
        w.WriteBoolean("hitCheckpoint", s.HitCheckpoint);
        w.WriteNumber("dashesAtLevelStart", s.DashesAtLevelStart);
        w.WriteEndObject();
        w.WriteEndObject();
    }

    private static void WriteIds(Utf8JsonWriter w, string name, IEnumerable<EntityID> ids) {
        w.WriteStartArray(name);
        foreach (EntityID id in ids.OrderBy(i => i.Level, StringComparer.Ordinal).ThenBy(i => i.ID)) {
            w.WriteStartArray();
            w.WriteStringValue(id.Level);
            w.WriteNumberValue(id.ID);
            w.WriteEndArray();
        }
        w.WriteEndArray();
    }

    private static void WriteRooms(Utf8JsonWriter w, Level level) {
        // 切换房间的判定按地图里的房间顺序取第一个包含该点的房间（与游戏一致）
        w.WriteStartArray("rooms");
        foreach (LevelData room in level.Session.MapData.Levels) {
            w.WriteStartObject();
            w.WriteString("name", room.Name);
            w.WritePropertyName("bounds");
            Rect(w, room.Bounds);
            w.WriteBoolean("dummy", room.Dummy);
            w.WriteEndObject();
        }
        w.WriteEndArray();
    }

    private static void WriteSolids(Utf8JsonWriter w, Level level, int marginTiles) {
        w.WriteStartObject("solids");
        if (level.SolidTiles?.Collider is not Grid grid) {
            w.WriteEndObject();
            return;
        }
        Rectangle b = level.Bounds;
        float originX = grid.AbsoluteLeft, originY = grid.AbsoluteTop;
        int cw = (int) grid.CellWidth, ch = (int) grid.CellHeight;
        int x0 = Math.Max(0, (int) Math.Floor((b.Left - originX) / cw) - marginTiles);
        int y0 = Math.Max(0, (int) Math.Floor((b.Top - originY) / ch) - marginTiles);
        int x1 = Math.Min(grid.CellsX, (int) Math.Ceiling((b.Right - originX) / cw) + marginTiles);
        int y1 = Math.Min(grid.CellsY, (int) Math.Ceiling((b.Bottom - originY) / ch) + marginTiles);
        w.WritePropertyName("origin");
        Vec(w, new Vector2(originX + x0 * cw, originY + y0 * ch));
        w.WriteNumber("cellWidth", cw);
        w.WriteNumber("cellHeight", ch);
        w.WriteNumber("cellsX", Math.Max(0, x1 - x0));
        w.WriteNumber("cellsY", Math.Max(0, y1 - y0));
        w.WriteStartArray("rows");
        var row = new char[Math.Max(0, x1 - x0)];
        for (int y = y0; y < y1; y++) {
            for (int x = x0; x < x1; x++) row[x - x0] = grid.Data[x, y] ? '1' : '0';
            w.WriteStringValue(new string(row));
        }
        w.WriteEndArray();
        w.WriteEndObject();
    }

    // ---- 实体与组件 ----

    private static void WriteEntity(Utf8JsonWriter w, Entity e, int index, Ctx c) {
        w.WriteStartObject();
        w.WriteNumber("i", index);
        w.WriteNumber("u", Uid(e));
        w.WriteString("type", TypeName(e.GetType()));
        // 基类链：模拟器没有移植的实体，按最近的已知基类（Solid、JumpThru、Trigger 等）给出近似
        w.WriteStartArray("bases");
        for (Type? t = e.GetType().BaseType; t != null && t != typeof(object); t = t.BaseType) w.WriteStringValue(TypeName(t));
        w.WriteEndArray();
        w.WritePropertyName("data");
        WriteEntityData(w, e);
        if (Skip(e.GetType())) {
            w.WriteBoolean("visualOnly", true);
            w.WriteEndObject();
            return;
        }
        w.WritePropertyName("f");
        WriteFields(w, e, c, 0);
        w.WriteStartArray("c");
        int k = 0;
        foreach (Component comp in e.Components) {
            w.WriteStartObject();
            w.WriteNumber("k", k++);
            w.WriteString("type", TypeName(comp.GetType()));
            if (!Skip(comp.GetType())) {
                if (comp is Coroutine routine) WriteCoroutineCore(w, routine, c, 0);
                w.WritePropertyName("f");
                WriteFields(w, comp, c, 0);
            }
            w.WriteEndObject();
        }
        w.WriteEndArray();
        w.WriteEndObject();
    }

    /// <summary>实体来自地图时附上它的地图数据（构造参数），模拟器用同样的构造方式重建。</summary>
    private static void WriteEntityData(Utf8JsonWriter w, Entity e) {
        EntityData? d = e.SourceData;
        if (d == null) {
            w.WriteNullValue();
            return;
        }
        w.WriteStartObject();
        w.WriteString("name", d.Name);
        w.WriteNumber("id", d.ID);
        w.WriteString("level", d.Level?.Name ?? e.SourceId.Level);
        w.WritePropertyName("position");
        Vec(w, d.Position);
        w.WritePropertyName("offset");
        if (d.Level != null) Vec(w, new Vector2(d.Level.Bounds.Left, d.Level.Bounds.Top)); else w.WriteNullValue();
        w.WritePropertyName("origin");
        Vec(w, d.Origin);
        w.WriteNumber("width", d.Width);
        w.WriteNumber("height", d.Height);
        w.WriteStartArray("nodes");
        if (d.Nodes != null) {
            foreach (Vector2 n in d.Nodes) Vec(w, n);
        }
        w.WriteEndArray();
        w.WriteStartObject("values");
        if (d.Values != null) {
            foreach (KeyValuePair<string, object> kv in d.Values) {
                w.WritePropertyName(kv.Key);
                switch (kv.Value) {
                    case null: w.WriteNullValue(); break;
                    case bool b: w.WriteBooleanValue(b); break;
                    case float f: FloatValue(w, f); break;
                    case int i: w.WriteNumberValue(i); break;
                    case short s: w.WriteNumberValue(s); break;
                    case byte by: w.WriteNumberValue(by); break;
                    default: w.WriteStringValue(Convert.ToString(kv.Value, CultureInfo.InvariantCulture)); break;
                }
            }
        }
        w.WriteEndObject();
        w.WriteEndObject();
    }

    private static void WriteFields(Utf8JsonWriter w, object obj, Ctx c, int depth) {
        w.WriteStartObject();
        foreach ((string key, FieldInfo f) in FieldsOf(obj.GetType())) {
            object? v;
            try {
                v = f.GetValue(obj);
            } catch (Exception) {
                continue;
            }
            w.WritePropertyName(key);
            if (v != null && v.GetType().IsPrimitive && !f.FieldType.IsPrimitive) {
                // 装箱的数值（例如协程当前产出的等待秒数）：写明原始类型
                w.WriteStartObject();
                w.WriteString("$p", v.GetType().Name);
                w.WritePropertyName("v");
                Value(w, v, c, depth + 1);
                w.WriteEndObject();
            } else {
                Value(w, v, c, depth + 1);
            }
        }
        if (obj is Sprite sprite) {
            WriteSpriteAnimations(w, sprite, c);
        }
        w.WriteEndObject();
    }

    /// <summary>精灵的动画表：{"$anims": {动画名: 编号}}；当前动画：{"$anim": 编号}。动画内容在快照末尾的 anims 里。</summary>
    private static void WriteSpriteAnimations(Utf8JsonWriter w, Sprite sprite, Ctx c) {
        w.WriteStartObject("animations");
        w.WriteStartObject("$anims");
        if (sprite.animations != null) {
            foreach (KeyValuePair<string, Sprite.Animation> kv in sprite.animations) {
                if (kv.Value != null) w.WriteNumber(kv.Key, AnimId(c, kv.Value));
            }
        }
        w.WriteEndObject();
        w.WriteEndObject();
        w.WritePropertyName("currentAnimation");
        if (sprite.currentAnimation == null) {
            w.WriteNullValue();
        } else {
            w.WriteStartObject();
            w.WriteNumber("$anim", AnimId(c, sprite.currentAnimation));
            w.WriteEndObject();
        }
    }

    private static int AnimId(Ctx c, Sprite.Animation a) {
        if (!c.AnimIds.TryGetValue(a, out int id)) {
            id = c.Anims.Count;
            c.AnimIds[a] = id;
            c.Anims.Add(a);
        }
        return id;
    }

    /// <summary>一套动画：间隔 d、每帧贴图名 f（贴图名为空时写 null）、播完后接续的动画 g（[名字, 权重] 列表）。</summary>
    private static void WriteAnimation(Utf8JsonWriter w, Sprite.Animation a) {
        w.WriteStartObject();
        w.WritePropertyName("d");
        FloatValue(w, a.Delay);
        w.WriteStartArray("f");
        if (a.Frames != null) {
            foreach (MTexture t in a.Frames) {
                if (t?.AtlasPath != null) w.WriteStringValue(t.AtlasPath); else w.WriteNullValue();
            }
        }
        w.WriteEndArray();
        w.WritePropertyName("g");
        if (a.Goto == null) {
            w.WriteNullValue();
        } else {
            w.WriteStartArray();
            foreach (var choice in a.Goto.Choices) {
                w.WriteStartArray();
                w.WriteStringValue(choice.Value);
                FloatValue(w, choice.Weight);
                w.WriteEndArray();
            }
            w.WriteEndArray();
        }
        w.WriteEndObject();
    }

    /// <summary>
    /// 模拟器用的资源表（只在本机使用）：游戏图集里每张贴图的尺寸，以及精灵库里每种精灵的动画。
    /// 运行中新建的精灵（例如新生成的实体）靠它得到与游戏相同的动画帧数与间隔。
    /// </summary>
    public static void WriteAssets(Utf8JsonWriter w) {
        w.WriteString("format", Format);
        w.WriteStartObject("atlases");
        foreach ((string name, Atlas? atlas) in new[] { ("Game", GFX.Game), ("Gui", GFX.Gui), ("Misc", GFX.Misc) }) {
            if (atlas == null) continue;
            w.WriteStartObject(name);
            foreach (KeyValuePair<string, MTexture> kv in atlas.Textures.OrderBy(kv => kv.Key, StringComparer.Ordinal)) {
                w.WriteStartArray(kv.Key);
                w.WriteNumberValue(kv.Value.Width);
                w.WriteNumberValue(kv.Value.Height);
                w.WriteEndArray();
            }
            w.WriteEndObject();
        }
        w.WriteEndObject();
        // Madeline 每一帧贴图的元数据（头发位置、抱物品时的高度偏移等；抱着的东西跟着这个偏移走）
        w.WriteStartObject("playerFrames");
        foreach (KeyValuePair<string, PlayerAnimMetadata> kv in PlayerSprite.FrameMetadata.OrderBy(kv => kv.Key, StringComparer.Ordinal)) {
            PlayerAnimMetadata m = kv.Value;
            if (m == null) continue;
            w.WriteStartArray(kv.Key);
            FloatValue(w, m.HairOffset.X);
            FloatValue(w, m.HairOffset.Y);
            w.WriteNumberValue(m.Frame);
            w.WriteNumberValue(m.CarryYOffset);
            w.WriteBooleanValue(m.HasHair);
            w.WriteEndArray();
        }
        w.WriteEndObject();
        w.WriteStartObject("spriteBanks");
        foreach ((string name, SpriteBank? bank) in new[] { ("SpriteBank", GFX.SpriteBank), ("GuiSpriteBank", GFX.GuiSpriteBank), ("PortraitsSpriteBank", GFX.PortraitsSpriteBank) }) {
            if (bank?.SpriteData == null) continue;
            w.WriteStartObject(name);
            foreach (KeyValuePair<string, SpriteData> kv in bank.SpriteData.OrderBy(kv => kv.Key, StringComparer.Ordinal)) {
                Sprite? s = kv.Value?.Sprite;
                if (s == null) continue;
                w.WriteStartObject(kv.Key);
                w.WriteString("atlas", s.atlas == GFX.Game ? "Game" : s.atlas == GFX.Gui ? "Gui" : s.atlas == GFX.Misc ? "Misc" : null);
                w.WriteString("path", s.Path);
                Vec(w, "position", s.Position);
                Vec(w, "origin", s.Origin);
                if (s.Justify is Vector2 j) Vec(w, "justify", j); else w.WriteNull("justify");
                w.WriteString("start", s.CurrentAnimationID);
                w.WriteBoolean("animating", s.Animating);
                w.WriteNumber("frame", s.CurrentAnimationFrame);
                w.WriteStartObject("anims");
                if (s.animations != null) {
                    foreach (KeyValuePair<string, Sprite.Animation> a in s.animations) {
                        if (a.Value == null) continue;
                        w.WritePropertyName(a.Key);
                        WriteAnimation(w, a.Value);
                    }
                }
                w.WriteEndObject();
                w.WriteEndObject();
            }
            w.WriteEndObject();
        }
        w.WriteEndObject();
    }

    /// <summary>
    /// 类型层级上的全部实例字段。键是字段名；子类与基类同名时，基类的字段写成"类名.字段名"。
    /// 实体的场景、组件列表与地图数据另行写出，不在字段里重复。
    /// </summary>
    private static (string, FieldInfo)[] FieldsOf(Type type) {
        if (FieldCache.TryGetValue(type, out var cached)) return cached;
        var list = new List<(string, FieldInfo)>();
        var used = new HashSet<string>();
        for (Type? t = type; t != null && t != typeof(object); t = t.BaseType) {
            foreach (FieldInfo f in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)) {
                if (t == typeof(Entity) && f.Name is "<Scene>k__BackingField" or "<Components>k__BackingField" or "_sourceData" or "<SourceId>k__BackingField") continue;
                if (t == typeof(Component) && f.Name == "<Entity>k__BackingField") continue;
                // 精灵的动画表与当前动画对象（贴图帧）不写；播放速度、当前动画名、帧号等照常写出（有的实体拿它们计时）
                if (t == typeof(Sprite) && f.Name is "animations" or "currentAnimation") continue;
                string key = used.Add(f.Name) ? f.Name : t.Name + "." + f.Name;
                used.Add(key);
                list.Add((key, f));
            }
        }
        var result = list.ToArray();
        FieldCache[type] = result;
        return result;
    }

    private static bool Skip(Type type) {
        if (SkipCache.TryGetValue(type, out bool s)) return s;
        s = false;
        if (type.Namespace is { } ns && (ns.StartsWith("FMOD") || ns.StartsWith("Microsoft.Xna.Framework.Graphics") || ns.StartsWith("Microsoft.Xna.Framework.Audio")
                                         || ns.StartsWith("System.Reflection"))) {
            s = true;
        }
        // 反射对象（Mod 缓存的 FieldInfo、Type 等）：模拟器用不上，展开后体积很大（一个 Mod 字段就能写出几百 KB）
        if (typeof(MemberInfo).IsAssignableFrom(type) || typeof(System.Reflection.Assembly).IsAssignableFrom(type) || typeof(Module).IsAssignableFrom(type)) {
            s = true;
        }
        for (Type? t = type; !s && t != null; t = t.BaseType) {
            string? name = t.IsGenericType ? t.GetGenericTypeDefinition().FullName : t.FullName;
            if (Array.IndexOf(SkipTypeNames, name) >= 0) s = true;
        }
        SkipCache[type] = s;
        return s;
    }

    private static void Value(Utf8JsonWriter w, object? v, Ctx c, int depth) {
        switch (v) {
            case null:
                w.WriteNullValue();
                return;
            case bool b:
                w.WriteBooleanValue(b);
                return;
            case float f:
                FloatValue(w, f);
                return;
            case double d:
                if (double.IsFinite(d)) w.WriteNumberValue(d); else w.WriteStringValue(d.ToString(CultureInfo.InvariantCulture));
                return;
            case int i: w.WriteNumberValue(i); return;
            case long l: w.WriteNumberValue(l); return;
            case uint ui: w.WriteNumberValue(ui); return;
            case ulong ul: w.WriteNumberValue(ul); return;
            case short sh: w.WriteNumberValue(sh); return;
            case ushort us: w.WriteNumberValue(us); return;
            case byte by: w.WriteNumberValue(by); return;
            case sbyte sb: w.WriteNumberValue(sb); return;
            case char ch: w.WriteStringValue(ch.ToString()); return;
            case string str: w.WriteStringValue(str); return;
            case Enum en: w.WriteNumberValue(Convert.ToInt64(en, CultureInfo.InvariantCulture)); return;
            case Vector2 v2: Vec(w, v2); return;
            case Vector3 v3:
                w.WriteStartArray();
                FloatValue(w, v3.X);
                FloatValue(w, v3.Y);
                FloatValue(w, v3.Z);
                w.WriteEndArray();
                return;
            case Point p:
                w.WriteStartArray();
                w.WriteNumberValue(p.X);
                w.WriteNumberValue(p.Y);
                w.WriteEndArray();
                return;
            case Rectangle r: Rect(w, r); return;
            case Color:
                w.WriteNullValue();
                return;
            case Entity e:
                w.WriteStartObject();
                if (c.EntityIndex.TryGetValue(e, out int ei)) {
                    w.WriteNumber("$e", ei);
                } else {
                    w.WriteNumber("$e", -1);
                    w.WriteString("t", TypeName(e.GetType()));
                }
                w.WriteEndObject();
                return;
            case Scene:
                w.WriteStartObject();
                w.WriteBoolean("$scene", true);
                w.WriteEndObject();
                return;
            case Delegate del:
                w.WriteStartObject();
                w.WriteString("$d", del.Method.Name);
                w.WriteString("dt", del.Method.DeclaringType != null ? TypeName(del.Method.DeclaringType) : "");
                // 回调的对象：实体、组件、场景，或编译器为 lambda 生成的闭包对象（里面是捕获的变量），模拟器据此重新绑定
                w.WritePropertyName("target");
                if (depth < MaxDepth && del.Target is { } target && IsGameObject(target)) Value(w, target, c, depth + 1);
                else w.WriteNullValue();
                w.WriteEndObject();
                return;
        }

        Type type = v.GetType();
        // 指向实体身上组件的引用先写成 $c（即使组件本身是画面声音类），模拟器据此指回同一个组件对象
        if (v is Component comp && comp.Entity != null && c.EntityIndex.TryGetValue(comp.Entity, out int owner)) {
            int k = comp.Entity.Components.components.IndexOf(comp);
            if (k >= 0) {
                w.WriteStartObject();
                w.WriteStartArray("$c");
                w.WriteNumberValue(owner);
                w.WriteNumberValue(k);
                w.WriteEndArray();
                w.WriteEndObject();
                return;
            }
        }
        if (Skip(type)) {
            c.Skipped.Add(TypeName(type));
            w.WriteStartObject();
            w.WriteString("$skip", TypeName(type));
            w.WriteEndObject();
            return;
        }
        if (v is Collider col) {
            WriteCollider(w, col, c, depth);
            return;
        }
        if (depth >= MaxDepth) {
            w.WriteStartObject();
            w.WriteString("$deep", TypeName(type));
            w.WriteEndObject();
            return;
        }
        if (v is IDictionary dict) {
            w.WriteStartObject();
            w.WriteStartArray("$dict");
            int n = 0;
            foreach (DictionaryEntry kv in dict) {
                if (n++ >= MaxCollection) break;
                w.WriteStartArray();
                Value(w, kv.Key, c, depth + 1);
                Value(w, kv.Value, c, depth + 1);
                w.WriteEndArray();
            }
            w.WriteEndArray();
            w.WriteEndObject();
            return;
        }
        if (v is Array or ICollection || IsGenericCollection(type)) {
            // 数组、列表、栈、队列、集合：按枚举顺序写出（栈从栈顶开始）
            w.WriteStartArray();
            int n = 0;
            foreach (object? item in (IEnumerable) v) {
                if (n++ >= MaxCollection) break;
                Value(w, item, c, depth + 1);
            }
            w.WriteEndArray();
            return;
        }
        if (type.IsValueType) {
            w.WriteStartObject();
            w.WriteString("$s", TypeName(type));
            w.WritePropertyName("f");
            WriteFields(w, v, c, depth);
            w.WriteEndObject();
            return;
        }
        if (c.ObjectIds.TryGetValue(v, out int id)) {
            w.WriteStartObject();
            w.WriteNumber("$ref", id);
            w.WriteEndObject();
            return;
        }
        id = c.NextObjectId++;
        c.ObjectIds[v] = id;
        w.WriteStartObject();
        w.WriteString("$o", TypeName(type));
        w.WriteNumber("$id", id);
        if (v is Coroutine co) WriteCoroutineCore(w, co, c, depth);
        w.WritePropertyName("f");
        WriteFields(w, v, c, depth);
        w.WriteEndObject();
    }

    // ---- 被 Mod 包装的协程 ----

    private static readonly System.Reflection.Assembly GameAssembly = typeof(Player).Assembly;

    /// <summary>
    /// Mod 用钩子包装原版协程（例如 On.Celeste.Player.DashCoroutine）时，协程栈里放的是 Mod 程序集里的迭代器，
    /// 原版迭代器藏在它的字段里，层数一多还会被深度上限截断。模拟器没有 Mod 的类型，原来只能整条丢掉，协程随即结束
    /// （冲刺不按时结束等）。这里在 "core" 里另写一份与协程栈逐项对应的原版形状：需要替换的项写成只含原版迭代器的
    /// Flattened（与没装 Mod 时相同），不需要的项写 null，找不到唯一的原版迭代器时写 {"$lost": 原因}。
    /// 整个栈都不涉及 Mod 时不写。写在 "f" 前面，原版迭代器的对象编号先在这里出现。
    /// </summary>
    private static void WriteCoroutineCore(Utf8JsonWriter w, Coroutine routine, Ctx c, int depth) {
        if (routine.enumerators is not { Count: > 0 } stack) return;
        IEnumerator[] items = stack.ToArray(); // 栈顶在前，与写 enumerators 时的顺序相同
        if (items.All(item => !HasForeign(item, 0))) return;
        w.WriteStartArray("core");
        foreach (IEnumerator item in items) {
            if (!HasForeign(item, 0)) {
                w.WriteNullValue();
                continue;
            }
            var core = new List<IEnumerator>();
            string? lost = CoreOf(item, core, 0);
            w.WriteStartObject();
            if (lost != null) {
                w.WriteString("$lost", lost);
                w.WriteEndObject();
                continue;
            }
            // 与原版的深度相同：协程(depth) → enumerators → Flattened → enums → 迭代器
            w.WriteString("$s", TypeName(typeof(Celeste.Mod.SwapImmediatelyExtension.Flattened)));
            w.WriteStartObject("f");
            w.WriteStartArray("enums");
            foreach (IEnumerator e in core) Value(w, e, c, depth + 4);
            w.WriteEndArray();
            object? current = null;
            try {
                current = item.Current;
            } catch (Exception) {
            }
            w.WritePropertyName("current");
            Value(w, current, c, depth + 3);
            w.WriteEndObject();
            w.WriteEndObject();
        }
        w.WriteEndArray();
    }

    private static bool IsGameIterator(Type t) =>
        t.Assembly == GameAssembly && t.Name.StartsWith("<", StringComparison.Ordinal) && typeof(IEnumerator).IsAssignableFrom(t);

    /// <summary>迭代器链（Flattened 的 enums 逐层往下）里有没有不在游戏程序集里的类型。</summary>
    private static bool HasForeign(object? item, int depth) {
        if (item == null || depth > 16) return false;
        if (item is Celeste.Mod.SwapImmediatelyExtension.Flattened flat) return flat.enums != null && flat.enums.Any(e => HasForeign(e, depth + 1));
        return item.GetType().Assembly != GameAssembly;
    }

    /// <summary>按栈顶在前的顺序收集一项里的原版迭代器；Mod 的包装里要恰好找到一个，否则返回原因。</summary>
    private static string? CoreOf(object? item, List<IEnumerator> core, int depth) {
        if (item == null) return null;
        if (depth > 16) return "chain too deep";
        if (item is Celeste.Mod.SwapImmediatelyExtension.Flattened flat) {
            if (flat.enums == null) return null;
            foreach (IEnumerator e in flat.enums) {
                string? lost = CoreOf(e, core, depth + 1);
                if (lost != null) return lost;
            }
            return null;
        }
        Type t = item.GetType();
        if (t.Assembly == GameAssembly) {
            if (item is IEnumerator own) core.Add(own);
            return null;
        }
        var found = new List<IEnumerator>();
        FindGameIterators(item, found, new HashSet<object>(ReferenceEqualityComparer.Instance), 0);
        if (found.Count != 1) return $"{found.Count} vanilla iterators inside {TypeName(t)}";
        core.Add(found[0]);
        return null;
    }

    /// <summary>
    /// 在 Mod 的包装对象里找原版迭代器：只沿着迭代器、迭代器集合、Flattened 结构、SwapImmediately 与编译器生成的类
    /// （闭包）往下，不进实体、组件、场景、委托、反射对象和画面声音类对象。找到原版迭代器就停在那里，不进它内部。
    /// </summary>
    private static void FindGameIterators(object? o, List<IEnumerator> found, HashSet<object> seen, int depth) {
        if (o == null || depth > 16 || o is string or Entity or Component or Scene or Delegate || !seen.Add(o)) return;
        Type t = o.GetType();
        if (t.IsPrimitive || t.IsEnum || Skip(t)) return;
        if (IsGameIterator(t)) {
            if (!found.Contains((IEnumerator) o)) found.Add((IEnumerator) o);
            return;
        }
        if (o is Celeste.Mod.SwapImmediatelyExtension.Flattened flat) {
            if (flat.enums != null) foreach (IEnumerator e in flat.enums) FindGameIterators(e, found, seen, depth + 1);
            return;
        }
        if (o is IEnumerable items and not IEnumerator && (t.IsArray || IsGenericCollection(t))) {
            int n = 0;
            foreach (object? item in items) {
                if (n++ >= MaxCollection) break;
                if (item is IEnumerator or Celeste.Mod.SwapImmediatelyExtension.Flattened) FindGameIterators(item, found, seen, depth + 1);
            }
            return;
        }
        bool walk = o is IEnumerator || t.Name.StartsWith("<", StringComparison.Ordinal) || t.Name.Contains("SwapImmediately") || t.IsValueType;
        if (!walk) return;
        foreach (var (_, f) in FieldsOf(t)) {
            object? v;
            try {
                v = f.GetValue(o);
            } catch (Exception) {
                continue;
            }
            if (v is IEnumerator || (v is IEnumerable and not string && v.GetType() is { } vt && (vt.IsArray || IsGenericCollection(vt)))
                || (v != null && (v.GetType().Name.StartsWith("<", StringComparison.Ordinal) || v.GetType().Name.Contains("SwapImmediately"))))
                FindGameIterators(v, found, seen, depth + 1);
        }
    }

    private static bool IsGameObject(object o) {
        if (o is Entity or Component or Scene) return true;
        Type t = o.GetType();
        return !Skip(t) && t.Namespace is "Celeste" or "Monocle" && !t.IsValueType;
    }

    private static bool IsGenericCollection(Type type) {
        if (!type.IsGenericType) return false;
        Type def = type.GetGenericTypeDefinition();
        return def == typeof(List<>) || def == typeof(Stack<>) || def == typeof(Queue<>) || def == typeof(HashSet<>)
               || def == typeof(LinkedList<>) || def == typeof(SortedSet<>);
    }

    private static void WriteCollider(Utf8JsonWriter w, Collider col, Ctx c, int depth) {
        w.WriteStartObject();
        // 同一个碰撞箱可能被多个字段引用（例如当前碰撞箱与站立碰撞箱），用编号保持同一性
        if (c.ObjectIds.TryGetValue(col, out int known)) {
            w.WriteNumber("$ref", known);
            w.WriteEndObject();
            return;
        }
        int cid = c.NextObjectId++;
        c.ObjectIds[col] = cid;
        w.WriteNumber("$id", cid);
        switch (col) {
            case Hitbox h:
                w.WriteString("$col", "Hitbox");
                Num(w, "w", h.Width);
                Num(w, "h", h.Height);
                w.WritePropertyName("pos");
                Vec(w, h.Position);
                break;
            case Circle ci:
                w.WriteString("$col", "Circle");
                Num(w, "r", ci.Radius);
                w.WritePropertyName("pos");
                Vec(w, ci.Position);
                break;
            case ColliderList cl:
                w.WriteString("$col", "ColliderList");
                w.WritePropertyName("pos");
                Vec(w, cl.Position);
                w.WriteStartArray("list");
                foreach (Collider inner in cl.colliders) WriteCollider(w, inner, c, depth + 1);
                w.WriteEndArray();
                break;
            case Grid g:
                // 地形网格单独写在 solids 里；其他网格（少见）写出全部数据
                w.WriteString("$col", "Grid");
                w.WritePropertyName("pos");
                Vec(w, g.Position);
                w.WriteBoolean("solidTiles", g.Entity is SolidTiles);
                if (g.Entity is not SolidTiles) {
                    Num(w, "cellWidth", g.CellWidth);
                    Num(w, "cellHeight", g.CellHeight);
                    w.WriteNumber("cellsX", g.CellsX);
                    w.WriteNumber("cellsY", g.CellsY);
                    w.WriteStartArray("rows");
                    var row = new char[g.CellsX];
                    for (int y = 0; y < g.CellsY; y++) {
                        for (int x = 0; x < g.CellsX; x++) row[x] = g.Data[x, y] ? '1' : '0';
                        w.WriteStringValue(new string(row));
                    }
                    w.WriteEndArray();
                }
                break;
            default:
                w.WriteString("$col", TypeName(col.GetType()));
                w.WritePropertyName("pos");
                Vec(w, col.Position);
                break;
        }
        w.WriteEndObject();
    }

    private static void WriteIndexList(Utf8JsonWriter w, string name, IEnumerable<Entity> entities, Ctx c) {
        w.WriteStartArray(name);
        foreach (Entity e in entities) {
            w.WriteNumberValue(c.EntityIndex.TryGetValue(e, out int i) ? i : -1);
        }
        w.WriteEndArray();
    }

    private static void WriteTracker(Utf8JsonWriter w, Tracker tracker, Ctx c) {
        w.WriteStartObject("tracker");
        w.WriteStartObject("entities");
        foreach (KeyValuePair<Type, List<Entity>> kv in tracker.Entities.OrderBy(kv => kv.Key.FullName, StringComparer.Ordinal)) {
            if (kv.Value.Count == 0) continue;
            WriteIndexList(w, TypeName(kv.Key), kv.Value, c);
        }
        w.WriteEndObject();
        w.WriteStartObject("components");
        foreach (KeyValuePair<Type, List<Component>> kv in tracker.Components.OrderBy(kv => kv.Key.FullName, StringComparer.Ordinal)) {
            if (kv.Value.Count == 0) continue;
            w.WriteStartArray(TypeName(kv.Key));
            foreach (Component comp in kv.Value) {
                w.WriteStartArray();
                int ei = comp.Entity != null && c.EntityIndex.TryGetValue(comp.Entity, out int idx) ? idx : -1;
                w.WriteNumberValue(ei);
                w.WriteNumberValue(comp.Entity != null ? comp.Entity.Components.components.IndexOf(comp) : -1);
                w.WriteEndArray();
            }
            w.WriteEndArray();
        }
        w.WriteEndObject();
        w.WriteEndObject();
    }

    // ---- 基本写法 ----

    /// <summary>类型名：命名空间 + 类名，嵌套类型用"+"（与反射的 FullName 一致，但去掉泛型参数的程序集信息）。</summary>
    private static string TypeName(Type t) {
        if (!t.IsGenericType) return t.FullName ?? t.Name;
        string def = t.GetGenericTypeDefinition().FullName ?? t.Name;
        return def + "[" + string.Join(",", t.GetGenericArguments().Select(TypeName)) + "]";
    }

    private static void FloatValue(Utf8JsonWriter w, float f) {
        if (float.IsFinite(f)) w.WriteNumberValue(f);
        else w.WriteStringValue(f.ToString(CultureInfo.InvariantCulture));
    }

    private static void Num(Utf8JsonWriter w, string name, float f) {
        w.WritePropertyName(name);
        FloatValue(w, f);
    }

    private static void Vec(Utf8JsonWriter w, Vector2 v) {
        w.WriteStartArray();
        FloatValue(w, v.X);
        FloatValue(w, v.Y);
        w.WriteEndArray();
    }

    private static void Vec(Utf8JsonWriter w, string name, Vector2 v) {
        w.WritePropertyName(name);
        Vec(w, v);
    }

    private static void Rect(Utf8JsonWriter w, Rectangle r) {
        w.WriteStartArray();
        w.WriteNumberValue(r.X);
        w.WriteNumberValue(r.Y);
        w.WriteNumberValue(r.Width);
        w.WriteNumberValue(r.Height);
        w.WriteEndArray();
    }
}
