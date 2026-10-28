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
