using System.Text.Json;
namespace CelesteAI.Bridge;
// 只替换外部I/O边界；真实Start/Step/Finish/EndOverlay状态机直接执行。
internal static class Bridge { public static bool CapturePending => false; public static long Frame => 0; }
internal static class Log { public static void Info(string message) {} }
internal static class IpcServer { public static void Send(string message) {} }
internal static class Reply {
    public static string Ok(long id, Action<Utf8JsonWriter> body) {
        using var stream = new MemoryStream();
        using(var writer = new Utf8JsonWriter(stream)) { writer.WriteStartObject(); body(writer); writer.WriteEndObject(); }
        return System.Text.Encoding.UTF8.GetString(stream.ToArray());
    }
}
