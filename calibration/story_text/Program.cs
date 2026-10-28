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

// A cutscene flag is not a loss of player input: the prologue starts this way.
level.Completed=false;
var player=(Player)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Player));
player.StateMachine=new Monocle.StateMachine(32);
level.InCutscene=true;
if (!PlayerControl.IsControllable(level,player)) throw new Exception("Controllable tutorial intro was blocked");
player.StateMachine.State=Player.StDummy;
if (PlayerControl.IsControllable(level,player)) throw new Exception("Dummy animation is not player control");
entities.entities.Clear();
var tutorial=(BirdTutorialGui)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(BirdTutorialGui));
tutorial.Visible=true;tutorial.Open=true;tutorial.Scale=.5f;tutorial.controls=new() { "Dash" };
entities.entities.Add(tutorial);
entities.entities.Add(player);
Check("",StoryText.InputNode(level) ?? "");
tutorial.Scale=1;
Check("tutorial",StoryText.InputNode(level)!);
level.InCutscene=false;
player.StateMachine.State=Player.StIntroRespawn;
Check("",StoryText.InputNode(level) ?? ""); // an ambient bird (1A climbing) during respawn is not an input node
player.StateMachine.State=Player.StDummy;
Check("tutorial",StoryText.InputNode(level)!); // a dummy waiting on the tutorial still is
level.InCutscene=true;
player.StateMachine.State=Player.StNormal;
Check("",StoryText.InputNode(level) ?? ""); // a hint never blocks otherwise normal movement
player.StateMachine.State=Player.StDummy;
tutorial.Open=false;
Check("",StoryText.InputNode(level) ?? "");
entities.entities.Clear();
var box=(Textbox)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Textbox));
box.Visible=true;box.ease=1;box.waitingForInput=false;
entities.entities.Add(box);
player.StateMachine.State=Player.StNormal;
if (!PlayerControl.IsControllable(level,player)) throw new Exception("Text appearance must not override actual movement state");
Check("",StoryText.InputNode(level) ?? "");
box.waitingForInput=true;
box.runRoutine=new Monocle.Coroutine();
box.runRoutine.waitTimer=.1f;
Check("",StoryText.InputNode(level) ?? ""); // visible page, but confirmation is still debounced
box.runRoutine.waitTimer=0f;
Check("dialogue",StoryText.InputNode(level)!);
box.disableInput=true;
Check("",StoryText.InputNode(level) ?? "");
box.disableInput=false;
level.Paused=true;
Check("",StoryText.InputNode(level) ?? "");
level.Paused=false;
var enter=(LevelEnter)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(LevelEnter));
var card=(Postcard)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Postcard));
enter.postcard=card;card.Visible=true;card.alpha=1;card.buttonEase=0;
var initialEnterEntities=new Monocle.EntityList(enter);
typeof(Monocle.Scene).GetField("<Entities>k__BackingField",System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
    .SetValue(enter,initialEnterEntities);
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(card,enter);
var initialCardOwner=new Monocle.Entity();
initialCardOwner.Add(new Monocle.Coroutine(card.DisplayRoutine()));initialEnterEntities.entities.Add(initialCardOwner);
Check("",StoryText.InputNode(enter) ?? "");
card.buttonEase=1;
Check("postcard",StoryText.InputNode(enter)!);
card.alpha=.5f;
Check("",StoryText.InputNode(enter) ?? "");
Console.WriteLine("PASS: player control during cutscenes and real dialogue/tutorial input nodes");

// Menus must keep animating and accept physical input; chapter results stay frozen.
foreach (Type type in new[] { typeof(Level), typeof(AreaComplete), typeof(IntroVignette), typeof(LevelEnter) }) {
    var scene=(Monocle.Scene)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(type);
    if (!SceneControl.FreezeWhenIdle(scene) || !SceneControl.OwnInput(true,scene,false)) throw new Exception("Chapter scene released: "+type.Name);
    if (SceneControl.OwnInput(false,scene,true)) throw new Exception("Inactive bridge captured input");
}
foreach (Type type in new[] { typeof(Overworld), typeof(LevelLoader), typeof(LevelExit), typeof(GameLoader), typeof(OverworldLoader), typeof(Monocle.Scene) }) {
    var scene=(Monocle.Scene)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(type);
    if (SceneControl.FreezeWhenIdle(scene) || SceneControl.OwnInput(true,scene,false)) throw new Exception("Idle menu captured: "+type.Name);
    if (!SceneControl.OwnInput(true,scene,true)) throw new Exception("Explicit menu step lost input");
}
Console.WriteLine("PASS: menus run freely, explicit steps own input, chapter/story/results remain controlled");

var menuWorld=(Overworld)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Overworld));
var select=(OuiChapterSelect)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(OuiChapterSelect));
menuWorld.Current=select;select.Focused=true;
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(select,menuWorld);
if(!SceneControl.FreezeWhenIdle(menuWorld) || !SceneControl.OwnInput(true,menuWorld,false)) throw new Exception("Ready chapter select was not frozen");
select.Focused=false;
if(SceneControl.FreezeWhenIdle(menuWorld)) throw new Exception("Entering chapter select animation froze");
select.Focused=true;
var panel=(OuiChapterPanel)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(OuiChapterPanel));
menuWorld.Current=panel;panel.Focused=true;
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(panel,menuWorld);
if(!SceneControl.FreezeWhenIdle(menuWorld)) throw new Exception("Ready chapter panel was not frozen");
menuWorld.Next=select;
if(SceneControl.FreezeWhenIdle(menuWorld)) throw new Exception("Menu transition froze");
menuWorld.Next=null;SceneControl.FreezeChapterSelect=false;
if(SceneControl.FreezeWhenIdle(menuWorld)) throw new Exception("Disabled chapter freeze stayed enabled");
SceneControl.FreezeChapterSelect=true;
Console.WriteLine("PASS: chapter select/panel freeze only when input-ready; transitions and disabled policy run");

// If a freely running menu swaps to a chapter scene inside Engine.Update,
// stop at the frame gate before that chapter consumes menu input.
if (SceneControl.HaltEngine(true,false,true,(Monocle.Scene)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Monocle.Scene)))) throw new Exception("Free menu halted");
if (!SceneControl.HaltEngine(true,false,true,level)) throw new Exception("Menu-to-level boundary advanced");
var results=(AreaComplete)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(AreaComplete));
if (!SceneControl.HaltEngine(true,false,true,results)) throw new Exception("Results boundary advanced");
if (SceneControl.HaltEngine(true,false,false,level)) throw new Exception("Explicit chapter step halted");
if (SceneControl.HaltEngine(false,true,true,level)) throw new Exception("Inactive bridge halted engine");
Console.WriteLine("PASS: free-running transitions stop before chapter/results updates");

var currentArea = new AreaKey(7, AreaMode.CSide);
var selectedArea = RoomAreaSelection.Select(currentArea, null, null);
if (selectedArea.ID != 7 || selectedArea.Mode != AreaMode.CSide) throw new Exception("Loaded C side was lost");
if (RoomAreaSelection.Select(currentArea, 1, null).Mode != AreaMode.Normal) throw new Exception("Legacy chapter default changed");
if (RoomAreaSelection.Select(new AreaKey(1), 7, 2).Mode != AreaMode.CSide) throw new Exception("Explicit C side was lost");
foreach (int invalidMode in new[] {-1,3}) {
    try { RoomAreaSelection.Select(currentArea,7,invalidMode); throw new Exception("Invalid mode accepted"); }
    catch (ArgumentException) { }
}
Console.WriteLine("PASS: test room area selection preserves current side, legacy A and explicit C; invalid sides rejected");

