using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace CelesteAI.Bridge;

/// <summary>一帧内按住的动作。游戏内动作走手柄，菜单动作走键盘，彼此不冲突。</summary>
[Flags]
public enum Act : uint {
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
    // 第二套跳跃、冲刺、蹲冲键：与真人把同一动作绑到两个键一样，按住一个键时按下另一个键也算"按下"
    Jump2 = 1 << 13,
    Dash2 = 1 << 14,
    CrouchDash2 = 1 << 15,
    Grab2 = 1 << 16,
    Restart = 1 << 17,
}

/// <summary>
/// 一帧的输入。文本写法沿用 TAS 习惯，用逗号或加号分隔：
/// L R U D 方向，J 跳，X 冲刺，Z 蹲冲（下冲刺），G 抓，T 交互，C 确认，B 返回，P 暂停，N 日志；
/// K、Y、V 分别是第二个跳、冲刺、蹲冲键（同一动作绑两个键，可以在相邻两帧各"按下"一次）；
/// A:角度 表示左摇杆的模拟方向（0 为右，90 为上），用于羽毛等需要角度的场合。
/// </summary>
public readonly struct InputFrame : IEquatable<InputFrame> {
    public static readonly InputFrame Neutral = new(Act.None, null);

    public readonly Act Actions;
    public readonly float? AngleDeg;
    public readonly bool Tas;

    public InputFrame(Act actions, float? angleDeg, bool tas = false) {
        Actions = actions;
        AngleDeg = angleDeg;
        Tas = tas;
    }

    public bool Has(Act a) => (Actions & a) != 0;

    public static void ValidateTransition(InputFrame previous, bool tas) {
        if (previous.Tas != tas && (previous.Actions != Act.None || previous.AngleDeg != null))
            throw new InvalidOperationException("input_format change requires an explicit neutral frame in the previous format");
    }

    private static readonly Dictionary<char, Act> Letters = new() {
        ['L'] = Act.Left, ['R'] = Act.Right, ['U'] = Act.Up, ['D'] = Act.Down,
        ['J'] = Act.Jump, ['X'] = Act.Dash, ['Z'] = Act.CrouchDash, ['G'] = Act.Grab,
        ['T'] = Act.Talk, ['C'] = Act.Confirm, ['B'] = Act.Back, ['P'] = Act.Pause, ['N'] = Act.Journal,
        ['K'] = Act.Jump2, ['Y'] = Act.Dash2, ['V'] = Act.CrouchDash2,
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

    // Digital CelesteTAS inputs. Their shared menu bindings are installed on
    // separate physical channels, so native inputs retain their old meaning.
    public static InputFrame ParseTas(string? text) {
        Act acts = Act.None;
        foreach (string raw in (text ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries)) {
            string key = raw.Trim().ToUpperInvariant();
            if (key.Length != 1) throw new FormatException($"unsupported TAS input '{raw}'");
            acts |= key[0] switch {
                'L' => Act.Left, 'R' => Act.Right, 'U' => Act.Up, 'D' => Act.Down,
                'J' => Act.Jump, 'K' => Act.Jump2, 'X' => Act.Dash, 'C' => Act.Dash2,
                'Z' => Act.CrouchDash, 'V' => Act.CrouchDash2, 'G' => Act.Grab, 'H' => Act.Grab2,
                'S' => Act.Pause, 'Q' => Act.Restart, 'N' => Act.Journal, 'O' => Act.Confirm,
                _ => throw new FormatException($"unsupported TAS input '{raw}'; analog/custom bindings are not implemented"),
            };
        }
        return new InputFrame(acts, null, tas: true);
    }

    public string ToSimulatorString() {
        // The simulator consumes native gameplay letters, not TAS letters.
        if (Tas && (Actions & (Act.Confirm | Act.Back | Act.Pause | Act.Journal | Act.Restart | Act.Grab2)) != 0)
            throw new NotSupportedException("Simulator cannot export a TAS modal/secondary-grab input state; advance explicitly to a gameplay state first");
        return new InputFrame(Actions | (Tas && Has(Act.Dash) ? Act.Talk : Act.None), AngleDeg).ToString();
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

    public bool Equals(InputFrame other) => Actions == other.Actions && Nullable.Equals(AngleDeg, other.AngleDeg) && Tas == other.Tas;
    public override bool Equals(object? obj) => obj is InputFrame f && Equals(f);
    public override int GetHashCode() => HashCode.Combine(Actions, AngleDeg, Tas);
}
