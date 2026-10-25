using System;
using System.Collections.Generic;

namespace CelesteAI.Bridge;

/// <summary>与原版从大到小的 actualDepth 顺序合并；同深度保留插入次序。</summary>
internal sealed class RenderDepthQueue<T> {
    private readonly List<(double Depth, int Order, T Value)> items = new();
    private int cursor;
    public void Add(double depth, T value) => items.Add((depth,items.Count,value));
    public void Sort() => items.Sort((a,b) => a.Depth == b.Depth ? a.Order.CompareTo(b.Order) : b.Depth.CompareTo(a.Depth));
    public void Before(double depth, Action<T> draw) {
        while (cursor < items.Count && items[cursor].Depth >= depth) draw(items[cursor++].Value);
    }
    public void Finish(Action<T> draw) => Before(double.NegativeInfinity,draw);
    public void Clear() { items.Clear(); cursor = 0; }
}