// 真实 Monocle 组合碰撞体：圆刺临时禁用时仍须报告，圆形和矩形都保留世界坐标。
var hazard = new Monocle.Entity(new Vector2(100, 50)) {
    Collidable = false,
    Visible = false,
    Collider = new Monocle.ColliderList(new Monocle.Circle(6), new Monocle.Hitbox(16, 4, -8, -3))
};
if (!EntityGeometry.Include(hazard)) throw new Exception("Dormant collider disappeared from observation");
using (var buffer = new MemoryStream()) {
    using (var writer = new System.Text.Json.Utf8JsonWriter(buffer)) EntityGeometry.Write(writer, hazard.Collider);
    using var document = System.Text.Json.JsonDocument.Parse(buffer.ToArray());
    var parts = document.RootElement.GetProperty("parts");
    if (parts.GetArrayLength() != 2 || parts[0].GetProperty("shape").GetString() != "circle"
        || parts[0].GetProperty("r").GetSingle() != 6 || parts[0].GetProperty("x").GetSingle() != 94
        || parts[0].GetProperty("y").GetSingle() != 44
        || parts[1].GetProperty("shape").GetString() != "rect"
        || parts[1].GetProperty("x").GetSingle() != 92 || parts[1].GetProperty("y").GetSingle() != 47)
        throw new Exception("Mixed collider geometry or world coordinates lost");
}
hazard.Collider = null;
if (EntityGeometry.Include(hazard)) throw new Exception("Colliderless entity included");
Console.WriteLine("PASS: dormant entities and mixed circle/rectangle collider geometry retained");

System.Text.Json.JsonDocument UiSnapshot(Monocle.Scene scene) {
    using var buffer = new MemoryStream();
    using (var writer = new System.Text.Json.Utf8JsonWriter(buffer)) {
        writer.WriteStartObject(); StoryText.WriteUi(writer, scene); writer.WriteEndObject();
    }
    return System.Text.Json.JsonDocument.Parse(buffer.ToArray());
}
var poem = (Poem)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Poem));
poem.Visible = true; poem.Alpha = .5f; poem.TextAlpha = 1f; poem.text = "已显示的诗句";
level.Frozen = true; entities.entities.Add(poem);
using (var snapshot = UiSnapshot(level)) {
    var ui = snapshot.RootElement.GetProperty("ui");
    Check("Poem", ui.GetProperty("type").GetString()!);
    Check("已显示的诗句", ui.GetProperty("text").GetString()!);
    if (ui.TryGetProperty("confirmReady", out _)) throw new Exception("Poem visibility was mistaken for input readiness");
}
poem.TextAlpha = 0;
using (var snapshot = UiSnapshot(level)) {
    if (snapshot.RootElement.GetProperty("ui").TryGetProperty("text", out _)) throw new Exception("Hidden poem text leaked");
}
poem.Alpha = 0;
using (var snapshot = UiSnapshot(level)) {
    if (snapshot.RootElement.TryGetProperty("ui", out _)) throw new Exception("Hidden Poem reported");
}
var resultsScreen = (AreaComplete)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(AreaComplete));
foreach (var flags in new[] { (false, true), (true, true), (true, false) }) {
    resultsScreen.finishedSlide = flags.Item1; resultsScreen.canConfirm = flags.Item2;
    using var snapshot = UiSnapshot(resultsScreen);
    if (snapshot.RootElement.GetProperty("ui").GetProperty("confirmReady").GetBoolean() != (flags.Item1 && flags.Item2))
        throw new Exception("AreaComplete readiness does not match the native confirm guard");
}
Console.WriteLine("PASS: frozen Poem visibility/text and native AreaComplete confirmation readiness observed without inputs");
var dyingPlayer=(Player)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Player));
dyingPlayer.Dead=true;
if (!PlayerControl.IsDeathTransition(true,dyingPlayer)) throw new Exception("Actual death was lost");
if (PlayerControl.IsDeathTransition(false,dyingPlayer)) throw new Exception("Death animation counted twice");
if (PlayerControl.IsDeathTransition(true,null)) throw new Exception("Scene exit counted as a death");
dyingPlayer.Dead=false;
if (PlayerControl.IsDeathTransition(true,dyingPlayer)) throw new Exception("Living player counted as dead");
Console.WriteLine("PASS: real death emits once; absent player on scene exit is not a death");

// A decision to attempt Skip must be offered before the textbox waits for input.
// Use real Level.CanPause and actual game entities, with no engine/graphics update.
level.Frozen=false; level.Paused=false; level.Completed=false;
level.InCutscene=true; level.SkippingCutscene=false;
entities.entities.Clear(); entities.entities.Add(player); entities.entities.Add(box);
var decisionTracker=(Monocle.Tracker)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Monocle.Tracker));
decisionTracker.Entities=new() { [typeof(Player)]=new() {player} };
typeof(Monocle.Scene).GetField("<Tracker>k__BackingField", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
    .SetValue(level,decisionTracker);
player.Dead=false; player.StateMachine.State=Player.StDummy;
box.Visible=true; box.waitingForInput=false; box.disableInput=false;
level.PauseLock=false;
if (!level.CanPause) throw new Exception("Decision fixture unexpectedly cannot pause");
Check("",StoryText.InputNode(level) ?? "");
Check("skip",StoryText.DecisionNode(level)!);
level.PauseLock=true;
Check("",StoryText.DecisionNode(level) ?? "");
level.PauseLock=false;
Check("skip",StoryText.DecisionNode(level)!);
box.waitingForInput=true; box.runRoutine.waitTimer=0f;
Check("dialogue",StoryText.DecisionNode(level)!); // real input takes priority over Skip
box.waitingForInput=false;
player.StateMachine.State=Player.StNormal;
Check("",StoryText.DecisionNode(level) ?? ""); // playable tutorial intro stays playable
player.StateMachine.State=Player.StDummy;
level.Paused=true;
Check("",StoryText.DecisionNode(level) ?? "");
level.Paused=false; level.Frozen=true;
Check("",StoryText.DecisionNode(level) ?? "");
level.Frozen=false; player.Dead=true;
Check("",StoryText.DecisionNode(level) ?? "");
player.Dead=false; level.transition=new Monocle.Coroutine();
Check("",StoryText.DecisionNode(level) ?? "");
level.transition=null;
level.RetryPlayerCorpse=(PlayerDeadBody)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(PlayerDeadBody));
Check("",StoryText.DecisionNode(level) ?? "");
level.RetryPlayerCorpse=null; level.SkippingCutscene=true;
Check("",StoryText.DecisionNode(level) ?? "");
level.SkippingCutscene=false; level.InCutscene=false;
Check("",StoryText.DecisionNode(level) ?? "");
level.InCutscene=true;
entities.entities.Remove(player);
decisionTracker.Entities[typeof(Player)].Clear();
Check("",StoryText.DecisionNode(level) ?? "");
var intro=(IntroVignette)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(IntroVignette));
Check("skip",StoryText.DecisionNode(intro)!);
intro.exiting=true;
Check("",StoryText.DecisionNode(intro) ?? "");
intro.exiting=false; intro.menu=(TextMenu)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(TextMenu));
Check("",StoryText.DecisionNode(intro) ?? "");
card.alpha=1; card.buttonEase=1;
Check("postcard",StoryText.DecisionNode(enter)!);
Console.WriteLine("PASS: early Skip decision, real input priority, pause lock, playable tutorial, paused/frozen/dead/skipping/transition/retry and intro menu/exit exclusions");

