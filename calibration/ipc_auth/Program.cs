// 连接口令与首行校验的独立检查（不需要游戏）：在本进程里启动常驻核心的两个端口服务，
// 用真实的本机连接逐项核对。全部通过时输出 PASS 并返回 0。
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace CelesteAI.Bridge;

public static class Log {
    public static readonly List<string> Lines = new();
    public static void Info(string text) { lock (Lines) Lines.Add("INFO " + text); }
    public static void Warn(string text) { lock (Lines) Lines.Add("WARN " + text); }
    public static void Error(string text) { lock (Lines) Lines.Add("ERROR " + text); }
}

/// <summary>Stand-in for the game thread: echo every forwarded request.</summary>
public static class BridgeRuntime {
    public static bool HoldingReload => false;
    public static readonly List<string> Seen = new();
    public static void Enqueue(Request request) {
        lock (Seen) Seen.Add(request.Cmd);
        IpcServer.Send(Reply.Ok(request.Id, w => w.WriteString("echo", request.Cmd)));
    }
}

public static class Program {
    private static int failures;

    private static void Check(bool condition, string what) {
        if (condition) return;
        failures++;
        Console.WriteLine("FAIL " + what);
    }

    private static int FreePort() {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        int port = ((IPEndPoint) probe.LocalEndpoint).Port;
        probe.Stop();
        return port;
    }

    private sealed class Wire : IDisposable {
        public readonly TcpClient Client = new();
        private readonly NetworkStream stream;
        public Wire(int port) {
            Client.Connect(IPAddress.Loopback, port);
            Client.NoDelay = true;
            Client.ReceiveTimeout = 3000;
            stream = Client.GetStream();
        }
        public void Send(string text) {
            byte[] bytes = Encoding.UTF8.GetBytes(text);
            stream.Write(bytes, 0, bytes.Length);
        }
        /// <summary>One line, or null when the peer closed the connection or nothing arrived in time.</summary>
        public string? Line(int timeoutMs = 3000) {
            Client.ReceiveTimeout = timeoutMs;
            var bytes = new List<byte>();
            try {
                while (true) {
                    int value = stream.ReadByte();
                    if (value < 0) return null;
                    if (value == '\n') return Encoding.UTF8.GetString(bytes.ToArray());
                    bytes.Add((byte) value);
                }
            } catch (IOException) {
                return null;
            }
        }
        /// <summary>True when the peer closed the connection (EOF or reset) within the timeout.</summary>
        public bool Closed(int timeoutMs = 3000) {
            Client.ReceiveTimeout = timeoutMs;
            try {
                return stream.ReadByte() < 0;
            } catch (IOException ex) when (ex.InnerException is SocketException socket && socket.SocketErrorCode == SocketError.TimedOut) {
                return false;
            } catch (IOException) {
                return true;
            }
        }
        public void Dispose() => Client.Dispose();
    }

    private static JsonElement Json(string? line) {
        using JsonDocument document = JsonDocument.Parse(line ?? "{}");
        return document.RootElement.Clone();
    }

    private static bool Wait(Func<bool> condition, int timeoutMs = 3000) {
        long until = Environment.TickCount64 + timeoutMs;
        while (!condition()) {
            if (Environment.TickCount64 > until) return false;
            Thread.Sleep(10);
        }
        return true;
    }

    private static int SeenCount() { lock (BridgeRuntime.Seen) return BridgeRuntime.Seen.Count; }

