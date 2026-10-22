using System;
using System.Buffers;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Threading;

namespace CelesteAI.Bridge;

internal sealed record Request(long Id, string Cmd, JsonElement Args);

/// <summary>
/// 只监听 127.0.0.1 的行协议服务：每行一个 JSON 对象。
/// 收到的请求放进队列，由游戏主线程在下一个节拍处理；回复经独立写线程发出，不阻塞游戏。
/// 同一时间只服务一个主控连接，新连接会顶替旧连接。
/// </summary>
internal static class IpcServer {
    private static TcpListener? listener;
    private static volatile bool running;
    private static TcpClient? client;
    private static readonly object ClientLock = new();
    private static readonly BlockingCollection<string> Outbox = new(new ConcurrentQueue<string>());

    public static void Start(int port) {
        try {
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
        } catch (SocketException ex) {
            Log.Error($"cannot listen on 127.0.0.1:{port}: {ex.Message}");
            return;
        }
        running = true;
        new Thread(AcceptLoop) { IsBackground = true, Name = "CelesteAIBridge-accept" }.Start();
        new Thread(WriteLoop) { IsBackground = true, Name = "CelesteAIBridge-write" }.Start();
    }

    public static void Stop() {
        running = false;
        try { listener?.Stop(); } catch { /* ignored */ }
        lock (ClientLock) {
            client?.Close();
            client = null;
        }
    }

    public static void Send(string json) {
        if (running) {
            Outbox.Add(json);
        }
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
            lock (ClientLock) {
                client?.Close();
                client = accepted;
            }
            Log.Info("orchestrator connected");
            new Thread(() => ReadLoop(accepted)) { IsBackground = true, Name = "CelesteAIBridge-read" }.Start();
        }
    }

    private static void ReadLoop(TcpClient c) {
        try {
            using var reader = new StreamReader(c.GetStream(), new UTF8Encoding(false));
            string? line;
            while (running && (line = reader.ReadLine()) != null) {
                if (string.IsNullOrWhiteSpace(line)) continue;
                Request req;
                try {
                    using JsonDocument doc = JsonDocument.Parse(line);
                    JsonElement root = doc.RootElement;
                    long id = root.TryGetProperty("id", out JsonElement idEl) && idEl.ValueKind == JsonValueKind.Number ? idEl.GetInt64() : 0;
                    string cmd = root.TryGetProperty("cmd", out JsonElement cmdEl) ? cmdEl.GetString() ?? "" : "";
                    req = new Request(id, cmd, root.Clone());
                } catch (JsonException ex) {
                    Send(Reply.Error(0, $"bad json: {ex.Message}"));
                    continue;
                }
                Bridge.Enqueue(req);
            }
        } catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException) {
            // 连接断开
        }
        Log.Info("orchestrator disconnected");
    }

    private static void WriteLoop() {
        foreach (string message in Outbox.GetConsumingEnumerable()) {
            TcpClient? c;
            lock (ClientLock) c = client;
            if (c == null) continue;
            try {
                byte[] bytes = Encoding.UTF8.GetBytes(message + "\n");
                c.GetStream().Write(bytes, 0, bytes.Length);
            } catch (Exception) {
                // 主控已断开，丢弃
            }
        }
    }
}

/// <summary>构造回复：{"id":…,"ok":true|false, "error"?:…, …}</summary>
internal static class Reply {
    private static readonly JsonWriterOptions Options = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static string Ok(long id, Action<Utf8JsonWriter>? body = null) => Build(id, true, null, body);

    public static string Error(long id, string error) => Build(id, false, error, null);

    private static string Build(long id, bool ok, string? error, Action<Utf8JsonWriter>? body) {
        var buffer = new ArrayBufferWriter<byte>();
        using (var w = new Utf8JsonWriter(buffer, Options)) {
            w.WriteStartObject();
            w.WriteNumber("id", id);
            w.WriteBoolean("ok", ok);
            if (error != null) w.WriteString("error", error);
            body?.Invoke(w);
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    /// <summary>把一段内容先序列化成 JSON 对象，稍后整体嵌入回复。</summary>
    public static byte[] Object(Action<Utf8JsonWriter> fill) {
        var buffer = new ArrayBufferWriter<byte>();
        using (var w = new Utf8JsonWriter(buffer, Options)) {
            w.WriteStartObject();
            fill(w);
            w.WriteEndObject();
        }
        return buffer.WrittenSpan.ToArray();
    }
}
