using Celeste;
using CelesteAI.Bridge;
using Microsoft.Xna.Framework;

FancyText.Char Glyph(char ch, float fade=1f) => new() {Character=ch,Fade=fade,Color=Color.White};
void Check(string expected, string actual) {
    if(expected!=actual) throw new Exception($"Expected {expected}, got {actual}");
}
var text=new FancyText.Text {Nodes=new() {
    Glyph('已'),Glyph('显'),new FancyText.NewLine(),Glyph('示'),Glyph('隐',0),Glyph('藏',0),
    new FancyText.NewPage(),Glyph('下'),Glyph('页')
}};
Check("已显\n示",StoryText.ReadPage(text,0));
Check("下页",StoryText.ReadPage(text,7));
Check("",StoryText.ReadPage(text,0,0));
Check("",StoryText.ReadPage(text,100));
Check("",StoryText.ReadPage(null,0));
text.Nodes=new() {Glyph('透')};
((FancyText.Char)text.Nodes[0]).Color=Color.Transparent;
Check("",StoryText.ReadPage(text,0));
text.Nodes=new() {Glyph('未',0.001f)};
Check("",StoryText.ReadPage(text,0));
Console.WriteLine("PASS: hidden glyphs, next page, previous page, transparency and absent text excluded");

// Bypass graphics/font construction: exercise the actual bridge reader on game types.
var memorial=(MemorialText)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(MemorialText));
memorial.Visible=true;
memorial.message="献给\n登山者";
memorial.alpha=1;
memorial.index=2.9f;
Check("献给",StoryText.ReadMemorial(memorial));
memorial.index=5;
Check("献给\n登山",StoryText.ReadMemorial(memorial));
memorial.index=100;
Check("献给\n登山者",StoryText.ReadMemorial(memorial));
memorial.Show=false; memorial.alpha=.5f;
Check("献给\n登山者",StoryText.ReadMemorial(memorial)); // fading out is still drawn
Check("",StoryText.ReadMemorial(memorial,completed:true));
memorial.alpha=0;
Check("",StoryText.ReadMemorial(memorial));
memorial.alpha=.001f;
Check("",StoryText.ReadMemorial(memorial));
memorial.alpha=1;memorial.Visible=false;
Check("",StoryText.ReadMemorial(memorial));
memorial.Visible=true;memorial.index=0;
Check("",StoryText.ReadMemorial(memorial));
Check("",StoryText.ReadMemorial(null));
memorial.message="ABCDE";memorial.index=2;memorial.Dreamy=true;memorial.timer=1;
Check("DE",StoryText.ReadMemorial(memorial)); // rendered scrambling, not the underlying AB
memorial.message="碑\U0001f3d4后";memorial.index=2;memorial.Dreamy=false;
Check("碑",StoryText.ReadMemorial(memorial));
Console.WriteLine("PASS: memorial reveal, localization, fading, hidden/completed state and dream glyphs");

// Serialize a real Level/EntityList without loading maps, fonts or graphics.
var level=(Level)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Level));
var entities=new Monocle.EntityList(level);
typeof(Monocle.Scene).GetField("<Entities>k__BackingField", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
    .SetValue(level,entities);
entities.entities.Add(memorial);
memorial.message="碑文\n后文";memorial.index=2;
System.Text.Json.JsonDocument Snapshot() {
    using var buffer=new MemoryStream();
    using (var writer=new System.Text.Json.Utf8JsonWriter(buffer)) {
        writer.WriteStartObject(); StoryText.Write(writer,level); writer.WriteEndObject();
    }
    return System.Text.Json.JsonDocument.Parse(buffer.ToArray());
}
using (var snapshot=Snapshot()) {
    var entry=snapshot.RootElement.GetProperty("story")[0];
    Check("memorial",entry.GetProperty("kind").GetString()!);
    Check("碑文",entry.GetProperty("text").GetString()!);
    if (entry.GetProperty("waitingForInput").GetBoolean()) throw new Exception("Memorial is not a dialogue prompt");
}
level.Paused=true;
using (var snapshot=Snapshot())
    if (snapshot.RootElement.GetProperty("story").GetArrayLength()!=0) throw new Exception("Paused memorial leaked");
level.Paused=false; level.Completed=true;
using (var snapshot=Snapshot())
    if (snapshot.RootElement.GetProperty("story").GetArrayLength()!=0) throw new Exception("Completed memorial leaked");
Console.WriteLine("PASS: memorial reaches story JSON only in visible gameplay state");
