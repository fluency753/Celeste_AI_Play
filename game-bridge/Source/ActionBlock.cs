using System.Text.Json;

namespace CelesteAI.Bridge;

internal sealed record ActionBlock(string Stop, string RuleId, string Action, string Reason,
    object? Expected = null, object? Actual = null) {
    public void Write(Utf8JsonWriter w) => JsonSerializer.Serialize(w, new {
        rule_id = RuleId, action = Action, reason = Reason, expected = Expected, actual = Actual,
    });
}
