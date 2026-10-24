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
