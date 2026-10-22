using System;
using System.IO;
using Celeste;
using Microsoft.Xna.Framework.Graphics;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 截取 320×180 的游戏画面层（不含 HUD），编码为 PNG。必须在渲染线程、绘制完成后调用。
/// "level" 为加光照后的最终画面，"gameplay" 为加光照前的画面。
/// </summary>
internal static class FrameGrabber {
    public static string? CapturePngBase64(string buffer) {
        VirtualRenderTarget? source = buffer == "gameplay" ? GameplayBuffers.Gameplay : GameplayBuffers.Level;
        RenderTarget2D? target = source?.Target;
        if (target == null || target.IsDisposed) {
            return null;
        }

        try {
            using var ms = new MemoryStream();
            target.SaveAsPng(ms, target.Width, target.Height);
            return Convert.ToBase64String(ms.ToArray());
        } catch (Exception ex) {
            Log.Warn($"capture failed: {ex.Message}");
            return null;
        }
    }
}
