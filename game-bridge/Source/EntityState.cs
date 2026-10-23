using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Celeste;
using Microsoft.Xna.Framework;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 实体的固定编号与通用状态读取。
/// 编号在同一个关卡场景里不变，便于跨暂停、跨帧对照同一个实体。
/// 状态用反射读出实体自身类上声明的简单字段（数值、布尔、枚举、向量、矩形、节点数组），
/// 不需要为每种实体单独写代码，后面章节的新实体（包括 Mod 实体）也能覆盖。
/// </summary>
internal static class EntityState {
    private static readonly ConditionalWeakTable<Entity, StrongBox<int>> Ids = new();
    private static int nextId = 1;
    private static readonly Dictionary<Type, FieldInfo[]> Cache = new();
    private const int MaxFields = 48;
    private const int MaxArray = 32;

    // 这些基类上的字段属于引擎通用部分（位置、碰撞、深度等），不重复报告
    private static readonly HashSet<Type> StopTypes = new() {
        typeof(Entity), typeof(Actor), typeof(Solid), typeof(Platform), typeof(JumpThru), typeof(Trigger),
    };

    public static int IdOf(Entity e) => Ids.GetValue(e, _ => new StrongBox<int>(nextId++)).Value;

    private static bool Supported(Type t) =>
        t == typeof(bool) || t == typeof(int) || t == typeof(float) || t == typeof(double) || t == typeof(string)
        || t.IsEnum || t == typeof(Vector2) || t == typeof(Vector2?) || t == typeof(Rectangle)
        || t == typeof(Vector2[]) || t == typeof(List<Vector2>) || t == typeof(float[]) || t == typeof(int[]);

    private static FieldInfo[] FieldsOf(Type type) {
        if (Cache.TryGetValue(type, out FieldInfo[]? cached)) {
            return cached;
        }
        var list = new List<FieldInfo>();
        for (Type? t = type; t != null && !StopTypes.Contains(t) && t != typeof(object); t = t.BaseType) {
            foreach (FieldInfo f in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
                                                | BindingFlags.DeclaredOnly)) {
                if (Supported(f.FieldType) && list.Count < MaxFields) {
                    list.Add(f);
                }
            }
        }
        FieldInfo[] result = list.ToArray();
        Cache[type] = result;
        return result;
    }

    private static string CleanName(string name) {
        // 自动属性的后备字段：<Name>k__BackingField → Name
        if (name.StartsWith('<')) {
            int end = name.IndexOf('>');
            if (end > 1) return name[1..end];
        }
        return name;
    }

    private static void Num(Utf8JsonWriter w, float v) {
        if (float.IsFinite(v)) w.WriteNumberValue(v); else w.WriteNullValue();
    }

    private static void Vec(Utf8JsonWriter w, Vector2 v) {
        w.WriteStartArray();
        Num(w, v.X);
        Num(w, v.Y);
        w.WriteEndArray();
    }

    public static void WriteFields(Utf8JsonWriter w, Entity e) {
        w.WriteStartObject("fields");
        var seen = new HashSet<string>();
        foreach (FieldInfo f in FieldsOf(e.GetType())) {
            object? value;
            try {
                value = f.GetValue(e);
            } catch (Exception) {
                continue;
            }
            string name = CleanName(f.Name);
            if (!seen.Add(name)) continue;
            w.WritePropertyName(name);
            switch (value) {
                case null:
                    w.WriteNullValue();
                    break;
                case bool b:
                    w.WriteBooleanValue(b);
                    break;
                case int i:
                    w.WriteNumberValue(i);
                    break;
                case float fl:
                    Num(w, fl);
                    break;
                case double d:
                    if (double.IsFinite(d)) w.WriteNumberValue(d); else w.WriteNullValue();
                    break;
                case string s:
                    w.WriteStringValue(s.Length > 64 ? s[..64] : s);
                    break;
                case Enum en:
                    w.WriteStringValue(en.ToString());
                    break;
                case Vector2 v:
                    Vec(w, v);
                    break;
                case Rectangle r:
                    w.WriteStartArray();
                    w.WriteNumberValue(r.X);
                    w.WriteNumberValue(r.Y);
                    w.WriteNumberValue(r.Width);
                    w.WriteNumberValue(r.Height);
                    w.WriteEndArray();
                    break;
                case Vector2[] arr:
                    w.WriteStartArray();
                    for (int k = 0; k < arr.Length && k < MaxArray; k++) Vec(w, arr[k]);
                    w.WriteEndArray();
                    break;
                case List<Vector2> lst:
                    w.WriteStartArray();
                    for (int k = 0; k < lst.Count && k < MaxArray; k++) Vec(w, lst[k]);
                    w.WriteEndArray();
                    break;
                case float[] fa:
                    w.WriteStartArray();
                    for (int k = 0; k < fa.Length && k < MaxArray; k++) Num(w, fa[k]);
                    w.WriteEndArray();
                    break;
                case int[] ia:
                    w.WriteStartArray();
                    for (int k = 0; k < ia.Length && k < MaxArray; k++) w.WriteNumberValue(ia[k]);
                    w.WriteEndArray();
                    break;
                default:
                    w.WriteNullValue();
                    break;
            }
        }
        // 引擎通用的运动信息
        switch (e) {
            case Actor actor:
                w.WritePropertyName("liftSpeed");
                Vec(w, actor.LiftSpeed);
                break;
            case Platform platform:
                w.WritePropertyName("liftSpeed");
                Vec(w, platform.LiftSpeed);
                break;
        }
        if (e.Get<StateMachine>() is { } machine) {
            w.WriteNumber("stateMachine", machine.State);
        }
        w.WriteEndObject();
    }

    /// <summary>逐帧轨迹用：实体碰撞箱左上角（与观测里的 x、y 一致）。</summary>
    public static bool TryTopLeft(Entity e, out Vector2 topLeft) {
        if (e.Collider is { } c) {
            topLeft = new Vector2(c.AbsoluteLeft, c.AbsoluteTop);
            return true;
        }
        topLeft = e.Position;
        return false;
    }
}