// The memo's drawn alpha is not proof that its input loop has begun. Keep
// real game-generated iterator instances, including Everest's wrapper; never
// run them here (their animation bodies require graphics/audio/game inputs).
entities.entities.Clear();
level.InCutscene=true; level.Paused=false; level.Frozen=false; level.SkippingCutscene=false;
var memo=(CS03_Memo.MemoPage)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(CS03_Memo.MemoPage));
memo.Visible=true;memo.alpha=1;
var memoOwner=new CS03_Memo(player);
memoOwner.memo=memo;
var memoRoutine=new Monocle.Coroutine(memo.EaseIn());
memoOwner.Add(memoRoutine);
entities.entities.Add(memoOwner);entities.entities.Add(memo);
void Playback(string state,string source) {
    var actual=StoryText.PlaybackState(level);
    Check(state,actual.State);Check(source,actual.Source);
}
Check("",StoryText.InputNode(level) ?? "");
Playback("playing","memo_animation"); // even alpha=1 is still the EaseIn iterator
memoRoutine.Replace(Celeste.Mod.SwapImmediatelyExtension.SafeEnumerate(memo.Wait()));
Check("memo",StoryText.InputNode(level)!);
Playback("input","memo");
using(var snapshot=Snapshot()) {
    var ui=snapshot.RootElement.GetProperty("ui");
    Check("MemoPage",ui.GetProperty("type").GetString()!);
    if(!ui.GetProperty("inputReady").GetBoolean()) throw new Exception("Memo Wait was not ready");
    if(ui.TryGetProperty("text",out _)) throw new Exception("Memo leaked text beyond the visible viewport");
    Check("input",snapshot.RootElement.GetProperty("storyPlaybackState").GetProperty("state").GetString()!);
}
memo.Visible=false;
Check("",StoryText.InputNode(level) ?? "");
memo.Visible=true;memo.alpha=0;
Check("",StoryText.InputNode(level) ?? "");
memo.alpha=1;memo.easingOut=true;memoRoutine.Replace(memo.EaseOut());
Check("",StoryText.InputNode(level) ?? "");
Playback("playing","memo_animation");
memo.easingOut=false;
System.Collections.IEnumerator UnknownRoutine() {yield return null;}
memoRoutine.Replace(UnknownRoutine());
Check("",StoryText.InputNode(level) ?? "");
Playback("unknown","memo_unrecognized_routine");
entities.entities.Clear();
Playback("unknown","unrecognized_cutscene");
level.InCutscene=false;Playback("none","no_cutscene");
level.InCutscene=true;
box.Visible=true;box.disableInput=false;box.waitingForInput=false;box.isInTrigger=false;box.ease=1;
box.runRoutine=new Monocle.Coroutine(box.EaseOpen());
entities.entities.Add(box);
Playback("playing","dialogue_animation");
var reveal=box.RunRoutine();
reveal.GetType().GetField("<>2__current",System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
    .SetValue(reveal,.1f);
box.runRoutine.Replace(reveal);box.text=new FancyText.Text {Nodes=new() {Glyph('显')}};box.index=1;
Playback("playing","dialogue_delay"); // last glyph delay already incremented index past the page
box.isInTrigger=true;Playback("unknown","dialogue_unrecognized_routine");
box.isInTrigger=false;box.waitingForInput=true;box.runRoutine.waitTimer=.1f;
Playback("playing","dialogue_debounce");
box.runRoutine.waitTimer=0;
Playback("input","dialogue");
box.waitingForInput=false;box.runRoutine.Replace(UnknownRoutine());
Playback("unknown","dialogue_unrecognized_routine");
entities.entities.Clear();
// The postcard is driven by a LevelEnter-owned entity coroutine. Its mere
// existence is not proof that the current phase can advance without input.
var enterEntities=new Monocle.EntityList(enter);
typeof(Monocle.Scene).GetField("<Entities>k__BackingField",System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
    .SetValue(enter,enterEntities);
var enterOwner=new Monocle.Entity();
var cardRoutine=new Monocle.Coroutine(card.EaseIn());
enterOwner.Add(cardRoutine);enterEntities.entities.Add(enterOwner);
card.Visible=true;card.alpha=1;card.buttonEase=0;
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(card,enter);
Check("playing",StoryText.PlaybackState(enter).State);
Check("postcard_animation",StoryText.PlaybackState(enter).Source);
cardRoutine.Replace(Celeste.Mod.SwapImmediatelyExtension.SafeEnumerate(card.DisplayRoutine()));
cardRoutine.waitTimer=.5f;
Check("playing",StoryText.PlaybackState(enter).State);
Check("postcard_delay",StoryText.PlaybackState(enter).Source);
cardRoutine.waitTimer=0;card.buttonEase=1;
Check("input",StoryText.PlaybackState(enter).State);
card.buttonEase=0;cardRoutine.Replace(UnknownRoutine());
Check("unknown",StoryText.PlaybackState(enter).State);
var loader=(OverworldLoader)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(OverworldLoader));
var loaderEntities=new Monocle.EntityList(loader);
typeof(Monocle.Scene).GetField("<Entities>k__BackingField",System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
    .SetValue(loader,loaderEntities);
loader.postcard=card;loader.loaded=true;loaderEntities.entities.Add(enterOwner);
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(card,loader);
card.buttonEase=1;card.alpha=1;cardRoutine.Replace(card.DisplayRoutine());
Check("postcard",StoryText.InputNode(loader)!);
Check("input",StoryText.PlaybackState(loader).State);
if(!ReloadBoundary.LoaderPresentation(loader) || !SceneControl.FreezeWhenIdle(loader)) throw new Exception("Native loader card did not freeze as a presentation");
cardRoutine.Replace(card.EaseOut());
Check("",StoryText.InputNode(loader) ?? "");
Check("playing",StoryText.PlaybackState(loader).State);
if(!ReloadBoundary.LoaderPresentation(loader)) throw new Exception("Attached closing card lost its preserve boundary");
card.alpha=0;
if(!ReloadBoundary.LoaderPresentation(loader)) throw new Exception("Zero-alpha animation was treated as a removed postcard");
loaderEntities.current.Add(card);
card.RemoveSelf();
if(!ReferenceEquals(card.Scene,loader) || !loaderEntities.toRemove.Contains(card)) throw new Exception("Native RemoveSelf did not queue deferred removal");
if(ReloadBoundary.LoaderPrompt(loader) || StoryText.ScenePostcard(loader)!=null) throw new Exception("Queued postcard removal retained stale presentation/UI");
cardRoutine.Replace(card.DisplayRoutine());cardRoutine.waitTimer=1.2f;
Check("playing",StoryText.PlaybackState(loader).State);
Check("postcard_delay",StoryText.PlaybackState(loader).Source);
using(var removedBuffer=new MemoryStream()) {
    using(var writer=new System.Text.Json.Utf8JsonWriter(removedBuffer)) {
        writer.WriteStartObject();StoryText.Write(writer,loader);writer.WriteEndObject();
    }
    using var removedJson=System.Text.Json.JsonDocument.Parse(removedBuffer.ToArray());
    if(removedJson.RootElement.TryGetProperty("ui",out _)) throw new Exception("Deferred removed postcard remained in UI output");
}
loaderEntities.toRemove.Clear();loaderEntities.removing.Clear();
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(card,null);
if(ReloadBoundary.LoaderPresentation(loader)) throw new Exception("Removed postcard field froze its old loader");
Check("",StoryText.InputNode(loader) ?? "");
card.Visible=true;typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(card,loader);loader.loaded=false;
if(ReloadBoundary.LoaderPresentation(loader)) throw new Exception("Unfinished background loader was preserved");
typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(card,enter);cardRoutine.Replace(card.DisplayRoutine());
enterEntities.current.Add(card);card.alpha=0;cardRoutine.waitTimer=1.2f;card.RemoveSelf();
if(StoryText.ScenePostcard(enter)!=null) throw new Exception("Queued LevelEnter postcard remained a rendered input node");
Check("playing",StoryText.PlaybackState(enter).State);
Check("postcard_delay",StoryText.PlaybackState(enter).Source);
enterEntities.toRemove.Clear();enterEntities.removing.Clear();cardRoutine.waitTimer=0;
// Restore the entities consumed by the optional StepJob integration below.
entities.entities.Add(box);
Console.WriteLine("PASS: memo iterator readiness, full-alpha entrance, Everest wrappers, closing/hidden state, UI facts, postcard phases and fail-closed story playback");

// Exercise native iterators through Monocle.Coroutine.Update; no expected
// playback state is injected. Graphics/audio construction remains external.
var nativeFlags=System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static |
    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
void ScenePlayback(Monocle.Scene scene,string expected,string source) {
    var actual=StoryText.PlaybackState(scene);Check(expected,actual.State);Check(source,actual.Source);
}
var deltaProperty=typeof(Monocle.Engine).GetProperty("DeltaTime",nativeFlags)!;
var rawDeltaProperty=typeof(Monocle.Engine).GetProperty("RawDeltaTime",nativeFlags)!;
float oldDelta=Monocle.Engine.DeltaTime,oldRawDelta=Monocle.Engine.RawDeltaTime;
try {
    deltaProperty.SetValue(null,1f/60f);rawDeltaProperty.SetValue(null,1f/60f);
    intro.menu=null;intro.exiting=false;intro.started=false;intro.timer=0;intro.textStart=0;
    intro.text=new FancyText.Text {Nodes=new() {Glyph('序',0),Glyph('章',0)}};
    intro.textCoroutine=new Monocle.Coroutine(intro.TextSequence(),removeOnComplete:false);
    ScenePlayback(intro,"playing","intro_text_sequence");
    intro.textCoroutine.Update();
    if(intro.textCoroutine.waitTimer<2.9f) throw new Exception("Native intro initial delay did not start");
    ScenePlayback(intro,"playing","intro_text_sequence");
    intro.menu=(TextMenu)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(TextMenu));
    ScenePlayback(intro,"unknown","intro_menu");
    intro.menu=null;
    int nativeFrames=0;
    while(!intro.textCoroutine.Finished && nativeFrames++<900) {
        ScenePlayback(intro,"playing","intro_text_sequence");
        intro.textCoroutine.Update();intro.timer+=1f/60f;
    }
    if(!intro.textCoroutine.Finished || intro.textStart!=int.MaxValue || nativeFrames<180)
        throw new Exception("Native intro text failed to advance from initial delay to its real finish");
    intro.timer=16.683f;
    ScenePlayback(intro,"playing","intro_text_sequence");
    Check("",StoryText.DecisionNode(intro) ?? "");
    intro.timer=16.682f;Check("skip",StoryText.DecisionNode(intro)!);
    intro.timer=18.683f;ScenePlayback(intro,"unknown","intro_unrecognized_routine");
    intro.timer=0;intro.textCoroutine.Replace(UnknownRoutine());
    ScenePlayback(intro,"unknown","intro_unrecognized_routine");
    intro.exiting=true;ScenePlayback(intro,"unknown","intro_unrecognized_exit");
    var introRenderers=new Monocle.RendererList(intro);
    typeof(Monocle.Scene).GetField("<RendererList>k__BackingField",nativeFlags)!.SetValue(intro,introRenderers);
    var nativeComplete=typeof(IntroVignette).GetMethods(nativeFlags).Single(m=>m.Name.StartsWith("<StartGame>b__") && m.GetParameters().Length==0);
    var nativeCallback=(Action)nativeComplete.CreateDelegate(typeof(Action),intro);
    var wipe=new FadeWipe(intro,false,nativeCallback);
    ScenePlayback(intro,"playing","intro_exit_wipe"); // queued addition
    introRenderers.UpdateLists();
    for(int i=0;i<40 && !wipe.Completed;i++) {
        ScenePlayback(intro,"playing","intro_exit_wipe");wipe.Update(intro);
    }
    if(!wipe.Completed) throw new Exception("Native wipe did not finish its animation");
    ScenePlayback(intro,"playing","intro_exit_wipe"); // callback runs next frame
    wipe.ending=true;ScenePlayback(intro,"unknown","intro_unrecognized_exit");
    wipe.ending=false;introRenderers.Remove(wipe);ScenePlayback(intro,"unknown","intro_unrecognized_exit");
    introRenderers.UpdateLists();
    var unrelatedWipe=new FadeWipe(intro,false,()=>{});
    ScenePlayback(intro,"unknown","intro_unrecognized_exit");
    introRenderers.UpdateLists();introRenderers.Remove(unrelatedWipe);introRenderers.UpdateLists();
    Console.WriteLine("PASS: native intro initial wait, full text progression, finished timer tail, pause menu, skip cutoff, unknown replacement and native exit wipe timing");

    // A native Granny initial delay and dialogue-trigger callback are automatic;
    // an unrelated coroutine must not inherit that owner's authority.
    entities.entities.Clear();level.InCutscene=true;level.Paused=false;level.Frozen=false;
    level.SkippingCutscene=false;player.Dead=false;player.StateMachine.State=Player.StDummy;
    var grannyNpc=(NPC00_Granny)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(NPC00_Granny));
    grannyNpc.Position=new Vector2(100,0);player.Position=Vector2.Zero;
    var grannyScene=new CS00_Granny(grannyNpc,player);
    var grannyRoutine=new Monocle.Coroutine(grannyScene.Cutscene(),false);grannyScene.Add(grannyRoutine);
    entities.entities.Add(player);entities.entities.Add(grannyScene);
    grannyRoutine.Update();
    if(grannyRoutine.waitTimer<.49f) throw new Exception("Native Granny lead-in did not produce its delay");
    ScenePlayback(level,"playing","cutscene_animation");
    box.Visible=true;box.isInTrigger=true;box.waitingForInput=false;
    box.runRoutine=new Monocle.Coroutine(grannyScene.Meet(),false);entities.entities.Add(box);
    box.runRoutine.Update();
    if(box.runRoutine.waitTimer<.24f) throw new Exception("Native Granny Meet did not produce its delay");
    ScenePlayback(level,"playing","dialogue_trigger_animation");
    box.runRoutine.Replace(UnknownRoutine());
    ScenePlayback(level,"unknown","dialogue_unrecognized_routine"); // owner routine cannot override the textbox
    entities.entities.Remove(box);grannyRoutine.Replace(UnknownRoutine());
    ScenePlayback(level,"unknown","unrecognized_cutscene");
    grannyRoutine.Replace(grannyScene.Cutscene());
    grannyRoutine.enumerators.Push(grannyScene.RunAlong());
    grannyRoutine.enumerators.Push(level.ZoomTo(new Vector2(210,90),2f,.5f));
    level.Zoom=1f;
    for(int i=0;i<4;i++) {ScenePlayback(level,"playing","cutscene_animation");grannyRoutine.Update();}
    if(level.Zoom<=1f) throw new Exception("Native nested Granny ZoomTo did not actually advance");
    grannyRoutine.Replace(UnknownRoutine());

    var endingScene=new CS00_Ending(player,null!,null!);
    var endingNative=endingScene.Cutscene(level);
    var endingRoutine=new Monocle.Coroutine(endingNative,false);endingScene.Add(endingRoutine);
    entities.entities.Add(endingScene);
    // The native ending itself owns this legacy global time factor.
#pragma warning disable CS0618
    float oldTimeRate=Monocle.Engine.TimeRate;
    Monocle.Engine.TimeRate=1f;endingRoutine.Update();Monocle.Engine.TimeRate=oldTimeRate;
#pragma warning restore CS0618
    ScenePlayback(level,"playing","cutscene_animation");
    // This is the exact native 1.4.0.0 iterator's input resume state (IL checked),
    // deliberately with no GUI: losing GUI evidence must never authorize wait.
    var endingState=endingNative.GetType().GetField("<>1__state",nativeFlags)!;
    endingState.SetValue(endingNative,7);
    ScenePlayback(level,"unknown","unrecognized_cutscene");
    endingState.SetValue(endingNative,99);ScenePlayback(level,"unknown","unrecognized_cutscene");
    endingRoutine.Replace(UnknownRoutine());
    ScenePlayback(level,"unknown","unrecognized_cutscene");
    entities.entities.Remove(endingScene);

    // Cross-chapter boundaries use actual native iterator types and real
    // component/owner chains. Resume states are from the audited 1.4.0.0 IL;
    // independent private play verifies the transitions into these states.
    Monocle.Entity BareEntity(Type type) {
        var result=(Monocle.Entity)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(type);
        var baseline=new Monocle.Entity();
        foreach(var field in typeof(Monocle.Entity).GetFields(nativeFlags | System.Reflection.BindingFlags.DeclaredOnly))
            if(!field.IsStatic) field.SetValue(result,field.GetValue(baseline));
        typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(result,level);
        return result;
    }
    void ResumeAt(System.Collections.IEnumerator iterator,int state) => iterator.GetType().GetField("<>1__state",nativeFlags)!.SetValue(iterator,state);
    void SingleStory(Monocle.Entity owner,Monocle.Coroutine routine) {
        entities.entities.Clear();entities.entities.Add(player);entities.entities.Add(owner);owner.Add(routine);
    }
    void Yielded(System.Collections.IEnumerator iterator,object? value) => iterator.GetType().GetField("<>2__current",nativeFlags)!.SetValue(iterator,value);
    // The parent really yields its child; Monocle really pops that child after
    // completion. No Finished flag or expected playback state is injected.
    entities.entities.Clear();entities.entities.Add(box);
    box.Visible=true;box.Active=true;box.waitingForInput=false;box.isInTrigger=false;box.ease=0;box.portrait=null;
    var handoffGlyph=Glyph('.');handoffGlyph.IsPunctuation=true;
    box.text=new FancyText.Text {Nodes=new() {handoffGlyph}};box.index=0;
    var nativeDialogue=box.RunRoutine();box.runRoutine=new Monocle.Coroutine(nativeDialogue,false);
    box.runRoutine.Update();
    ScenePlayback(level,"playing","dialogue_animation");
    box.ease=1f; // avoid external audio opening resources, not completion
    box.runRoutine.Update();
    if(!ReferenceEquals(StoryText.Current(box.runRoutine),nativeDialogue) || nativeDialogue.Current is not System.Collections.IEnumerator completedOpen || !StoryText.IteratorFinished(completedOpen))
        throw new Exception("Native dialogue did not reach its completed-child handoff");
    ScenePlayback(level,"playing","dialogue_resume");
    var unknownCompleted=UnknownRoutine();unknownCompleted.MoveNext();unknownCompleted.MoveNext();
    Yielded(nativeDialogue,unknownCompleted);ScenePlayback(level,"unknown","dialogue_unrecognized_routine");
    var wrongOwnerBox=(Textbox)BareEntity(typeof(Textbox));wrongOwnerBox.ease=1f;
    var wrongOpen=wrongOwnerBox.EaseOpen();wrongOpen.MoveNext();
    Yielded(nativeDialogue,wrongOpen);ScenePlayback(level,"unknown","dialogue_unrecognized_routine");
    Yielded(nativeDialogue,completedOpen);

    // Resume the native portrait loop past font/audio setup. MoveNext itself
    // must continue yielding until the sprite's actual animation finishes.
    var portrait=new FancyText.Portrait {Sprite="madeline",Animation="normal"};
    box.text=new FancyText.Text {Nodes=new() {portrait}};box.index=0;box.portrait=portrait;
    box.portraitExists=true;box.canSkip=false;box.portraitSprite=new Monocle.Sprite(null!,"");
    typeof(Monocle.Sprite).GetProperty("Animating")!.SetValue(box.portraitSprite,true);
    typeof(Monocle.Sprite).GetProperty("CurrentAnimationID")!.SetValue(box.portraitSprite,portrait.BeginAnimation);
    var portraitNative=box.RunRoutine();ResumeAt(portraitNative,4);Yielded(portraitNative,null);
    portraitNative.GetType().GetFields(nativeFlags).Single(field=>field.Name.StartsWith("<next>") && field.FieldType==typeof(FancyText.Portrait)).SetValue(portraitNative,portrait);
    box.runRoutine=new Monocle.Coroutine(portraitNative,false);box.runRoutine.Update();
    ScenePlayback(level,"playing","dialogue_portrait_animation");
    typeof(Monocle.Sprite).GetProperty("Animating")!.SetValue(box.portraitSprite,false);
    ScenePlayback(level,"playing","dialogue_portrait_animation");
    typeof(Monocle.Sprite).GetProperty("Animating")!.SetValue(box.portraitSprite,true);
    typeof(Monocle.Sprite).GetProperty("CurrentAnimationID")!.SetValue(box.portraitSprite,portrait.IdleAnimation);
    ScenePlayback(level,"playing","dialogue_portrait_animation");
    typeof(Monocle.Sprite).GetProperty("CurrentAnimationID")!.SetValue(box.portraitSprite,portrait.BeginAnimation);
    box.text.Nodes[0]=new FancyText.Portrait {Sprite="madeline",Animation="normal"};
    ScenePlayback(level,"unknown","dialogue_unrecognized_routine");
    box.text.Nodes[0]=portrait;box.portraitExists=false;box.portrait=null;box.portraitSprite=null;

    // A genuine callback finishes its nested camera animation before Textbox
    // resumes. Exercise every native update through this handoff.
    entities.entities.Clear();entities.entities.Add(box);entities.entities.Add(grannyScene);
    typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(grannyScene,level);
    grannyScene.Level=level;
    grannyRoutine.Replace(grannyScene.Cutscene());
    box.ease=0;box.index=0;box.waitingForInput=false;box.isInTrigger=false;
    box.text=new FancyText.Text {Nodes=new() {new FancyText.Trigger {Silent=true,Index=0}}};
    box.events=new Func<System.Collections.IEnumerator>[] {grannyScene.PanToMaddy};
    var triggerParent=box.RunRoutine();box.runRoutine=new Monocle.Coroutine(triggerParent,false);
    box.runRoutine.Update();int triggerFrames=0;
    while(!ReferenceEquals(StoryText.Current(box.runRoutine),triggerParent) && triggerFrames++<180) {
        ScenePlayback(level,"playing","dialogue_trigger_animation");box.runRoutine.Update();
    }
    if(triggerFrames<20 || !ReferenceEquals(StoryText.Current(box.runRoutine),triggerParent)) throw new Exception("Native callback did not finish its nested animation");
    ScenePlayback(level,"playing","dialogue_resume");
    box.events=null;box.isInTrigger=false;entities.entities.Clear();
    if(!StoryText.IsRoutine(Textbox.Say("unused"),typeof(Textbox),"Say") ||
        !StoryText.IsRoutine(CutsceneEntity.CameraTo(Vector2.Zero,.5f),typeof(CutsceneEntity),"CameraTo") ||
        StoryText.IsRoutine(UnknownRoutine(),typeof(Textbox),"Say")) throw new Exception("Native static iterator lookup lost its type boundary");
    Console.WriteLine("PASS: real dialogue EaseOpen and nested trigger completion resume, native portrait animation/null loop and static iterator identity");

    // Only external input/audio/resources are supplied here. Native coroutine
    // updates must cross EaseIn/Wait/EaseOut completion and timed-wait zeroes.
    var previousConfirm=Input.MenuConfirm;var previousCancel=Input.MenuCancel;
    var syntheticPress=(Monocle.VirtualButton)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Monocle.VirtualButton));
    syntheticPress.bufferCounter=1f;
    string[] silentEvents={"event:/ui/main/button_lowkey","event:/game/03_resort/memo_in","event:/game/03_resort/memo_out"};
    var audioBefore=silentEvents.Select(path=>(Path:path,Had:Audio.cachedEventDescriptions.TryGetValue(path,out var sound),Sound:sound)).ToArray();
    foreach(string path in silentEvents) Audio.cachedEventDescriptions[path]=null!;
    try {
        Input.MenuConfirm=syntheticPress;Input.MenuCancel=syntheticPress;
        var flowEnter=(LevelEnter)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(LevelEnter));
        var flowEntities=new Monocle.EntityList(flowEnter);
        typeof(Monocle.Scene).GetField("<Entities>k__BackingField",nativeFlags)!.SetValue(flowEnter,flowEntities);
        var flowCard=(Postcard)BareEntity(typeof(Postcard));flowEnter.postcard=flowCard;
        typeof(Monocle.Entity).GetProperty("Scene")!.SetValue(flowCard,flowEnter);
        flowEntities.entities.Add(flowCard);flowEntities.current.Add(flowCard);
        var flowOwner=new Monocle.Entity();var display=flowCard.DisplayRoutine();
        var flow=new Monocle.Coroutine(display,false);flowOwner.Add(flow);flowEntities.entities.Add(flowOwner);
        var postcardSources=new HashSet<string>();int postcardZeroWaits=0;
        for(int frame=0;frame<420 && !flow.Finished;frame++) {
            flow.Update();flowCard.Update();
            if(flow.Finished) break;
            var actual=StoryText.PlaybackState(flowEnter);
            if(actual.State=="unknown") throw new Exception("Native postcard flow stopped: "+actual.Source);
            postcardSources.Add(actual.Source);
            if(ReferenceEquals(StoryText.Current(flow),display) && display.Current is float && flow.waitTimer<=0) postcardZeroWaits++;
        }
        if(!flow.Finished || !postcardSources.Contains("postcard_resume") || postcardZeroWaits<2)
            throw new Exception("Native postcard missed its completion or zero-timer handoffs");

        var flowMemo=(CS03_Memo.MemoPage)BareEntity(typeof(CS03_Memo.MemoPage));
        var flowMemoOwner=new CS03_Memo(player);flowMemoOwner.memo=flowMemo;flowMemoOwner.Level=level;
        var memoParent=flowMemoOwner.Routine();ResumeAt(memoParent,3);
        var memoIn=flowMemo.EaseIn();Yielded(memoParent,memoIn);
        var memoFlow=new Monocle.Coroutine(memoParent,false);memoFlow.enumerators.Push(memoIn);flowMemoOwner.Add(memoFlow);
        entities.entities.Clear();entities.entities.Add(flowMemo);entities.entities.Add(flowMemoOwner);entities.current.Add(flowMemo);
        var memoSources=new HashSet<string>();int memoResumes=0;
        for(int frame=0;frame<240;frame++) {
            if(StoryText.Current(memoFlow) is { } active && StoryText.IsRoutine(active,typeof(CS03_Memo.MemoPage),"EaseOut") && StoryText.IteratorState(active)==0)
                ResumeAt(active,1); // external render-target height is absent
            memoFlow.Update();
            var actual=StoryText.PlaybackState(level);
            if(actual.State=="unknown") throw new Exception("Native memo flow stopped: "+actual.Source);
            memoSources.Add(actual.Source);
            if(level.Entities.toRemove.Contains(flowMemo)) {
                if(StoryText.SceneEntity<CS03_Memo.MemoPage>(level)!=null)
                    throw new Exception("Removed native memo still supplied its old UI");
                if(flowMemo.alpha>0 && !StoryText.HasVisibleStoryHud(level))
                    throw new Exception("Native memo's last rendered fade frame lost its HUD capture");
                using(var retiredMemo=UiSnapshot(level))
                    if(retiredMemo.RootElement.TryGetProperty("ui",out _)) throw new Exception("Queued memo remained a logical UI");
                Check("cutscene_animation",actual.Source);break;
            }
            if(actual.Source=="memo_resume") {
                memoResumes++;
                if(memoParent.Current is System.Collections.IEnumerator child && StoryText.IsRoutine(child,typeof(CS03_Memo.MemoPage),"EaseOut")) break;
            }
        }
        if(memoResumes!=2 || !memoSources.Contains("memo") || !memoSources.Contains("memo_animation") || !level.Entities.toRemove.Contains(flowMemo))
            throw new Exception("Native memo did not complete EaseIn, real input Wait, and EaseOut handoffs");
        Console.WriteLine("PASS: continuous native Postcard/Memo EaseIn, real confirm, EaseOut, completed-child resume and delay-zero transitions");
    } finally {
        Input.MenuConfirm=previousConfirm;Input.MenuCancel=previousCancel;
        foreach(var entry in audioBefore) {
            if(entry.Had) Audio.cachedEventDescriptions[entry.Path]=entry.Sound!;
            else Audio.cachedEventDescriptions.Remove(entry.Path);
        }
    }

    var journal=new CS02_Journal(player);
    var journalNative=journal.Routine();var journalRoutine=new Monocle.Coroutine(journalNative,false);
    SingleStory(journal,journalRoutine);ResumeAt(journalNative,3);
    ScenePlayback(level,"playing","cutscene_animation");
    ResumeAt(journalNative,4);Check("confirm",StoryText.InputNode(level)!);ScenePlayback(level,"input","confirm");
    ResumeAt(journalNative,6);ScenePlayback(level,"unknown","unrecognized_cutscene");

    var boss=(CS06_BossEnd)BareEntity(typeof(CS06_BossEnd));var bossNative=boss.WaitForPress();
    var bossRoutine=new Monocle.Coroutine(boss.Cutscene(level),false);
    bossRoutine.enumerators.Push(bossNative);SingleStory(boss,bossRoutine);
    ResumeAt(bossNative,1);boss.waitForKeyPress=true;
    Check("confirm",StoryText.InputNode(level)!);ScenePlayback(level,"input","confirm");

    var campfire=(CS06_Campfire)BareEntity(typeof(CS06_Campfire));
    var campNative=campfire.Cutscene(level);var campRoutine=new Monocle.Coroutine(campNative,false);
    SingleStory(campfire,campRoutine);ResumeAt(campNative,16);
    ScenePlayback(level,"unknown","unrecognized_cutscene"); // missing choice UI never authorizes waiting
    ResumeAt(campNative,99);ScenePlayback(level,"unknown","unrecognized_cutscene");

    foreach(var type in new[]{typeof(CS08_Ending),typeof(CS10_Ending)}) {
        var owner=BareEntity(type);
        var wrapper=(System.Collections.IEnumerator)type.GetMethod("EndingRoutine",nativeFlags)!.Invoke(owner,null)!;
        var original=(System.Collections.IEnumerator)type.GetMethod("orig_EndingRoutine",nativeFlags)!.Invoke(owner,null)!;
        var origField=wrapper.GetType().GetFields(nativeFlags).Single(field=>field.Name.StartsWith("<orig>"));
        origField.SetValue(wrapper,original);ResumeAt(wrapper,1);
        var routine=new Monocle.Coroutine(wrapper,false);SingleStory(owner,routine);
        ResumeAt(original,1);ScenePlayback(level,"playing","cutscene_animation");
        ResumeAt(original,type==typeof(CS08_Ending)?4:2);
        Check("confirm",StoryText.InputNode(level)!);ScenePlayback(level,"input","confirm");
        ResumeAt(original,99);ScenePlayback(level,"unknown","unrecognized_cutscene");
    }

    var npc=(NPC01_Theo)BareEntity(typeof(NPC01_Theo));
    var talk=new Monocle.Coroutine(npc.Talk(player),false);SingleStory(npc,talk);
    talk.enumerators.Push(npc.PlayerApproachRightSide(player));
    ScenePlayback(level,"playing","cutscene_animation");
    box.Visible=false;box.waitingForInput=true;box.runRoutine=new Monocle.Coroutine(box.RunRoutine(),false);
    entities.entities.Add(box);ScenePlayback(level,"unknown","dialogue_unrecognized_routine");
    entities.entities.Remove(box);
    talk.Replace(UnknownRoutine());
    var background=new Monocle.Coroutine(npc.Yolo(),false);npc.Add(background);
    ScenePlayback(level,"unknown","unrecognized_cutscene"); // auxiliary animation cannot replace its lost driver

    var game=(BreathingMinigame)BareEntity(typeof(BreathingMinigame));
    var breathingNative=game.Routine();var breathingRoutine=new Monocle.Coroutine(breathingNative,false);
    SingleStory(game,breathingRoutine);ResumeAt(breathingNative,1);
    ScenePlayback(level,"playing","breathing_animation");
    ResumeAt(breathingNative,9);Check("breathing",StoryText.InputNode(level)!);ScenePlayback(level,"input","breathing");
    ResumeAt(breathingNative,10);game.Pausing=true;
    ScenePlayback(level,"playing","breathing_animation");
    var showText=game.ShowText(1);ResumeAt(showText,4);breathingRoutine.enumerators.Push(showText);
    Check("confirm",StoryText.InputNode(level)!);ScenePlayback(level,"input","confirm");
    var gondola=(CS04_Gondola)BareEntity(typeof(CS04_Gondola));
    gondola.Add(new Monocle.Coroutine(gondola.Cutscene(),false));entities.entities.Add(gondola);
    box.Active=true;box.Visible=true;box.waitingForInput=false;box.isInTrigger=true;
    box.ease=0;box.runRoutine=new Monocle.Coroutine(gondola.StartBreathing(),false);entities.entities.Add(box);
    Check("confirm",StoryText.InputNode(level)!);ScenePlayback(level,"input","confirm");
    breathingRoutine.enumerators.Pop();ResumeAt(breathingNative,9);
    Check("breathing",StoryText.InputNode(level)!);ScenePlayback(level,"input","breathing");
    breathingRoutine.Replace(UnknownRoutine());
    game.Add(new Monocle.Coroutine(game.FadeBoxIn(),false));
    ScenePlayback(level,"unknown","breathing_unrecognized_routine");

    entities.entities.Clear();entities.entities.Add(player);
    var photoNpc=(NPC02_Theo)BareEntity(typeof(NPC02_Theo));
    photoNpc.Add(new Monocle.Coroutine(photoNpc.Talk(player),false));entities.entities.Add(photoNpc);
    var photo=(Selfie)BareEntity(typeof(Selfie));entities.entities.Add(photo);
    var photoWait=photo.WaitForInput();ResumeAt(photoWait,1);photo.waitForKeyPress=true;
    box.runRoutine=new Monocle.Coroutine(box.RunRoutine(),false);
    box.runRoutine.enumerators.Push(photoNpc.Selfie());box.runRoutine.enumerators.Push(photoWait);entities.entities.Add(box);
    Check("confirm",StoryText.InputNode(level)!);ScenePlayback(level,"input","confirm");
    box.runRoutine.Replace(UnknownRoutine());
    ScenePlayback(level,"unknown","selfie_unrecognized_routine");
    entities.current.Add(photo);photo.RemoveSelf();
    if(StoryText.SceneEntity<Selfie>(level)!=null || StoryText.InputNode(level)!=null)
        throw new Exception("Queued removed selfie retained its input");
    ScenePlayback(level,"unknown","dialogue_unrecognized_routine");
    using(var removedPhoto=UiSnapshot(level))
        if(removedPhoto.RootElement.TryGetProperty("ui",out _)) throw new Exception("Queued removed selfie retained its UI");

    entities.entities.Clear();entities.entities.Add(player);entities.entities.Add(game);entities.entities.Add(gondola);
    entities.current.Add(game);game.bgAlpha=0;game.Completed=false;
    var finalBreathing=game.Routine();ResumeAt(finalBreathing,15);
    var finalBreathingRoutine=new Monocle.Coroutine(finalBreathing,false);game.Add(finalBreathingRoutine);
    finalBreathingRoutine.Update();
    if(!game.Completed || !finalBreathingRoutine.Finished || !entities.toRemove.Contains(game))
        throw new Exception("Native breathing final delay did not queue its own completed removal");
    ScenePlayback(level,"playing","cutscene_animation");
    if(StoryText.SceneEntity<BreathingMinigame>(level)!=null || StoryText.InputNode(level)!=null || StoryText.HasVisibleStoryHud(level))
        throw new Exception("Queued removed breathing retained input or HUD");
    using(var removedBreathing=UiSnapshot(level))
        if(removedBreathing.RootElement.TryGetProperty("ui",out _)) throw new Exception("Queued removed breathing retained its UI");

    // ShowTutorial can finish visually before the native Ending parent resumes
    // its Dash loop. An early pulse must not be advertised as accepted input.
    entities.entities.Clear();entities.entities.Add(player);player.StateMachine.State=Player.StDummy;
    var dashBird=(BirdNPC)BareEntity(typeof(BirdNPC));
    var dashGui=(BirdTutorialGui)BareEntity(typeof(BirdTutorialGui));
    dashGui.controls=new() {"Dash"};dashGui.Scale=.8f;dashGui.Visible=true;
    var dashEnding=(CS00_Ending)BareEntity(typeof(CS00_Ending));dashEnding.player=player;dashEnding.bird=dashBird;
    var dashParent=dashEnding.Cutscene(level);ResumeAt(dashParent,6);
    var showTutorial=dashBird.ShowTutorial(dashGui,false);Yielded(dashParent,showTutorial);
    var dashFlow=new Monocle.Coroutine(dashParent,false);dashFlow.enumerators.Push(showTutorial);dashEnding.Add(dashFlow);
    entities.entities.Add(dashBird);entities.entities.Add(dashGui);entities.entities.Add(dashEnding);entities.current.Add(dashGui);
    var previousDash=Input.Dash;var previousAim=Input.Aim;var previousSession=level.Session;
    var dashPress=(Monocle.VirtualButton)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Monocle.VirtualButton));
    var dashAim=(Monocle.VirtualJoystick)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Monocle.VirtualJoystick));
    typeof(Monocle.VirtualJoystick).GetProperty("Value")!.SetValue(dashAim,new Vector2(1,-1));
    try {
        Input.Dash=dashPress;Input.Aim=dashAim;
        level.Session=(Session)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Session));
        dashFlow.Update();Check("",StoryText.InputNode(level) ?? "");
        dashGui.Scale=1f; // visual UI boundary; native coroutine still state 2
        dashPress.bufferCounter=1f;dashFlow.Update();
        if(dashEnding.keyOffed) throw new Exception("ShowTutorial consumed an early Dash unexpectedly");
        Check("",StoryText.InputNode(level) ?? "");
        dashPress.bufferCounter=0;dashPress.consumed=true;dashFlow.Update();
        Check("tutorial",StoryText.InputNode(level)!);ScenePlayback(level,"input","tutorial");
        dashBird.gui=(BirdTutorialGui)BareEntity(typeof(BirdTutorialGui));
        Check("",StoryText.InputNode(level) ?? "");
        dashBird.gui=dashGui;
        dashPress.bufferCounter=1f;dashPress.consumed=false;
        try {dashFlow.Update();}
        catch(NullReferenceException) when(dashEnding.keyOffed && Audio.CurrentMusicEventInstance==null) {
            // Native input was accepted. The headless fixture has no music
            // event for the following external FMOD cue; private play does.
        }
        if(!dashEnding.keyOffed || player.StateMachine.State!=Player.StBirdDashTutorial || level.Session.Inventory.Dashes!=1)
            throw new Exception("First Dash after the reported tutorial boundary was not accepted by the native parent");
    } finally {Input.Dash=previousDash;Input.Aim=previousAim;level.Session=previousSession;}
    Console.WriteLine("PASS: native ShowTutorial visual completion, child pop, parent Dash-loop readiness and first reported-frame direction+Dash acceptance");
    Console.WriteLine("PASS: native cross-chapter NPC approach, journal/boss/8A/9A confirm boundaries and Everest wrapper, breathing confirm/control/script pause; hidden textbox and background-driver substitution rejected");
    entities.entities.Clear();entities.entities.Add(player);

    player.StateMachine=new Monocle.StateMachine(32);
    player.StateMachine.SetCallbacks(Player.StIntroWalk,null,player.IntroWalkCoroutine);
    player.StateMachine.State=Player.StIntroWalk;
    ScenePlayback(level,"playing","player_intro_animation");
    player.StateMachine.currentCoroutine.Replace(UnknownRoutine());
    ScenePlayback(level,"unknown","unrecognized_cutscene");
    player.StateMachine.SetCallbacks(Player.StIntroJump,null,player.IntroJumpCoroutine);
    player.StateMachine.State=Player.StIntroJump;
    ScenePlayback(level,"playing","player_intro_animation");
    player.StateMachine.State=Player.StDummy;
    ScenePlayback(level,"unknown","unrecognized_cutscene");
    Console.WriteLine("PASS: real Granny lead-in/Meet progression, unknown textbox precedence, Ending automatic onset vs missing-GUI input state, native IntroWalk/Jump vs unknown/Dummy");
} finally {
    deltaProperty.SetValue(null,oldDelta);rawDeltaProperty.SetValue(null,oldRawDelta);
    // Restore the original integration-test fixture.
    entities.entities.Clear();entities.entities.Add(box);box.isInTrigger=false;
    player.StateMachine=new Monocle.StateMachine(32);
}