    public static int Main() {
        int ipcPort = FreePort(), clockPort = FreePort();
        IpcServer.Start(ipcPort);
        ClockServer.Start(clockPort);
        string tokenFile = IpcAuth.FilePath(ipcPort);
        try {
            Check(File.Exists(tokenFile), "token file was not written: " + tokenFile);
            string token = File.Exists(tokenFile) ? File.ReadAllText(tokenFile).Trim() : "";
            Check(token.Length == 64 && token.TrimStart("0123456789abcdef".ToCharArray()).Length == 0, "token is not 64 lowercase hex characters");
            string auth = "{\"id\":7,\"cmd\":\"auth\",\"token\":\"" + token + "\"}\n";

            // 1. A web page posting to the port: request line first, commands in the body.
            using (var page = new Wire(ipcPort)) {
                page.Send("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\n\r\n" +
                          "{\"id\":1,\"cmd\":\"record\",\"action\":\"start\",\"ffmpeg\":\"calc.exe\"}\n" + auth + "{\"id\":2,\"cmd\":\"step\"}\n");
                Check(page.Closed(), "an HTTP request was not disconnected");
            }
            Check(SeenCount() == 0, "a command smuggled in an HTTP body reached the game thread");

            // 2. A client that speaks the protocol but does not present the token.
            using (var old = new Wire(ipcPort)) {
                old.Send("{\"id\":3,\"cmd\":\"hello\"}\n");
                JsonElement refused = Json(old.Line());
                Check(refused.TryGetProperty("ok", out var ok) && !ok.GetBoolean(), "unauthenticated hello was not refused");
                Check(refused.TryGetProperty("auth", out var need) && need.GetString() == "required", "refusal does not say auth is required");
                Check(refused.GetProperty("id").GetInt64() == 3, "refusal does not carry the request id");
                Check(old.Closed(), "refused connection stayed open");
            }
            using (var wrong = new Wire(ipcPort)) {
                wrong.Send("{\"id\":4,\"cmd\":\"auth\",\"token\":\"" + new string('0', 64) + "\"}\n{\"id\":5,\"cmd\":\"step\"}\n");
                // More lines were already sent behind the refused one: the explanation must still arrive (not be lost to a reset).
                JsonElement refused = Json(wrong.Line());
                Check(refused.TryGetProperty("auth", out var need) && need.GetString() == "required", "wrong token was not refused with an explanation");
                Check(wrong.Closed(), "wrong-token connection stayed open");
            }
            using (var flood = new Wire(ipcPort)) {
                // A refused client that keeps sending is only read for a bounded amount, then dropped (its reply may be lost).
                try {
                    flood.Send("{\"id\":6,\"cmd\":\"hello\"}\n" + new string('y', 300000));
                } catch (IOException) {
                    // Dropped while still sending.
                }
                flood.Line();
                Check(flood.Closed(), "a refused client that keeps sending was not disconnected");
            }
            using (var notObject = new Wire(ipcPort)) {
                notObject.Send("[1,2,3]\n");
                Check(notObject.Closed(), "a non-object first line was not disconnected");
            }
            using (var huge = new Wire(ipcPort)) {
                huge.Send(new string('x', IpcAuth.MaxFirstLine + 100));
                Check(huge.Closed(), "an over-long first line was not disconnected");
            }
            Check(SeenCount() == 0, "a refused connection reached the game thread");

            // 3. The controller: token first, then ordinary commands.
            using var controller = new Wire(ipcPort);
            controller.Send(auth);
            JsonElement accepted = Json(controller.Line());
            Check(accepted.GetProperty("ok").GetBoolean() && accepted.GetProperty("auth").GetBoolean() &&
                  accepted.GetProperty("id").GetInt64() == 7, "correct token was not accepted");
            controller.Send("{\"id\":8,\"cmd\":\"hello\"}\n");
            Check(Json(controller.Line()).GetProperty("echo").GetString() == "hello", "authenticated command did not reach the game thread");
            controller.Send("{\"id\":9,\"cmd\":\"auth\",\"token\":\"again\"}\n");
            Check(Json(controller.Line()).GetProperty("auth").GetBoolean(), "repeated auth on an accepted connection was not answered");
            controller.Send("not json\n{\"id\":10,\"cmd\":\"observe\"}\n");
            Check(Json(controller.Line()).GetProperty("error").GetString()!.StartsWith("bad json"), "bad json after auth should be reported, not fatal");
            Check(Json(controller.Line()).GetProperty("echo").GetString() == "observe", "connection did not survive a bad line after auth");

            // 4. Strangers do not displace the controller; another controller does.
            using (var page = new Wire(ipcPort)) {
                page.Send("GET / HTTP/1.1\r\n\r\n");
                Check(page.Closed(), "second HTTP request was not disconnected");
            }
            var silent = new List<Wire>();
            for (int i = 0; i < 12; i++) silent.Add(new Wire(ipcPort));     // more than the pending limit
            controller.Send("{\"id\":11,\"cmd\":\"observe\"}\n");
            Check(Json(controller.Line()).GetProperty("id").GetInt64() == 11, "controller was displaced by unauthenticated connections");
            Check(silent[11].Closed(1000), "connections beyond the pending limit were not closed");
            Check(silent[0].Closed(8000), "a silent connection was not closed after the auth timeout");
            foreach (Wire wire in silent) wire.Dispose();
            using (var successor = new Wire(ipcPort)) {
                successor.Send(auth + "{\"id\":12,\"cmd\":\"hello\"}\n");
                Check(Json(successor.Line()).GetProperty("auth").GetBoolean(), "second controller was not accepted");
                Check(Json(successor.Line()).GetProperty("id").GetInt64() == 12, "second controller's command was lost");
                Check(controller.Closed(), "first controller was not closed when the second one authenticated");
            }

            // 5. Clock port: only a connection that speaks the clock protocol becomes the plugin.
            using (var page = new Wire(clockPort)) {
                page.Send("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\nT\nL 1\nT 99\n");
                Check(page.Closed(), "an HTTP request to the clock port was not disconnected");
            }
            Check(!ClockServer.Connected && !ClockServer.LockstepRequested && !ClockServer.TryTakeToken(0, out _),
                  "an HTTP body changed clock state");
            using var plugin = new Wire(clockPort);
            plugin.Send("S {\"state\":\"idle\"}\n");
            Check(Wait(() => ClockServer.Connected && ClockServer.PluginStatus == "{\"state\":\"idle\"}"), "plugin status line was not accepted");
            plugin.Send("L 1\nT\nT x\nT 5\n");
            Check(Wait(() => ClockServer.LockstepRequested), "lockstep request was lost");
            Check(ClockServer.TryTakeToken(1000, out long first) && first == 5, "token after malformed token lines was lost");
            using (var page = new Wire(clockPort)) {
                page.Send("GET /hud HTTP/1.1\r\n\r\n");
                Check(page.Closed(), "second HTTP request to the clock port was not disconnected");
            }
            using (var quiet = new Wire(clockPort)) {
                Thread.Sleep(200);
                Check(ClockServer.Connected && ClockServer.LockstepRequested, "a silent connection displaced the plugin");
                plugin.Send("T 6\n");
                Check(ClockServer.TryTakeToken(1000, out long second) && second == 6, "plugin stopped working after strangers connected");
                ClockServer.Send("R {\"action\":\"stop\"}");
                Check(plugin.Line() == "R {\"action\":\"stop\"}", "command for the plugin did not arrive");
            }
            using (var restarted = new Wire(clockPort)) {
                restarted.Send("L 0\n");
                Check(plugin.Closed(), "old plugin connection was not replaced by a new plugin connection");
                Check(Wait(() => ClockServer.Connected && !ClockServer.LockstepRequested), "new plugin connection did not take over");
            }
        } finally {
            IpcServer.Stop();
            ClockServer.Stop();
        }
        Check(!File.Exists(tokenFile), "token file was left behind after stop");
        if (failures > 0) {
            lock (Log.Lines) foreach (string line in Log.Lines) Console.WriteLine(line);
            Console.WriteLine($"FAILED ({failures})");
            return 1;
        }
        Console.WriteLine("PASS");
        return 0;
    }
}
