using System;
using System.IO;
using Celeste;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// 截取游戏画面，编码为 PNG。必须在原生绘制完成、桥接叠加层绘制之前调用。
/// "level" 为加光照后的最终画面，"gameplay" 为加光照前的画面。
/// </summary>
internal static class FrameGrabber {
    internal sealed record Capture(string? Image, string? Format, string? Error = null, bool StoryHud = false);
    private static RenderTarget2D? screenTarget;
    private static Texture2D? cropped;
    private static Color[] pixels = Array.Empty<Color>();
    private static bool capturing, redirected, rendered;
    internal static bool CaptureHookAvailable { get; set; }
    internal static bool LevelCaptureHookAvailable { get; set; }

    internal static void BeginFrame(bool storyHud) {
        capturing = storyHud && CaptureHookAvailable && (Engine.Scene is not Level || LevelCaptureHookAvailable);
        redirected = rendered = false;
        if (!capturing) return;
        try {
            var device = Engine.Graphics.GraphicsDevice;
            var presentation = device.PresentationParameters;
            if (screenTarget == null || screenTarget.IsDisposed || screenTarget.Width != presentation.BackBufferWidth ||
                screenTarget.Height != presentation.BackBufferHeight) {
                screenTarget?.Dispose();
                screenTarget = new RenderTarget2D(device, presentation.BackBufferWidth, presentation.BackBufferHeight,
                    false, presentation.BackBufferFormat, presentation.DepthStencilFormat,
                    presentation.MultiSampleCount, RenderTargetUsage.PreserveContents);
            }
        } catch (Exception ex) {
            capturing = false;
            Log.Warn($"story capture target unavailable: {ex.Message}");
        }
    }

    // Replace native screen-target assignments in Engine.RenderCore and the
    // Level.Render final composition. Scene.Render executes once, unless an
    // incomplete redirect is abandoned and the frame is redrawn natively.
    internal static RenderTarget2D? ScreenTarget(RenderTarget2D? original) {
        if (!capturing || original != null) return original;
        redirected = true;
        return screenTarget;
    }

    internal static void MarkRendered() {
        if (!capturing) return;
        var targets = Engine.Graphics.GraphicsDevice.GetRenderTargets();
        rendered = redirected && targets.Length == 1 && ReferenceEquals(targets[0].RenderTarget, screenTarget);
    }

    /// <summary>
    /// The native frame left the capture target before it finished (some draw
    /// switched to the screen or another target outside the two hooked
    /// assignments), so neither surface holds the whole frame.
    /// </summary>
    internal static bool RedirectIncomplete => capturing && redirected && !rendered;

    /// <summary>Stop redirecting this frame; the caller redraws it through the native path.</summary>
    internal static void AbandonRedirect() {
        try {
            var device = Engine.Graphics.GraphicsDevice;
            device.SetRenderTarget(null);
            device.Viewport = Engine.Viewport;
        } finally {
            capturing = redirected = rendered = false;
        }
        Log.Warn("story HUD capture redirect was incomplete; frame redrawn natively without a story HUD image");
    }

    internal static void EndFrame() {
        try {
            if (!redirected) return;
            var device = Engine.Graphics.GraphicsDevice;
            device.SetRenderTarget(null);
            if (rendered && screenTarget is { IsDisposed: false } target) {
                // Present the native frame, then the bridge draws its own HUD.
                device.Viewport = new Viewport(0, 0, target.Width, target.Height);
                Draw.SpriteBatch.Begin(SpriteSortMode.Deferred, BlendState.Opaque, SamplerState.PointClamp,
                    DepthStencilState.None, RasterizerState.CullNone);
                Draw.SpriteBatch.Draw(target, Vector2.Zero, Color.White);
                Draw.SpriteBatch.End();
            }
            device.Viewport = Engine.Viewport;
        } finally {
            capturing = redirected = rendered = false;
        }
    }

    internal static void Unload() {
        EndFrame();
        screenTarget?.Dispose(); screenTarget = null;
        cropped?.Dispose(); cropped = null;
        pixels = Array.Empty<Color>();
    }

    public static Capture CapturePngBase64(string buffer, bool storyHud) {
        if (buffer != "gameplay" && storyHud) {
            if (TryCaptureStoryHud() is { } story) return story;
            // No native HUD pixels this frame (hooks unavailable, or the redirect
            // was abandoned and the frame redrawn natively). A Level's world
            // buffer is still this frame; other story scenes have none.
            if (Engine.Scene is not Level)
                return new Capture(null, null, "原生剧情界面截图暂不可用，本次不附图片，请重新观测。", StoryHud: true);
        }
        VirtualRenderTarget? source = buffer == "gameplay" ? GameplayBuffers.Gameplay : GameplayBuffers.Level;
        RenderTarget2D? target = source?.Target;
        if (target == null || target.IsDisposed) {
            return new Capture(null, null, "游戏画面缓冲尚未就绪。");
        }

        try {
            using var ms = new MemoryStream();
            target.SaveAsPng(ms, target.Width, target.Height);
            return new Capture(Convert.ToBase64String(ms.ToArray()), $"png-base64-{target.Width}x{target.Height}");
        } catch (Exception ex) {
            Log.Warn($"capture failed: {ex.Message}");
            return new Capture(null, null, "游戏截图失败，请重新观测。");
        }
    }

    private static Capture? TryCaptureStoryHud() {
        // Read a reusable render target, never the backbuffer: the latter
        // leaks FNA3D staging textures on the installed graphics backend.
        if (!capturing || !rendered || screenTarget is not { IsDisposed: false } screen) return null;
        var device = Engine.Graphics.GraphicsDevice;
        var bindings = device.GetRenderTargets();
        var viewport = device.Viewport;
        try {
            device.SetRenderTarget(null);
            var bounds = Engine.Viewport.Bounds;
            int length = checked(bounds.Width * bounds.Height);
            if (pixels.Length != length) pixels = new Color[length];
            screen.GetData(0, bounds, pixels, 0, length);
            if (cropped == null || cropped.IsDisposed || cropped.Width != bounds.Width || cropped.Height != bounds.Height) {
                cropped?.Dispose();
                cropped = new Texture2D(device, bounds.Width, bounds.Height, false, SurfaceFormat.Color);
            }
            cropped.SetData(pixels);
            using var stream = new MemoryStream();
            cropped.SaveAsPng(stream, cropped.Width, cropped.Height);
            return new Capture(Convert.ToBase64String(stream.ToArray()), $"png-base64-{cropped.Width}x{cropped.Height}", StoryHud: true);
        } catch (Exception ex) {
            Log.Warn($"story HUD capture failed: {ex.Message}");
            return Engine.Scene is Level ? null
                : new Capture(null, null, "原生剧情界面截图失败；当前没有可用的剧情图片，请重新观测。", StoryHud: true);
        } finally {
            device.SetRenderTargets(bindings);
            device.Viewport = viewport;
        }
    }
}
