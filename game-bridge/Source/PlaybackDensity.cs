using System;

namespace CelesteAI.Bridge;

/// <summary>按实际绘制条数控制尾迹；少量路线保持完整，多路线只保留较新的普通线段。</summary>
internal readonly struct PlaybackDensity {
    public readonly float HalfLife;
    public readonly float Dim;
    public readonly float HistoryRetention;

    public PlaybackDensity(int count) {
        float ratio = Math.Min(1f, 12f / Math.Max(1, count));
        HalfLife = count <= 12 ? float.PositiveInfinity : Math.Max(2f, 18f * MathF.Sqrt(ratio));
        Dim = Math.Max(0.02f, MathF.Pow(ratio, 0.7f));
        HistoryRetention = 0.2f * ratio;
    }

    public float Visibility(float age) => MathF.Pow(0.5f, Math.Max(0f, age) / HalfLife);

    public int FadeFrames(int requested) => requested <= 0 ? 0
        : Math.Max(1, (int)MathF.Round(requested * MathF.Sqrt(HistoryRetention / 0.2f)));
}
