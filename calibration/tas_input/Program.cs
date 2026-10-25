using Celeste;
using CelesteAI.Bridge;
using Monocle;
using System.Runtime.CompilerServices;

void Check(bool value, string label) { if (!value) throw new Exception(label); }
MInput.Initialize();
MInput.Active=true; MInput.Disabled=false;
MInput.ControllerHasFocus=true; MInput.GamePads[0].Attached=true;
var entry = new MapEntryGuard("Celeste/1-ForsakenCity", 10);
foreach (string key in new[] {"J", "O"})
    Check(entry.Check(InputFrame.ParseTas(key), "Celeste/2-OldSite", 10) != null, $"TAS {key} entered wrong map");
Check(entry.Check(InputFrame.Parse("C"), "Celeste/2-OldSite", 10) != null, "Native confirm entered wrong map");
Check(entry.Check(InputFrame.Parse("C"), "Celeste/1-ForsakenCity", 11) != null, "Wrong save slot entered map");
foreach (string key in new[] {"L", "R", "U", "D", "B", ""})
    Check(entry.Check(InputFrame.Parse(key), "Celeste/2-OldSite", 10) == null, $"Navigation {key} blocked");
Check(entry.Check(InputFrame.ParseTas("J"), "Celeste/1-ForsakenCity", 10) == null, "Right map confirmation blocked");
Check(new MapEntryGuard(null,10).Check(InputFrame.Parse("C"), "Celeste/2-OldSite",10) == null, "Unrestricted map blocked");
var settings=(Settings)RuntimeHelpers.GetUninitializedObject(typeof(Settings));
InputInjector.ConfigureBindings(settings);
var jump=new VirtualButton(settings.Jump,0,0,0.2f);
var bufferedJump=new VirtualButton(settings.Jump,0,.08f,0.2f);
var dash=new VirtualButton(settings.Dash,0,0,0.2f);
var grab=new VirtualButton(settings.Grab,0,0,0.2f);
var talk=new VirtualButton(settings.Talk,0,0,0.2f);
var confirm=new VirtualButton(settings.Confirm,0,0,0.2f);
var cancel=new VirtualButton(settings.Cancel,0,0,0.2f);
var pause=new VirtualButton(settings.Pause,0,0,0.2f);
var journal=new VirtualButton(settings.Journal,0,0,0.2f);
var restart=new VirtualButton(settings.QuickRestart,0,0,0.2f);
void Tas(string keys) => InputInjector.Feed(InputFrame.ParseTas(keys));
void Native(string keys) => InputInjector.Feed(InputFrame.Parse(keys));

Tas("J"); Check(jump.Pressed && confirm.Pressed && jump.Check,"J must jump and confirm simultaneously");
Check(bufferedJump.Pressed,"Native jump buffer did not see TAS press");bufferedJump.ConsumeBuffer();
Tas("J"); Check(!jump.Pressed && !confirm.Pressed && jump.Check,"Holding J created a new edge");
Check(!bufferedJump.Pressed,"Holding TAS key refilled a consumed native jump buffer");
Tas("J,K"); Check(jump.Pressed && !confirm.Pressed,"Second jump must create its own edge, not confirm");
Check(bufferedJump.Pressed,"Second TAS jump did not refill native jump buffer");bufferedJump.ConsumeBuffer();
Tas(""); Check(!jump.Check && jump.Released && confirm.Released,"Neutral did not release TAS keys");
Tas("X"); Check(dash.Pressed && talk.Pressed && cancel.Pressed,"X must dash/talk/cancel simultaneously");
Tas("X,C"); Check(dash.Pressed && cancel.Pressed && !talk.Pressed,"C must be second dash/cancel, not confirm/talk");
Tas("");
Tas("N"); Check(journal.Pressed && talk.Pressed,"N must journal and talk simultaneously");
Tas("S,Q,G,H,O"); Check(pause.Pressed && restart.Pressed && grab.Check && confirm.Pressed,"TAS menu/grab channels missing");
Tas("");
Native("J"); Check(jump.Pressed && !confirm.Pressed,"Native J changed meaning");
Native("");
Native("C"); Check(confirm.Pressed && !dash.Pressed,"Native C changed meaning");
Native("");
Native("X"); Check(dash.Pressed && !cancel.Pressed && !talk.Pressed,"Native X acquired TAS menu bindings");
Check(InputFrame.ParseTas("C").ToSimulatorString()=="Y","TAS C was exported as native confirm");
Check(InputFrame.ParseTas("X").ToSimulatorString()=="X,T","TAS X lost its gameplay Talk signal in simulator export");
foreach (var modal in new[]{"S","Q","N","O","H"}) {
    try { InputFrame.ParseTas(modal).ToSimulatorString();throw new Exception("Unsupported simulator state exported"); }
    catch (NotSupportedException) { }
}
Check(!InputFrame.ParseTas("J").Equals(InputFrame.Parse("J")),"Format identity was lost");
try { InputFrame.ValidateTransition(InputFrame.ParseTas("J"),false);throw new Exception("Held format switch accepted"); }
catch (InvalidOperationException) { }
InputFrame.ValidateTransition(InputFrame.ParseTas("J"),true);
InputFrame.ValidateTransition(InputFrame.ParseTas(""),false);
InputFrame.ValidateTransition(InputFrame.Neutral,true);
foreach (var unsupported in new[]{"F,90","A,R","M,L","P,A","Y","B"}) {
    try { InputFrame.ParseTas(unsupported);throw new Exception("Unsupported input accepted: "+unsupported); }
    catch (FormatException) { }
}
Console.WriteLine("PASS: actual virtual-button edges, shared bindings, holds/releases, simultaneous/alternate keys, native isolation, simulator held keys and unsupported inputs");

namespace CelesteAI.Bridge {
    internal static class BridgeModule { public static Celeste.Mod.EverestModule? Instance => null; }
    internal static class Log {
        public static void Info(string message) { }
        public static void Warn(string message) { }
    }
}
