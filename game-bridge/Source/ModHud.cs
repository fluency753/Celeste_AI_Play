using System;
using System.Diagnostics;
using System.IO.MemoryMappedFiles;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>
/// HUD 的 MOD 形式（主控 --hud-mod）：主控把 HUD 卡片画成图片放进共享内存（overlay/modhud.py），这里在每帧画完游戏
/// 之后把它贴在游戏显示区的右上角。章节计时不在图片里：主控给出数字的字形表，这里按当帧的游戏时间拼出来，录像里逐帧准确。
/// 画在后备缓冲上、在游戏自己的画面缓冲之后，智能体的观测截图（GameplayBuffers.Level）里没有 HUD。
/// 共享内存布局见 overlay/modhud.py；主控 5 秒没有心跳（退出了）就不画。
/// </summary>
internal static class ModHud {
    private const int Magic = 0x48494143, LayoutVersion = 1;
    private const int HeaderSize = 4096, ImageMax = 6 * 1024 * 1024, AtlasMax = 1024 * 1024;
    private const long MappingSize = HeaderSize + ImageMax + AtlasMax;
    private const int MaxGlyphs = 32;
    private const long StaleMs = 5000;

    private static MemoryMappedFile? mapping;
    private static MemoryMappedViewAccessor? view;
    private static bool openFailed;
    private static Texture2D? card, atlas;
    private static long uploadedSeq = -1;
    private static int timerX, timerY, marginRight, marginTop, glyphCount;
    private static readonly int[] Glyphs = new int[MaxGlyphs * 9];
    private static byte[] buffer = Array.Empty<byte>();
    private static bool lastVisible;
    private static readonly Stopwatch SinceIdleDraw = Stopwatch.StartNew();

    public static void Unload() {
        card?.Dispose(); card = null;
        atlas?.Dispose(); atlas = null;
        view?.Dispose(); view = null;
        mapping?.Dispose(); mapping = null;
        uploadedSeq = -1;
    }

    private static MemoryMappedViewAccessor? View {
        get {
            if (view != null || openFailed || !OperatingSystem.IsWindows()) return view;
            try {
                mapping = MemoryMappedFile.CreateOrOpen($"Local\\CelesteAIHud-{BridgeModule.Settings.Port}", MappingSize);
                view = mapping.CreateViewAccessor();
            } catch (Exception ex) {
                openFailed = true;
                Log.Warn($"in-game HUD unavailable: {ex.Message}");
            }
            return view;
        }
    }

    /// <summary>主控在发 HUD（魔数对、开着、心跳不旧）。</summary>
    private static bool Live(MemoryMappedViewAccessor v) {
        if (v.ReadInt32(0) != Magic || v.ReadInt32(4) != LayoutVersion || v.ReadInt32(16) == 0) return false;
        long beat = v.ReadInt64(2048);
        return beat > 0 && Math.Abs(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - beat) < StaleMs;
    }

    /// <summary>锁步录制时游戏停住的节拍不重画（Bridge.HaltWithoutDrawing）；HUD 有新图、或要显示/隐藏时，
    /// 最多每 0.1 秒允许重画一次，让思考期间的 HUD 也在更新。这些重画没有时钟令牌，不进录像。</summary>
    public static bool RedrawDue {
        get {
            MemoryMappedViewAccessor? v = View;
            if (v == null || SinceIdleDraw.ElapsedMilliseconds < 100) return false;
            bool visible = Live(v);
            long seq = v.ReadInt64(8);
            return visible != lastVisible || (visible && (seq & 1) == 0 && seq != uploadedSeq);
        }
    }

    public static void NoteIdleDraw() => SinceIdleDraw.Restart();

    private static readonly Stopwatch SinceReport = Stopwatch.StartNew();
    private static readonly byte[] Text = new byte[64];
    private static bool reportFailed;

