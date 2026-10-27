using System;
using System.Buffers;
using System.Collections.Generic;
using System.Text.Encodings.Web;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 一次"推进 N 帧"的任务：逐帧取出按键，记录轨迹与事件，按截断条件提前停下。
/// 截断条件：death 死亡、room 换房、uncontrollable 失去控制、controllable 恢复控制、
/// land 落地、refill 冲刺次数恢复、complete 通关、story_input 到达剧情输入节点、story_decision 到达剧情决策机会。
/// stop_rooms：进入其中某个房间时以 room 停下（按段通关用，其他换房间不停）。
/// 给了预期轨迹（expect）时还会逐帧对照 Madeline 的位置与状态，偏差超过容差（tolerance）就以 deviate 停下。
/// </summary>
internal sealed class StepJob {
    /// <summary>预期轨迹的一帧：该帧推进之后 Madeline 的位置（与轨迹里的 x、y 同一口径），可选状态名。</summary>
    public readonly record struct ExpectRow(float X, float Y, string? State);

    public readonly long RequestId;
    public readonly string InputFormat;
    public readonly int FramesPerTick;
    public readonly bool WantImage;
    public readonly string Buffer;
    public readonly SnapshotOptions Snapshot;

    private readonly List<InputFrame> plan;
    private readonly HashSet<string> stopOn;
    private readonly HashSet<string> stopRooms;
    private readonly MapEntryGuard? entryGuard;
    private readonly bool allowChapterReset;
    private readonly bool allowMapExit;
    private readonly bool recordTrajectory;
    private readonly ArrayBufferWriter<byte> trajectoryBuffer = new();
    private readonly Utf8JsonWriter trajectory;
    private readonly List<(long frame, string type, string? detail)> events = new();
    private readonly bool recordEntities;
    private readonly ArrayBufferWriter<byte> entityBuffer = new();
    private readonly Utf8JsonWriter entityTrajectory;
    private readonly Dictionary<int, Vector2> lastEntityPos = new();
    private readonly long startFrame;
    private readonly int progressEvery;
    private readonly List<ExpectRow?>? expect;
    private readonly float tolerance;
    private (long frame, int index, ExpectRow expected, float? x, float? y, string? state)? deviation;

    private int index;
    private string? lastRoom;
    private bool lastAlive;
    private bool lastControllable;
    private string? lastStoryInput;
    private string? lastStoryDecision;
    private bool lastOnGround;
    private int lastDashes;

    public int LoadingFrames;
    public string? StopReason { get; private set; }
    public bool Finished => StopReason != null;

