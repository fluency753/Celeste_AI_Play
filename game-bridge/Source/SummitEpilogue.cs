using System.Runtime.CompilerServices;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

internal static class SummitEpilogue {
    internal const string Summit = "Celeste/7-Summit";
    internal const string Epilogue = "Celeste/8-Epilogue";

    internal sealed record Finish(int Slot, long Ticks, bool Valid, bool Recorded);
    private static readonly ConditionalWeakTable<Session, Finish> Finishes = new();

    internal static Finish? FromCredits(Level? source, Session target, bool fromSave) {
        if (fromSave || source is not { Completed: true } || source != Engine.Scene ||
                source.Session is not { Area.Mode: AreaMode.Normal } session ||
                session.Area.SID != Summit || target.Area.Mode != AreaMode.Normal || target.Area.SID != Epilogue ||
                SaveData.Instance is not { } save)
            return null;
        return new(save.FileSlot, session.Time, session.StartedFromBeginning, Bridge.RecordedFromChapterStart(session));
    }

    internal static void Remember(Session target, Finish finish) => Finishes.AddOrUpdate(target, finish);

    internal static void Write(Utf8JsonWriter writer, Scene? scene) {
        Session? session = scene switch {
            LevelEnter enter => enter.session,
            Level level => level.Session,
            _ => null,
        };
        if (session == null || !Finishes.TryGetValue(session, out var finish) ||
                session.Area.SID != Epilogue || session.Area.Mode != AreaMode.Normal ||
                SaveData.Instance?.FileSlot != finish.Slot)
            return;
        writer.WriteStartObject("summitEpilogue");
        writer.WriteString("sid", Summit);
        writer.WriteString("side", "A");
        writer.WriteNumber("chapterTimeTicks", finish.Ticks);
        writer.WriteBoolean("chapterTimeValid", finish.Valid);
        writer.WriteBoolean("chapterRecordedFromStart", finish.Recorded);
        writer.WriteEndObject();
    }
}