    /// <summary>每个节拍（最多每 0.25 秒一次）把桥接的状态写进共享内存，控制台不用连桥接（桥接只接受一个连接，
    /// 连上会顶掉主控）就能看到：心跳、是否接管、锁步、帧号、OBS 插件、录像音频、版本、场景。</summary>
    public static void ReportStatus(bool active, bool lockstep, long frame, bool clock, bool recording) {
        if (SinceReport.ElapsedMilliseconds < 250 && SinceReport.ElapsedMilliseconds > 0) return;
        MemoryMappedViewAccessor? v = View;
        if (v == null) return;
        SinceReport.Restart();
        try {
            v.Write(2064, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
            v.Write(2072, active ? 1 : 0);
            v.Write(2076, lockstep ? 1 : 0);
            v.Write(2080, frame);
            v.Write(2088, clock ? 1 : 0);
            v.Write(2092, recording ? 1 : 0);
            WriteText(v, 2096, 32, Bridge.Version);
            string scene = Engine.Scene?.GetType().Name ?? "none";
            if (Engine.Scene is global::Celeste.Level level)
                scene = $"Level {level.Session.Area.SID} {level.Session.Area.Mode} {level.Session.Level}";
            WriteText(v, 2128, 64, scene);
        } catch (Exception ex) {
            if (!reportFailed) Log.Warn($"bridge status report failed: {ex.Message}");
            reportFailed = true;
        }
    }

    private static void WriteText(MemoryMappedViewAccessor v, long offset, int size, string text) {
        Array.Clear(Text);
        byte[] bytes = System.Text.Encoding.UTF8.GetBytes(text);
        Array.Copy(bytes, Text, Math.Min(bytes.Length, Math.Min(size, Text.Length) - 1));
        v.WriteArray(offset, Text, 0, size);
    }

    /// <summary>把共享内存里的新卡片读进纹理（顺序锁：写到一半的不要）。</summary>
    private static void Upload(MemoryMappedViewAccessor v, GraphicsDevice device) {
        long seq = v.ReadInt64(8);
        if ((seq & 1) != 0 || seq == uploadedSeq) return;
        int w = v.ReadInt32(20), h = v.ReadInt32(24), aw = v.ReadInt32(28), ah = v.ReadInt32(32);
        int n = Math.Clamp(v.ReadInt32(64), 0, MaxGlyphs);
        if (w <= 0 || h <= 0 || (long)w * h * 4 > ImageMax || aw <= 0 || ah <= 0 || (long)aw * ah * 4 > AtlasMax) return;
        int tx = v.ReadInt32(36), ty = v.ReadInt32(40), mr = v.ReadInt32(68), mt = v.ReadInt32(72);
        var glyphs = new int[n * 9];
        v.ReadArray(80, glyphs, 0, glyphs.Length);
        int cardBytes = w * h * 4, atlasBytes = aw * ah * 4;
        if (buffer.Length < cardBytes + atlasBytes) buffer = new byte[cardBytes + atlasBytes];
        v.ReadArray(HeaderSize, buffer, 0, cardBytes);
        v.ReadArray(HeaderSize + ImageMax, buffer, cardBytes, atlasBytes);
        if (v.ReadInt64(8) != seq) return; // 读的时候主控在写：下一帧再读
        if (card == null || card.Width != w || card.Height != h) { card?.Dispose(); card = new Texture2D(device, w, h); }
        if (atlas == null || atlas.Width != aw || atlas.Height != ah) { atlas?.Dispose(); atlas = new Texture2D(device, aw, ah); }
        card.SetData(buffer, 0, cardBytes);
        atlas.SetData(buffer, cardBytes, atlasBytes);
        Array.Copy(glyphs, Glyphs, glyphs.Length);
        glyphCount = n; timerX = tx; timerY = ty; marginRight = mr; marginTop = mt;
        uploadedSeq = seq;
    }

    /// <summary>游戏这一帧画完（Celeste.RenderCore 之后）调用：在游戏显示区右上角画 HUD 与章节计时。</summary>
    public static void DrawOverlay() {
        MemoryMappedViewAccessor? v = View;
        if (v == null) return;
        GraphicsDevice device = Engine.Instance.GraphicsDevice;
        Viewport game = Engine.Viewport;
        try {
            v.Write(2056, game.Width);  // 告诉主控显示区多大，卡片按它的宽度画（1080p 下 350 像素宽）
            v.Write(2060, game.Height);
            lastVisible = Live(v);
            if (!lastVisible) return;
            Upload(v, device);
            if (card == null || atlas == null) return;
            // 显示区大小刚变、主控还没按新尺寸重画时，先按比例缩放
            float scale = card.Width > 0 ? Math.Clamp(game.Width * 350f / 1920f / card.Width, 0.25f, 4f) : 1f;
            if (Math.Abs(scale - 1f) < 0.02f) scale = 1f;
            float x = game.Width - marginRight - card.Width * scale, y = marginTop;
            device.SetRenderTarget(null);
            Viewport before = device.Viewport;
            device.Viewport = game;
            Monocle.Draw.SpriteBatch.Begin(SpriteSortMode.Deferred, BlendState.AlphaBlend,
                scale == 1f ? SamplerState.PointClamp : SamplerState.LinearClamp, DepthStencilState.None, RasterizerState.CullNone);
            Monocle.Draw.SpriteBatch.Draw(card, new Vector2(MathF.Round(x), MathF.Round(y)), null, Color.White, 0f, Vector2.Zero, scale, SpriteEffects.None, 0f);
            DrawTimer(v, MathF.Round(x) + timerX * scale, MathF.Round(y) + timerY * scale, scale);
            Monocle.Draw.SpriteBatch.End();
            device.Viewport = before;
        } catch (Exception ex) {
            Log.Warn($"in-game HUD draw failed: {ex.Message}");
            Unload();
            openFailed = true;
        }
    }

    /// <summary>章节计时 H:MM:SS.mmm：已通关各章之和加上当前章节此刻的计时（当前章已通关时不重复计入）。</summary>
    private static void DrawTimer(MemoryMappedViewAccessor v, float x, float baseline, float scale) {
        bool live = v.ReadInt32(44) != 0;
        long ticks = v.ReadInt64(48);
        if (live) ticks += Engine.Scene is global::Celeste.Level level ? level.Session.Time : v.ReadInt64(56);
        long ms = Math.Max(0, ticks / TimeSpan.TicksPerMillisecond);
        string big = $"{ms / 3600000}:{ms / 60000 % 60:00}:{ms / 1000 % 60:00}";
        string small = $".{ms % 1000:000}";
        x = DrawGlyphs(big, 0, x, baseline, scale);
        DrawGlyphs(small, 1, x, baseline, scale);
    }

    private static float DrawGlyphs(string text, int size, float x, float baseline, float scale) {
        foreach (char ch in text) {
            for (int i = 0; i < glyphCount; i++) {
                int o = i * 9;
                if (Glyphs[o] != ch || Glyphs[o + 1] != size) continue;
                var source = new Rectangle(Glyphs[o + 2], Glyphs[o + 3], Glyphs[o + 4], Glyphs[o + 5]);
                var at = new Vector2(MathF.Round(x + Glyphs[o + 6] * scale), MathF.Round(baseline + Glyphs[o + 7] * scale));
                Monocle.Draw.SpriteBatch.Draw(atlas!, at, source, Color.White, 0f, Vector2.Zero, scale, SpriteEffects.None, 0f);
                x += Glyphs[o + 8] * scale;
                break;
            }
        }
        return x;
    }
}
