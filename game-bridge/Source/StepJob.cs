using System;
using System.Buffers;
using System.Collections.Generic;
using System.Text.Encodings.Web;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 一次"推进 N 帧"的任务：逐帧取出按键，记录轨迹与事件，按截断条件提前停下。
/// 截断条件：death 死亡、room 换房、uncontrollable 失去控制、controllable 恢复控制、
/// land 落地、refill 冲刺次数恢复、complete 通关。
/// </summary>
internal sealed class StepJob {
    public readonly long RequestId;
    public readonly int FramesPerTick;
    public readonly bool WantImage;
    public readonly string Buffer;
    public readonly SnapshotOptions Snapshot;

    private readonly List<InputFrame> plan;
    private readonly HashSet<string> stopOn;
    private readonly bool recordTrajectory;
    private readonly ArrayBufferWriter<byte> trajectoryBuffer = new();
    private readonly Utf8JsonWriter trajectory;
    private readonly List<(long frame, string type, string? detail)> events = new();
    private readonly long startFrame;

    private int index;
    private string? lastRoom;
    private bool lastAlive;
    private bool lastControllable;
    private bool lastOnGround;
    private int lastDashes;

    public int LoadingFrames;
    public string? StopReason { get; private set; }
    public bool Finished => StopReason != null;

    public StepJob(long requestId, List<InputFrame> plan, IEnumerable<string> stopOn, int framesPerTick,
        bool wantImage, string buffer, SnapshotOptions snapshot, bool recordTrajectory) {
        RequestId = requestId;
        this.plan = plan;
        this.stopOn = new HashSet<string>(stopOn, StringComparer.OrdinalIgnoreCase);
        FramesPerTick = framesPerTick;
        WantImage = wantImage;
        Buffer = buffer;
        Snapshot = snapshot;
        this.recordTrajectory = recordTrajectory;
        trajectory = new Utf8JsonWriter(trajectoryBuffer, new JsonWriterOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        trajectory.WriteStartArray();
        startFrame = Bridge.Frame;

        Level? level = Engine.Scene as Level;
        Player? p = level == null ? null : StateExtractor.GetPlayer(level);
        lastRoom = level?.Session.Level;
        lastAlive = p is { Dead: false };
        lastControllable = StateExtractor.IsControllable(level, p);
        lastOnGround = p?.onGround ?? false;
        lastDashes = p?.Dashes ?? 0;

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

    public void AfterFrame(long frame) {
        Level? level = Engine.Scene as Level;
        Player? p = level == null ? null : StateExtractor.GetPlayer(level);
        if (recordTrajectory) {
            StateExtractor.WriteTrajectoryRow(trajectory, frame, p);
        }

        bool alive = p is { Dead: false };
        string? room = level?.Session.Level;
        bool controllable = StateExtractor.IsControllable(level, p);
        bool onGround = alive && p!.onGround;
        int dashes = p?.Dashes ?? 0;

        if (lastAlive && !alive) Emit(frame, "death", room);
        if (room != null && lastRoom != null && room != lastRoom) Emit(frame, "room", room);
        if (lastControllable && !controllable) Emit(frame, "uncontrollable", null);
        if (!lastControllable && controllable) Emit(frame, "controllable", null);
        if (!lastOnGround && onGround) Emit(frame, "land", null);
        if (alive && dashes > lastDashes) Emit(frame, "refill", dashes.ToString());
        if (level is { Completed: true }) Emit(frame, "complete", null);

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

    private void Emit(long frame, string type, string? detail) {
        events.Add((frame, type, detail));
        if (StopReason == null && stopOn.Contains(type)) {
            StopReason = type;
        }
    }

    public void WriteResult(Utf8JsonWriter w) {
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
        if (recordTrajectory) {
            trajectory.WriteEndArray();
            trajectory.Flush();
            w.WriteStartObject("trajectoryFormat");
            w.WriteString("columns", "frame,x,y,vx,vy,state,dashes,onGround");
            w.WriteEndObject();
            w.WritePropertyName("trajectory");
            w.WriteRawValue(trajectoryBuffer.WrittenSpan, skipInputValidation: true);
        }
    }

    /// <summary>
    /// 解析 inputs：数组元素可以是 "6,R,J"（前面的数字是帧数）或 {"n":6,"keys":"R,J"}。
    /// </summary>
    public static List<InputFrame> ParsePlan(JsonElement inputs, int maxFrames) {
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
            InputFrame frame = InputFrame.Parse(keys);
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
