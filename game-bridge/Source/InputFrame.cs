using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace CelesteAI.Bridge;

/// <summary>一帧内按住的动作。游戏内动作走手柄，菜单动作走键盘，彼此不冲突。</summary>
[Flags]
public enum Act : ushort {
    None = 0,
    Left = 1 << 0,
    Right = 1 << 1,
    Up = 1 << 2,
    Down = 1 << 3,
    Jump = 1 << 4,
    Dash = 1 << 5,
    CrouchDash = 1 << 6,
    Grab = 1 << 7,
    Talk = 1 << 8,
    Confirm = 1 << 9,
    Back = 1 << 10,
    Pause = 1 << 11,
    Journal = 1 << 12,
}

/// <summary>
/// 一帧的输入。文本写法沿用 TAS 习惯，用逗号或加号分隔：
/// L R U D 方向，J 跳，X 冲刺，Z 下冲刺，G 抓，T 交互，C 确认，B 返回，P 暂停，N 日志；
/// A:角度 表示左摇杆的模拟方向（0 为右，90 为上），用于羽毛等需要角度的场合。
/// </summary>
public readonly struct InputFrame : IEquatable<InputFrame> {
    public static readonly InputFrame Neutral = new(Act.None, null);

    public readonly Act Actions;
    public readonly float? AngleDeg;

    public InputFrame(Act actions, float? angleDeg) {
        Actions = actions;
        AngleDeg = angleDeg;
    }

    public bool Has(Act a) => (Actions & a) != 0;

    private static readonly Dictionary<char, Act> Letters = new() {
        ['L'] = Act.Left, ['R'] = Act.Right, ['U'] = Act.Up, ['D'] = Act.Down,
        ['J'] = Act.Jump, ['X'] = Act.Dash, ['Z'] = Act.CrouchDash, ['G'] = Act.Grab,
        ['T'] = Act.Talk, ['C'] = Act.Confirm, ['B'] = Act.Back, ['P'] = Act.Pause, ['N'] = Act.Journal,
    };

    public static InputFrame Parse(string? text) {
        if (string.IsNullOrWhiteSpace(text)) {
            return Neutral;
        }

        Act acts = Act.None;
        float? angle = null;
        foreach (string raw in text.Split(new[] { ',', '+', ' ' }, StringSplitOptions.RemoveEmptyEntries)) {
            string token = raw.Trim().ToUpperInvariant();
            if (token.StartsWith("A:")) {
                if (!float.TryParse(token.AsSpan(2), NumberStyles.Float, CultureInfo.InvariantCulture, out float deg)) {
                    throw new FormatException($"bad angle token '{raw}'");
                }
                angle = deg;
                continue;
            }
            foreach (char c in token) {
                if (!Letters.TryGetValue(c, out Act a)) {
                    throw new FormatException($"unknown key '{c}' in '{raw}'");
                }
                acts |= a;
            }
        }
        return new InputFrame(acts, angle);
    }

    public override string ToString() {
        var sb = new StringBuilder();
        foreach (var (letter, act) in Letters) {
            if ((Actions & act) != 0) {
                if (sb.Length > 0) sb.Append(',');
                sb.Append(letter);
            }
        }
        if (AngleDeg is { } a) {
            if (sb.Length > 0) sb.Append(',');
            sb.Append("A:").Append(a.ToString(CultureInfo.InvariantCulture));
        }
        return sb.ToString();
    }

    public bool Equals(InputFrame other) => Actions == other.Actions && Nullable.Equals(AngleDeg, other.AngleDeg);
    public override bool Equals(object? obj) => obj is InputFrame f && Equals(f);
    public override int GetHashCode() => HashCode.Combine(Actions, AngleDeg);
}
