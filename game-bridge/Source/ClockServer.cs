using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace CelesteAI.Bridge;

/// <summary>
/// 锁步时钟：只给 OBS 插件用的第二个本机端口（默认 47776）。
/// 录制时插件每个视频节拍发来一个令牌 "T id"，桥接推进一帧，等这一帧呈现到屏幕后回 "F id 帧号 章节计时"；
/// 没有要推进的帧时回 "N id"。插件用 "L 1" / "L 0" 开关锁步，用 "S {json}" 报告录制状态；
/// 桥接用 "R {json}" 把主控的录制指令转给插件。
/// </summary>
internal static class ClockServer {
    private static TcpListener? listener;
    private static volatile bool running;
    private static TcpClient? client;
    private static readonly object ClientLock = new();
    private static readonly ConcurrentQueue<long> Tokens = new();
    private static readonly SemaphoreSlim TokenSignal = new(0);

    public static volatile bool LockstepRequested;
    public static volatile string? PluginStatus;

    public static bool Connected {
        get { lock (ClientLock) return client != null; }
    }

    public static void Start(int port) {
        try {
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
        } catch (SocketException ex) {
            Log.Error($"clock: cannot listen on 127.0.0.1:{port}: {ex.Message}");
            return;
        }
        running = true;
        new Thread(AcceptLoop) { IsBackground = true, Name = "CelesteAIBridge-clock" }.Start();
        Log.Info($"clock listening on 127.0.0.1:{port}");
    }

    public static void Stop() {
        running = false;
        try { listener?.Stop(); } catch { /* ignored */ }
        Drop(null);
    }

    /// <summary>取一个令牌；没有时最多等 waitMs 毫秒。</summary>
    public static bool TryTakeToken(int waitMs, out long id) {
        if (Tokens.TryDequeue(out id)) return true;
        if (waitMs <= 0) return false;
        long deadline = Environment.TickCount64 + waitMs;
        while (true) {
            int left = (int) (deadline - Environment.TickCount64);
            if (left <= 0 || !TokenSignal.Wait(left)) return Tokens.TryDequeue(out id);
            if (Tokens.TryDequeue(out id)) return true;
        }
    }

    public static void Send(string line) {
        TcpClient? c;
        lock (ClientLock) c = client;
        if (c == null) return;
        try {
            byte[] bytes = Encoding.UTF8.GetBytes(line + "\n");
            lock (c) c.GetStream().Write(bytes, 0, bytes.Length);
        } catch (Exception) {
            Drop(c);
        }
    }

    private static void Drop(TcpClient? which) {
        lock (ClientLock) {
            if (which != null && client != which) return;
            try { client?.Close(); } catch { /* ignored */ }
            client = null;
        }
        LockstepRequested = false;
        while (Tokens.TryDequeue(out _)) { }
    }

    private static void AcceptLoop() {
        while (running) {
            TcpClient accepted;
            try {
                accepted = listener!.AcceptTcpClient();
            } catch (Exception) {
                if (!running) return;
                continue;
            }
            accepted.NoDelay = true;
            Drop(null);
            lock (ClientLock) client = accepted;
            Log.Info("clock: OBS plugin connected");
            new Thread(() => ReadLoop(accepted)) { IsBackground = true, Name = "CelesteAIBridge-clock-read" }.Start();
        }
    }

    private static void ReadLoop(TcpClient c) {
        try {
            using var reader = new StreamReader(c.GetStream(), new UTF8Encoding(false));
            string? line;
            while (running && (line = reader.ReadLine()) != null) {
                if (line.Length < 1) continue;
                switch (line[0]) {
                    case 'T':
                        if (long.TryParse(line.AsSpan(2), out long id)) {
                            Tokens.Enqueue(id);
                            TokenSignal.Release();
                        }
                        break;
                    case 'L':
                        LockstepRequested = line.Length > 2 && line[2] == '1';
                        Log.Info($"clock: lockstep {(LockstepRequested ? "on" : "off")}");
                        break;
                    case 'S':
                        PluginStatus = line.Length > 2 ? line[2..] : null;
                        break;
                }
            }
        } catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException) {
            // 断开
        }
        Drop(c);
        Log.Info("clock: OBS plugin disconnected");
    }
}
