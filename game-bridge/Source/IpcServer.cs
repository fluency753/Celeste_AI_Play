using System;
using System.Buffers;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Threading;

namespace CelesteAI.Bridge;

public sealed record Request(long Id, string Cmd, JsonElement Args);

/// <summary>
/// 本机端口的连接口令与首行读取。端口只监听 127.0.0.1，但本机上任何程序、以及浏览器里的网页
/// （把一段数据直接发到本机端口）都连得上，所以连接要先证明自己读得到当前用户的文件：
/// 核心启动监听后生成随机口令，写进当前用户的本机应用数据目录；主控读它，连接后第一行先报口令。
/// </summary>
public static class IpcAuth {
    public const int MaxFirstLine = 8192;

    public static string Folder =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CelesteAIPlay");

    public static string FilePath(int port) => Path.Combine(Folder, $"ipc-{port}.token");

    /// <summary>生成口令并写入文件（先写临时文件再改名）。写不成就抛出：没有口令文件，谁也连不上。</summary>
    public static string Create(int port) {
        string token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant();
        Directory.CreateDirectory(Folder);
        string path = FilePath(port), temporary = path + "." + Environment.ProcessId + ".tmp";
        File.WriteAllText(temporary, token + "\n", new UTF8Encoding(false));
        File.Move(temporary, path, overwrite: true);
        return token;
    }

    /// <summary>退出时删掉自己写的口令文件（别的实例已经换成它的口令时不动）。</summary>
    public static void Remove(int port, string token) {
        try {
            string path = FilePath(port);
            if (File.Exists(path) && File.ReadAllText(path).Trim() == token) File.Delete(path);
        } catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) {
            // 留下的旧文件没有用处：下次启动会换成新口令
        }
    }

    public static bool Matches(string? expected, string? offered) {
        if (string.IsNullOrEmpty(expected) || offered == null) return false;
        return CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(offered));
    }

    /// <summary>
    /// 直接从套接字逐字节读一行（不含换行），最多 maxBytes 字节；连接断开、超时或超长时返回 null。
    /// 不经缓冲读取器，之后再给同一个流套 StreamReader 不会丢数据。
    /// </summary>
    public static string? ReadFirstLine(NetworkStream stream, int maxBytes = MaxFirstLine) {
        var buffer = new byte[Math.Min(maxBytes, 256)];
        int length = 0;
        while (true) {
            int value = stream.ReadByte();
            if (value < 0) return null;
            if (value == '\n') break;
            if (length == maxBytes) return null;
            if (length == buffer.Length) Array.Resize(ref buffer, Math.Min(maxBytes, buffer.Length * 2));
            buffer[length++] = (byte) value;
        }
        if (length > 0 && buffer[length - 1] == '\r') length--;
        return Encoding.UTF8.GetString(buffer, 0, length);
    }
}

/// <summary>
/// 只监听 127.0.0.1 的行协议服务：每行一个 JSON 对象。
/// 收到的请求放进队列，由游戏主线程在下一个节拍处理；回复经独立写线程发出，不阻塞游戏。
/// 连接后的第一行必须是 {"cmd":"auth","token":…}（口令见 IpcAuth）：第一行不是 JSON 的连接直接关掉，
/// 是 JSON 但口令不对的回一条说明再关掉。同一时间只服务一个主控连接；通过口令的新连接顶替旧连接，
/// 没通过的连接碰不到正在用的连接。
/// </summary>
public static class IpcServer {
    private const int AuthTimeoutMs = 5000;
    private const int MaxPending = 8;
    private const int RefusalDrainMs = 500;
    private const int RefusalDrainBytes = 65536;