    public StepJob(long requestId, List<InputFrame> plan, IEnumerable<string> stopOn, int framesPerTick,
        bool wantImage, string buffer, SnapshotOptions snapshot, bool recordTrajectory, bool recordEntities = false,
        List<ExpectRow?>? expect = null, float tolerance = 1f, int progressEvery = 0, IEnumerable<string>? stopRooms = null,
        string inputFormat = "celeste_ai", MapEntryGuard? entryGuard = null, bool allowChapterReset = false,
        bool allowMapExit = false) {
        RequestId = requestId;
        InputFormat = inputFormat;
        this.entryGuard = entryGuard;
        this.allowChapterReset = allowChapterReset;
        this.allowMapExit = allowMapExit;
        this.progressEvery = Math.Max(0, progressEvery);
        this.expect = expect is { Count: > 0 } ? expect : null;
        this.tolerance = tolerance;
        this.plan = plan;
        this.stopOn = new HashSet<string>(stopOn, StringComparer.OrdinalIgnoreCase);
        this.stopRooms = new HashSet<string>(stopRooms ?? Array.Empty<string>(), StringComparer.Ordinal);
        FramesPerTick = framesPerTick;
        WantImage = wantImage;
        Buffer = buffer;
        Snapshot = snapshot;
        this.recordTrajectory = recordTrajectory;
        trajectory = new Utf8JsonWriter(trajectoryBuffer, new JsonWriterOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        trajectory.WriteStartArray();
        this.recordEntities = recordEntities;
        entityTrajectory = new Utf8JsonWriter(entityBuffer, new JsonWriterOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        entityTrajectory.WriteStartArray();
        startFrame = Bridge.Frame;

        Level? level = Engine.Scene as Level;
        Player? p = level == null ? null : StateExtractor.GetPlayer(level);
        lastRoom = level?.Session.Level;
        lastAlive = p is { Dead: false };
        lastControllable = StateExtractor.IsControllable(level, p);
        lastStoryInput = StoryText.InputNode(Engine.Scene);
        lastStoryDecision = StoryText.DecisionNode(Engine.Scene);
        lastOnGround = p?.onGround ?? false;
        lastDashes = p?.Dashes ?? 0;
        if (recordEntities && level != null) {
            // 记下起始位置作为基准，之后只记录有变化的帧
            Rectangle view = StateExtractor.ViewRect(level, 64);
            foreach (Entity e in level.Entities) {
                if (e is Player or SolidTiles or Trigger || e.Collider == null) continue;
                EntityState.TryTopLeft(e, out Vector2 pos);
                if (view.Contains((int) pos.X, (int) pos.Y) || e.Collider.Bounds.Intersects(view)) {
                    lastEntityPos[EntityState.IdOf(e)] = pos;
                }
            }
        }

        // "直到可控/失控"类条件在开始时就已满足，则不推进
        if (this.stopOn.Contains("controllable") && lastControllable) {
            StopReason = "controllable";
        } else if (this.stopOn.Contains("uncontrollable") && !lastControllable) {
            StopReason = "uncontrollable";
        } else if (plan.Count == 0) {
            StopReason = "done";
        }
    }

    public InputFrame NextInput() => plan[index++];

    public bool AllowNextInput() {
        if (entryGuard != null && Engine.Scene is Overworld { Current: OuiChapterPanel panel }) {
            string? reason = entryGuard.Check(plan[index], panel.Area.SID, SaveData.Instance?.FileSlot);
            if (reason != null) {
                StopReason = "entry_blocked";
                events.Add((Bridge.Frame, "entry_blocked", reason));
                return false;
            }
        }
        return true;
    }

    public bool AllowMenuConfirmation(TextMenu.Item item) {
        if (!allowChapterReset && ChapterResetGuard.Check(item,
                allowMapExit || Engine.Scene is Level { Completed: true }) is { } blocked) {
            StopReason = "chapter_reset_blocked";
            events.Add((Bridge.Frame, "chapter_reset_blocked", blocked));
            return false;
        }
        return true;
    }

    public void AfterFrame(long frame) {
        Level? level = Engine.Scene as Level;
        Player? p = level == null ? null : StateExtractor.GetPlayer(level);
        if (recordTrajectory) {
            StateExtractor.WriteTrajectoryRow(trajectory, frame, p);
        }
        if (recordEntities && level != null) {
            RecordEntities(level, frame);
        }

        bool alive = p is { Dead: false };
        string? room = level?.Session.Level;
        bool controllable = StateExtractor.IsControllable(level, p);
        bool onGround = alive && p!.onGround;
        int dashes = p?.Dashes ?? 0;

        if (PlayerControl.IsDeathTransition(lastAlive, p)) Emit(frame, "death", room);
        if (room != null && lastRoom != null && room != lastRoom) {
            Emit(frame, "room", room);
            if (StopReason == null && stopRooms.Contains(room)) StopReason = "room";
        }
        if (lastControllable && !controllable) Emit(frame, "uncontrollable", null);
        if (!lastControllable && controllable) Emit(frame, "controllable", null);
        if (!lastOnGround && onGround) Emit(frame, "land", null);
        if (alive && dashes > lastDashes) Emit(frame, "refill", dashes.ToString());
        if (level is { Completed: true }) Emit(frame, "complete", null);
        string? storyInput = StoryText.InputNode(Engine.Scene);
        if (storyInput != null && storyInput != lastStoryInput) Emit(frame, "story_input", storyInput);
        lastStoryInput = storyInput;
        string? storyDecision = StoryText.DecisionNode(Engine.Scene);
        if (storyDecision != null && storyDecision != lastStoryDecision) Emit(frame, "story_decision", storyDecision);
        lastStoryDecision = storyDecision;

        CheckExpectation(frame, p);
        if (progressEvery > 0 && level != null && (index % progressEvery == 0 || StopReason != null || index >= plan.Count)) {
            SendProgress(level, p, frame);
        }

        lastAlive = alive;
        if (room != null) lastRoom = room;
        lastControllable = controllable;
        lastOnGround = onGround;
        lastDashes = dashes;

        if (StopReason == null && index >= plan.Count) {
            StopReason = "done";
        }
    }

    public void Cancel() => StopReason ??= "cancelled";

    /// <summary>执行过程中定时推送进度（HUD 用来逐帧显示章节计时、冲刺次数与房间）。</summary>
    private void SendProgress(Level level, Player? p, long frame) {
        Session s = level.Session;
        IpcServer.Send(Reply.Event("progress", w => {
            w.WriteNumber("req", RequestId);
            w.WriteNumber("frame", frame);
            w.WriteNumber("index", index);
            w.WriteString("sid", s.Area.SID);
            w.WriteString("room", s.Level);
            w.WriteNumber("chapterTimeTicks", s.Time);
            w.WriteNumber("deaths", s.Deaths);
            w.WriteNumber("dashes", p?.Dashes ?? 0);
            w.WriteNumber("maxDashes", p?.MaxDashes ?? 0);
            w.WriteBoolean("dead", p == null || p.Dead);
        }));
    }

    /// <summary>对照刚推进的这一帧与预期轨迹；第一次超出容差时记下偏差并停下。</summary>
    private void CheckExpectation(long frame, Player? p) {
        int i = index - 1; // 刚执行的是计划里的第 i 帧（从 0 起）
        if (expect == null || deviation != null || i < 0 || i >= expect.Count || expect[i] is not { } row) {
            return;
        }
        float? x = p == null ? null : p.Position.X + p.PositionRemainder.X;
        float? y = p == null ? null : p.Position.Y + p.PositionRemainder.Y;
        string? state = p == null ? null : StateExtractor.StateName(p.StateMachine.State);
        bool off = x == null || y == null
                   || MathF.Abs(x.Value - row.X) > tolerance || MathF.Abs(y.Value - row.Y) > tolerance
                   || (row.State != null && !string.Equals(row.State, state, StringComparison.OrdinalIgnoreCase));
        if (!off) {
            return;
        }
        deviation = (frame, i + 1, row, x, y, state);
        events.Add((frame, "deviate", (i + 1).ToString()));
        StopReason ??= "deviate";
    }

    /// <summary>画面附近（外扩 64 像素）每个实体：位置有变化的帧记一行 [帧, 编号, x, y]（碰撞箱左上角）。</summary>
    private void RecordEntities(Level level, long frame) {
        Rectangle view = StateExtractor.ViewRect(level, 64);
        foreach (Entity e in level.Entities) {
            if (e is Player or SolidTiles or Trigger || e.Collider == null) continue;
            EntityState.TryTopLeft(e, out Vector2 pos);
            if (!view.Contains((int) pos.X, (int) pos.Y) && !e.Collider.Bounds.Intersects(view)) continue;
            int id = EntityState.IdOf(e);
            if (lastEntityPos.TryGetValue(id, out Vector2 last) && last == pos) continue;
            lastEntityPos[id] = pos;
            entityTrajectory.WriteStartArray();
            entityTrajectory.WriteNumberValue(frame);
            entityTrajectory.WriteNumberValue(id);
            entityTrajectory.WriteNumberValue(pos.X);
            entityTrajectory.WriteNumberValue(pos.Y);
            entityTrajectory.WriteEndArray();
        }
    }

    private void Emit(long frame, string type, string? detail) {
        events.Add((frame, type, detail));
        if (StopReason == null && stopOn.Contains(type)) {
            StopReason = type;
        }
    }

    public void WriteResult(Utf8JsonWriter w) {
        w.WriteString("input_format", InputFormat);
        w.WriteString("stop", StopReason ?? "running");
        w.WriteNumber("consumed", index);
        w.WriteNumber("planned", plan.Count);
        w.WriteNumber("loadingFrames", LoadingFrames);
        w.WriteNumber("startFrame", startFrame);
        w.WriteNumber("endFrame", Bridge.Frame);
        w.WriteStartArray("events");
        foreach (var (frame, type, detail) in events) {
            w.WriteStartObject();
            w.WriteNumber("frame", frame);
            w.WriteString("type", type);
            if (detail != null) w.WriteString("detail", detail);
            w.WriteEndObject();
        }
        w.WriteEndArray();
        if (expect != null) {
            w.WriteNumber("expectFrames", expect.Count);
            w.WriteNumber("tolerance", tolerance);
        }
        if (deviation is { } d) {
            w.WriteStartObject("deviation");
            w.WriteNumber("frame", d.frame);
            w.WriteNumber("index", d.index);
            w.WriteStartArray("expected");
            w.WriteNumberValue(d.expected.X);
            w.WriteNumberValue(d.expected.Y);
            if (d.expected.State != null) w.WriteStringValue(d.expected.State);
            w.WriteEndArray();
            w.WriteStartArray("actual");
            if (d.x is { } ax) w.WriteNumberValue(ax); else w.WriteNullValue();
            if (d.y is { } ay) w.WriteNumberValue(ay); else w.WriteNullValue();
            if (d.state != null) w.WriteStringValue(d.state); else w.WriteNullValue();
            w.WriteEndArray();
            w.WriteEndObject();
        }
        if (recordTrajectory) {
            trajectory.WriteEndArray();
            trajectory.Flush();
            w.WriteStartObject("trajectoryFormat");
            w.WriteString("columns", "frame,x,y,vx,vy,state,dashes,onGround");
            w.WriteEndObject();
            w.WritePropertyName("trajectory");
            w.WriteRawValue(trajectoryBuffer.WrittenSpan, skipInputValidation: true);
        }
        if (recordEntities) {
            entityTrajectory.WriteEndArray();
            entityTrajectory.Flush();
            w.WriteString("entityTrajectoryFormat", "frame,id,x,y");
            w.WritePropertyName("entityTrajectory");
            w.WriteRawValue(entityBuffer.WrittenSpan, skipInputValidation: true);
        }
    }

    /// <summary>解析 expect：每个元素是 null（这一帧不检查）、[x, y] 或 [x, y, "状态名"]。</summary>
    public static List<ExpectRow?>? ParseExpect(JsonElement a, int maxFrames) {
        if (!a.TryGetProperty("expect", out JsonElement el) || el.ValueKind == JsonValueKind.Null) {
            return null;
        }
        if (el.ValueKind != JsonValueKind.Array) {
            throw new FormatException("expect must be an array");
        }
        var rows = new List<ExpectRow?>();
        foreach (JsonElement row in el.EnumerateArray()) {
            if (rows.Count >= maxFrames) {
                throw new FormatException($"expect exceeds {maxFrames} frames");
            }
            if (row.ValueKind == JsonValueKind.Null) {
                rows.Add(null);
                continue;
            }
            if (row.ValueKind != JsonValueKind.Array || row.GetArrayLength() < 2
                || row[0].ValueKind != JsonValueKind.Number || row[1].ValueKind != JsonValueKind.Number) {
                throw new FormatException("each expect row must be null, [x, y] or [x, y, state]");
            }
            string? state = row.GetArrayLength() >= 3 && row[2].ValueKind == JsonValueKind.String ? row[2].GetString() : null;
            rows.Add(new ExpectRow(row[0].GetSingle(), row[1].GetSingle(), state));
        }
        return rows;
    }

    /// <summary>
    /// 解析 inputs：数组元素可以是 "6,R,J"（前面的数字是帧数）或 {"n":6,"keys":"R,J"}。
    /// </summary>
    public static List<InputFrame> ParsePlan(JsonElement inputs, int maxFrames, string inputFormat = "celeste_ai") {
        if (inputFormat is not ("celeste_ai" or "celeste_tas")) throw new FormatException("unknown input_format");
        var result = new List<InputFrame>();
        if (inputs.ValueKind != JsonValueKind.Array) {
            throw new FormatException("inputs must be an array");
        }
        foreach (JsonElement seg in inputs.EnumerateArray()) {
            int n;
            string keys;
            if (seg.ValueKind == JsonValueKind.String) {
                string text = seg.GetString() ?? "";
                int comma = text.IndexOf(',');
                string countPart = comma < 0 ? text : text[..comma];
                keys = comma < 0 ? "" : text[(comma + 1)..];
                if (!int.TryParse(countPart.Trim(), out n)) {
                    throw new FormatException($"segment '{text}' must start with a frame count");
                }
            } else if (seg.ValueKind == JsonValueKind.Object) {
                n = seg.TryGetProperty("n", out JsonElement nEl) ? nEl.GetInt32() : 1;
                keys = seg.TryGetProperty("keys", out JsonElement kEl) ? kEl.GetString() ?? "" : "";
            } else {
                throw new FormatException("each segment must be a string or an object");
            }
            if (n < 0) throw new FormatException("frame count must be >= 0");
            InputFrame frame = inputFormat == "celeste_tas" ? InputFrame.ParseTas(keys) : InputFrame.Parse(keys);
            for (int i = 0; i < n; i++) {
                result.Add(frame);
                if (result.Count > maxFrames) {
                    throw new FormatException($"plan exceeds {maxFrames} frames");
                }
            }
        }
        return result;
    }
}
