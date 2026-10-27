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

public sealed record Request(long Id, string Cmd, JsonElement Args);

/// <summary>
/// 只监听 127.0.0.1 的行协议服务：每行一个 JSON 对象。
/// 收到的请求放进队列，由游戏主线程在下一个节拍处理；回复经独立写线程发出，不阻塞游戏。
/// 同一时间只服务一个主控连接，新连接会顶替旧连接。
/// </summary>
public static class IpcServer {
    private static TcpListener? listener;
    private static volatile bool running;
    private static TcpClient? client;
    private static readonly object ClientLock = new();
    private sealed record Outbound(TcpClient Client, string Json);
    private static BlockingCollection<Outbound> Outbox = new(new ConcurrentQueue<Outbound>());
    private static Thread? acceptThread, writeThread;
    private static readonly ConcurrentBag<Thread> readThreads = new();
    private static long generation;

    public static void Start(int port) {
        if (running) throw new InvalidOperationException("IPC server is already running");
        long epoch = Interlocked.Increment(ref generation);
        Outbox = new(new ConcurrentQueue<Outbound>());
        try {
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
        } catch (SocketException ex) {
            Log.Error($"cannot listen on 127.0.0.1:{port}: {ex.Message}");
            return;
        }
        running = true;
        var output = Outbox;
        var server = listener;
        acceptThread = new Thread(() => AcceptLoop(epoch, server!)) { IsBackground = true, Name = "CelesteAIBridge-accept" };
        writeThread = new Thread(() => WriteLoop(epoch, output)) { IsBackground = true, Name = "CelesteAIBridge-write" };
        acceptThread.Start(); writeThread.Start();
    }

    public static void Stop() {
        running = false;
        Interlocked.Increment(ref generation);
        try { listener?.Stop(); } catch { /* ignored */ }
        lock (ClientLock) {
            client?.Close();
            client = null;
        }
        Outbox.CompleteAdding();
        Join(acceptThread); Join(writeThread);
        while (readThreads.TryTake(out Thread? reader)) Join(reader);
        acceptThread = writeThread = null;
        listener = null;
    }

    public static void Send(string json) {
        TcpClient? destination;
        lock (ClientLock) destination = client;
        if (!running || destination == null) return;
        try { Outbox.Add(new(destination, json)); }
        catch (InvalidOperationException) { /* Server stopped between checking and enqueueing. */ }
    }

    private static void Join(Thread? thread) {
        if (thread != null && thread != Thread.CurrentThread && !thread.Join(2000))
            Log.Warn("IPC worker did not stop within two seconds: " + thread.Name);
    }

    private static void AcceptLoop(long epoch, TcpListener server) {
        while (running && epoch == Interlocked.Read(ref generation)) {
            TcpClient accepted;
            try {
                accepted = server.AcceptTcpClient();
            } catch (Exception) {
                if (!running || epoch != Interlocked.Read(ref generation)) return;
                continue;
            }
            accepted.NoDelay = true;
            lock (ClientLock) {
                if (!running || epoch != Interlocked.Read(ref generation)) { accepted.Close(); return; }
                client?.Close();
                client = accepted;
            }
            Log.Info("orchestrator connected");
            var thread = new Thread(() => ReadLoop(epoch, accepted)) { IsBackground = true, Name = "CelesteAIBridge-read" };
            readThreads.Add(thread); thread.Start();
        }
    }

    private static void ReadLoop(long epoch, TcpClient c) {
        try {
            using var reader = new StreamReader(c.GetStream(), new UTF8Encoding(false));
            string? line;
            while (running && epoch == Interlocked.Read(ref generation) && (line = reader.ReadLine()) != null) {
                lock (ClientLock) if (!ReferenceEquals(client, c)) break;
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
                BridgeRuntime.Enqueue(req);
            }
        } catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException) {
            // 连接断开
        }
        Log.Info("orchestrator disconnected");
    }

    private static void WriteLoop(long epoch, BlockingCollection<Outbound> output) {
        foreach (Outbound message in output.GetConsumingEnumerable()) {
            if (!running || epoch != Interlocked.Read(ref generation)) break;
            TcpClient? c;
            lock (ClientLock) c = client;
            if (c == null || !ReferenceEquals(c, message.Client)) continue;
            try {
                byte[] bytes = Encoding.UTF8.GetBytes(message.Json + "\n");
                c.GetStream().Write(bytes, 0, bytes.Length);
            } catch (Exception) {
                // 主控已断开，丢弃
            }
        }
    }
}

/// <summary>构造回复：{"id":…,"ok":true|false, "error"?:…, …}</summary>
public static class Reply {
    private static readonly JsonWriterOptions Options = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static string Ok(long id, Action<Utf8JsonWriter>? body = null) => Build(id, true, null, body);

    public static string Error(long id, string error) => Build(id, false, error, null);

    /// <summary>主动推送的事件（不是某个请求的回复）：{"event": name, …}。</summary>
    public static string Event(string name, Action<Utf8JsonWriter> body) {
        var buffer = new ArrayBufferWriter<byte>();
        using (var w = new Utf8JsonWriter(buffer, Options)) {
            w.WriteStartObject();
            w.WriteString("event", name);
            body(w);
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

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
