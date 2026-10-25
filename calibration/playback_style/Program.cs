using CelesteAI.Bridge;

void Check(bool condition, string message) {
    if (!condition) throw new Exception(message);
}

var sparse = new PlaybackDensity(8);
Check(sparse.Visibility(120) == 1f && sparse.FadeFrames(8) == 8, "少量路线不应被强制截短");
float previousVisibility = 1f, previousDim = 1f, previousHistory = 1f;
int previousFade = 8;
foreach (int count in new[] { 0, 1, 12, 13, 40, 100, 500, 2000, 100000 }) {
    var style = new PlaybackDensity(count);
    float visibility = style.Visibility(12);
    Check(float.IsFinite(visibility) && visibility >= 0f && visibility <= previousVisibility, "路线越多，尾迹不得更慢褪色");
    Check(style.Dim > 0f && style.Dim <= previousDim, "普通路线密集时不得反而更亮");
    Check(style.HistoryRetention >= 0f && style.HistoryRetention <= previousHistory, "历史层应更快退场");
    Check(style.FadeFrames(8) >= 1 && style.FadeFrames(8) <= previousFade, "轮末转灰应随密度加快");
    Check(style.FadeFrames(0) == 0 && style.Visibility(-1) == 1f, "不得提前隐藏未来线段或启用已关闭的转灰");
    Check(Math.Abs(style.Visibility(3) * style.Visibility(5) - style.Visibility(8)) < 0.00001f,
          "分帧缓存衰减必须与直接计算线段年龄一致");
    previousVisibility = visibility; previousDim = style.Dim;
    previousHistory = style.HistoryRetention; previousFade = style.FadeFrames(8);
    Console.WriteLine($"tracks={count} halfLife={style.HalfLife:F2} dim={style.Dim:F3} fade={style.FadeFrames(8)} visibleAfter12={visibility:F3}");
}
var dense = new PlaybackDensity(2000);
Check(dense.Visibility(12) >= 0.3f && dense.Visibility(40) < 0.04f,
      "密集路线应保留可辨识的短尾迹，同时在约2/3秒后消退");
Check(dense.FadeFrames(8) == 4, "密集路线的轮末转灰至少保留原请求的一半时间");
Check(new PlaybackDensity(142).HalfLife > 10f, "实际7C密集回放的尾迹半衰期应由约5帧延长至约10帧");
Console.WriteLine("PASS: sparse preservation, density monotonicity, bounds and cache/direct decay consistency");
