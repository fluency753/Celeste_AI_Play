using System;
using System.Collections.Generic;

namespace CelesteAI.Bridge;

/// <summary>只累计有新线段绘制的时间；并行路线不重复计时，间隙与轮末停留不计时。</summary>
internal sealed class PlaybackTrailClock {
    private readonly List<(float Start, float End)> intervals = new();
    private readonly float[] prefix;

    public PlaybackTrailClock(List<(float Start, float End)> spans) {
        spans.Sort((a,b) => a.Start.CompareTo(b.Start));
        foreach (var span in spans) {
            if (span.End <= span.Start) continue;
            if (intervals.Count == 0 || span.Start > intervals[^1].End) intervals.Add(span);
            else intervals[^1] = (intervals[^1].Start, Math.Max(intervals[^1].End,span.End));
        }
        prefix = new float[intervals.Count + 1];
        for (int i = 0; i < intervals.Count; i++) prefix[i+1] = prefix[i]+intervals[i].End-intervals[i].Start;
    }

    public float Age(float from, float to) {
        return to <= from ? 0 : Math.Max(0,At(to)-At(from));
    }

    private float At(float time) {
        int left = 0, right = intervals.Count;
        while (left < right) {
            int middle = (left+right)/2;
            if (intervals[middle].Start <= time) left = middle+1;
            else right = middle;
        }
        if (left == 0) return 0;
        var span = intervals[left-1];
        return prefix[left-1]+Math.Min(time-span.Start,span.End-span.Start);
    }
}
