namespace CelesteAI.Bridge;
// 只替换外部边界：日志与按深度合并的绘制通道（记下排进来的深度）；补画清单本身直接执行。
internal static class Log { public static void Info(string message) {} }
internal static class PlaybackVisualPass {
    public static readonly List<double> Depths = new();
    public static void Add(double depth, Action<int> draw) => Depths.Add(depth);
}