    private static TcpListener? listener;
    private static volatile bool running;
    private static TcpClient? client;
    private static readonly object ClientLock = new();
    private sealed record Outbound(TcpClient Client, string Json);
    private static BlockingCollection<Outbound> Outbox = new(new ConcurrentQueue<Outbound>());
    private static Thread? acceptThread, writeThread;
    private static readonly ConcurrentBag<Thread> readThreads = new();
    private static readonly ConcurrentDictionary<TcpClient, byte> Pending = new();
    private static long generation;
    private static volatile string? token;
    private static int tokenPort;

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
        try {
            // Only the instance that owns the port writes the token for it.
            token = IpcAuth.Create(port);
            tokenPort = port;
        } catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or NotSupportedException or System.Security.SecurityException) {
            token = null;
            Log.Error($"cannot write the connection token {IpcAuth.FilePath(port)}: {ex.Message}; controller connections will be refused");
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
        foreach (TcpClient waiting in Pending.Keys) {
            try { waiting.Close(); } catch { /* ignored */ }
        }
        if (token is { } issued) IpcAuth.Remove(tokenPort, issued);
        token = null;
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
            if (Pending.Count >= MaxPending) {
                try { accepted.Close(); } catch { /* ignored */ }
                continue;
            }
            accepted.NoDelay = true;
            Pending[accepted] = 0;
            var thread = new Thread(() => Serve(epoch, accepted)) { IsBackground = true, Name = "CelesteAIBridge-read" };
            readThreads.Add(thread); thread.Start();
        }
    }

    private static void Serve(long epoch, TcpClient c) {
        bool accepted = false;
        try {
            accepted = Authenticate(epoch, c);
        } catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException or InvalidOperationException) {
            // 没报口令就断开或超时
        } finally {
            Pending.TryRemove(c, out _);
        }
        if (!accepted) {
            try { c.Close(); } catch { /* ignored */ }
            return;
        }
        Log.Info("orchestrator connected");
        ReadLoop(epoch, c);
    }

    /// <summary>读第一行并核对口令；通过后才顶替当前连接。</summary>
    private static bool Authenticate(long epoch, TcpClient c) {
        c.ReceiveTimeout = AuthTimeoutMs;
        c.SendTimeout = AuthTimeoutMs;
        NetworkStream stream = c.GetStream();
        string? line = IpcAuth.ReadFirstLine(stream);
        if (line == null) return false;
        long id = 0;
        string cmd = "";
        string? offered = null;
        try {
            using JsonDocument doc = JsonDocument.Parse(line);
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return false;
            if (root.TryGetProperty("id", out JsonElement idEl) && idEl.ValueKind == JsonValueKind.Number) idEl.TryGetInt64(out id);
            if (root.TryGetProperty("cmd", out JsonElement cmdEl) && cmdEl.ValueKind == JsonValueKind.String) cmd = cmdEl.GetString() ?? "";
            if (root.TryGetProperty("token", out JsonElement tokenEl) && tokenEl.ValueKind == JsonValueKind.String) offered = tokenEl.GetString();
        } catch (JsonException) {
            return false;       // 不是本协议（例如网页发来的 HTTP 请求行）：不回话，直接关
        }
        if (cmd != "auth" || !IpcAuth.Matches(token, offered)) {
            WriteLine(stream, Reply.Refused(id,
                "Authentication required: send {\"cmd\":\"auth\",\"token\":…} first, with the token from " + IpcAuth.FilePath(tokenPort) +
                ". A controller older than project 1.25.0 cannot connect to this resident core; update the controller."));
            Dismiss(c, stream);
            return false;
        }
        lock (ClientLock) {
            if (!running || epoch != Interlocked.Read(ref generation)) return false;
            // Reply before becoming the active connection, so nothing queued for it can come first.
            WriteLine(stream, Reply.Ok(id, w => w.WriteBoolean("auth", true)));
            client?.Close();
            client = c;
        }
        c.ReceiveTimeout = 0;
        c.SendTimeout = 0;
        return true;
    }

    /// <summary>
    /// 回完拒绝说明后的收尾：先关掉发送方向（对方读到说明后随即读到结束），再把对方已经发来的内容读掉一小会儿。
    /// 接收缓冲里还有没读的数据时直接关连接，系统会发复位，对方可能连刚回的那条说明都收不到（Windows 上实测如此）。
    /// </summary>
    private static void Dismiss(TcpClient c, NetworkStream stream) {
        try {
            c.Client.Shutdown(SocketShutdown.Send);
            c.ReceiveTimeout = RefusalDrainMs;
            var scratch = new byte[4096];
            long until = Environment.TickCount64 + RefusalDrainMs;
            int budget = RefusalDrainBytes;
            while (budget > 0 && Environment.TickCount64 < until) {
                int read = stream.Read(scratch, 0, scratch.Length);
                if (read <= 0) break;
                budget -= read;
            }
        } catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException) {
            // 对方已经断开，或等到了时限
        }
    }

    private static void WriteLine(NetworkStream stream, string json) {
        byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
        stream.Write(bytes, 0, bytes.Length);
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
                    long id = 0;
                    if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("id", out JsonElement idEl) &&
                            idEl.ValueKind == JsonValueKind.Number) idEl.TryGetInt64(out id);
                    string cmd = root.ValueKind == JsonValueKind.Object && root.TryGetProperty("cmd", out JsonElement cmdEl) &&
                        cmdEl.ValueKind == JsonValueKind.String ? cmdEl.GetString() ?? "" : "";
                    if (cmd == "auth") {
                        // Already authenticated on this connection: answer without involving the game thread.
                        Send(Reply.Ok(id, w => w.WriteBoolean("auth", true)));
                        continue;
                    }
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

    /// <summary>没报对口令的连接收到的唯一一条回复：{"ok":false,"error":…,"auth":"required"}。</summary>
    public static string Refused(long id, string error) => Build(id, false, error, w => w.WriteString("auth", "required"));

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