// Optional integration check against the freshly built full bridge: execute the
// real StepJob constructor/NextInput/AfterFrame path, not a copy of its edge rule.
if (args.Length > 0) {
    var bridgePath=Path.GetFullPath(args[0]);
    var bridgeDirectory=Path.GetDirectoryName(bridgePath)!;
    // Payload builds and deployed Mods both keep the resident assembly beside
    // the payload's module, rather than defining InputFrame in the payload.
    System.Runtime.Loader.AssemblyLoadContext.Default.Resolving += (_,name) => {
        foreach(var candidate in new[] {
            Path.Combine(bridgeDirectory,name.Name+".dll"),
            Path.Combine(bridgeDirectory,"..","..","game-runtime","bin",name.Name+".dll"),
            Path.Combine(bridgeDirectory,"..","..","CelesteAIRuntime","bin",name.Name+".dll")})
            if(File.Exists(candidate)) return System.Reflection.Assembly.LoadFrom(Path.GetFullPath(candidate));
        return null;
    };
    var assembly=System.Reflection.Assembly.LoadFrom(bridgePath);
    var stepType=assembly.GetType("CelesteAI.Bridge.StepJob",throwOnError:true)!;
    var snapshotType=assembly.GetType("CelesteAI.Bridge.SnapshotOptions",throwOnError:true)!;
    var flags=System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Instance
        | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
    var ctor=stepType.GetConstructors(flags).Single();
    var parameters=ctor.GetParameters();
    var frameType=parameters.Single(p=>p.Name=="plan").ParameterType.GetGenericArguments().Single();
    var engine=(Monocle.Engine)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Monocle.Engine));
    var instance=typeof(Monocle.Engine).GetProperty("Instance",flags)!;
    var oldEngine=instance.GetValue(null);
    instance.SetValue(null,engine);
    typeof(Monocle.Engine).GetField("scene",flags)!.SetValue(engine,level);
    level.Session=(Session)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(Session));
    level.Session.Level="decision-test";
    entities.entities.Add(player); decisionTracker.Entities[typeof(Player)].Add(player);
    player.Dead=false; player.StateMachine.State=Player.StDummy;
    level.InCutscene=true; level.Paused=false; level.Frozen=false; level.SkippingCutscene=false;
    level.PauseLock=true; box.waitingForInput=false;
    try {
        var plan=(System.Collections.IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(frameType))!;
        for (int i=0;i<3;i++) plan.Add(Activator.CreateInstance(frameType));
        var values=parameters.Select(p => p.HasDefaultValue ? p.DefaultValue : p.ParameterType.IsValueType ? Activator.CreateInstance(p.ParameterType) : null).ToArray();
        for (int i=0;i<parameters.Length;i++) values[i]=parameters[i].Name switch {
            "requestId" => 1L, "plan" => plan, "stopOn" => new[]{"story_decision"},
            "framesPerTick" => 1, "buffer" => "gameplay", "snapshot" => Activator.CreateInstance(snapshotType),
            _ => values[i]
        };
        var job=ctor.Invoke(values);
        var next=stepType.GetMethod("NextInput",flags)!;
        var after=stepType.GetMethod("AfterFrame",flags)!;
        var reason=stepType.GetProperty("StopReason",flags)!;
        next.Invoke(job,null); after.Invoke(job,new object[]{1L});
        if (reason.GetValue(job)!=null) throw new Exception("Locked-pause frame stopped before skip opportunity");
        level.PauseLock=false;
        next.Invoke(job,null); after.Invoke(job,new object[]{2L});
        Check("story_decision",reason.GetValue(job)?.ToString() ?? "");
        var records=(System.Collections.IEnumerable)stepType.GetField("events",flags)!.GetValue(job)!;
        bool exact=false;
        foreach (var item in records) {
            var itemType=item.GetType();
            if ((string?)itemType.GetField("Item2")!.GetValue(item)=="story_decision"
                && (long)itemType.GetField("Item1")!.GetValue(item)! == 2L
                && (string?)itemType.GetField("Item3")!.GetValue(item)=="skip") exact=true;
        }
        if (!exact) throw new Exception("StepJob did not publish Skip on the first pause-unlocked frame");
        // Starting at an acknowledged opportunity must not zero-frame lock act;
        // no forced stop applies to a caller (including TAS) that did not request it.
        var alreadySkippable=ctor.Invoke(values);
        if (reason.GetValue(alreadySkippable)!=null) throw new Exception("Existing skip opportunity locked act before its first frame");
        next.Invoke(alreadySkippable,null); after.Invoke(alreadySkippable,new object[]{3L});
        if (reason.GetValue(alreadySkippable)!=null) throw new Exception("Stable skip opportunity stopped explicit act repeatedly");
        level.PauseLock=true;
        var noStopValues=(object?[])values.Clone();
        noStopValues[Array.FindIndex(parameters,p=>p.Name=="stopOn")]=Array.Empty<string>();
        var noForcedStop=ctor.Invoke(noStopValues);
        level.PauseLock=false;
        next.Invoke(noForcedStop,null); after.Invoke(noForcedStop,new object[]{4L});
        if (reason.GetValue(noForcedStop)!=null) throw new Exception("Unrequested story decision stopped a TAS/raw-input caller");
        box.Visible=false;memo.Visible=true;memo.alpha=1;memo.easingOut=false;
        entities.entities.Add(memoOwner);entities.entities.Add(memo);
        memoRoutine.Replace(memo.EaseIn());
        var memoValues=(object?[])values.Clone();
        memoValues[Array.FindIndex(parameters,p=>p.Name=="stopOn")]=new[]{"story_input"};
        var memoJob=ctor.Invoke(memoValues);
        next.Invoke(memoJob,null);after.Invoke(memoJob,new object[]{5L});
        if(reason.GetValue(memoJob)!=null) throw new Exception("Memo full-alpha entrance incorrectly accepted input");
        memoRoutine.Replace(Celeste.Mod.SwapImmediatelyExtension.SafeEnumerate(memo.Wait()));
        next.Invoke(memoJob,null);after.Invoke(memoJob,new object[]{6L});
        Check("story_input",reason.GetValue(memoJob)?.ToString() ?? "");
        bool memoEvent=false;
        foreach(var item in (System.Collections.IEnumerable)stepType.GetField("events",flags)!.GetValue(memoJob)!) {
            var itemType=item.GetType();
            if((string?)itemType.GetField("Item2")!.GetValue(item)=="story_input"
                && (long)itemType.GetField("Item1")!.GetValue(item)! ==6L
                && (string?)itemType.GetField("Item3")!.GetValue(item)=="memo") memoEvent=true;
        }
        if(!memoEvent) throw new Exception("StepJob missed the first memo Wait input frame");
        Console.WriteLine("PASS: full bridge StepJob stops and emits Skip at the first pause-unlocked frame");
        Console.WriteLine("PASS: full bridge StepJob preserves explicit act at an existing decision and never forces unrequested stops");
        Console.WriteLine("PASS: full bridge StepJob stops exactly when memo entrance switches to Wait");
    } finally { instance.SetValue(null,oldEngine); }
}
