using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using Celeste;
using CelesteAI.Bridge;
using Monocle;
using MonoMod.RuntimeDetour;
using MonoMod.Cil;
using Bridge = CelesteAI.Bridge.Bridge;

var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance;
AssemblyLoadContext.Default.Resolving += (_, name) => {
    var path = Path.Combine(args[0], name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.GetFullPath(path)) : null;
};
void Check(bool value, string message) { if (!value) throw new Exception(message); }
// No game process or save is touched. Use real menus, virtual buttons, production
// IL injection and StepJob; only button effects are counters instead of exits.
var engine = (Engine)RuntimeHelpers.GetUninitializedObject(typeof(Engine));
typeof(Engine).GetProperty("Instance", flags)!.SetValue(null, engine);
typeof(Engine).GetField("scene", flags)!.SetValue(engine, RuntimeHelpers.GetUninitializedObject(typeof(Scene)));
Dialog.Language = new Language();
Dialog.FallbackLanguage = Dialog.Language;
string[] ids = { "menu_pause_resume", "menu_pause_retry", "menu_pause_skip_cutscene", "intro_vignette_resume",
    "intro_vignette_skip", "menu_restart_cancel", "menu_return_cancel", "menu_pause_return", "menu_pause_savequit",
    "intro_vignette_quit", "menu_return_continue", "menu_pause_restartarea", "menu_restart_continue" };
foreach (string id in ids) {
    Dialog.Language.Dialog[id.ToUpperInvariant()] = id;
    Dialog.Language.Cleaned[id.ToUpperInvariant()] = id;
}
MInput.Initialize(); MInput.Active = true; MInput.Disabled = false;
MInput.ControllerHasFocus = true; MInput.GamePads[0].Attached = true;
var settings = (Settings)RuntimeHelpers.GetUninitializedObject(typeof(Settings));
Settings.Instance = settings;
InputInjector.ConfigureBindings(settings);
Input.MenuConfirm = new VirtualButton(settings.Confirm, 0, 0, .2f);
Input.MenuCancel = new VirtualButton(settings.Cancel, 0, 0, .2f);
Input.MenuJournal = new VirtualButton(settings.Journal, 0, 0, .2f);
Input.Pause = new VirtualButton(settings.Pause, 0, 0, .2f);
Input.ESC = new VirtualButton(0);
// Native menu vertical axis is configured from the same synthetic gamepad.
Input.MenuUp = new VirtualButton(0);
Input.MenuUp.Nodes.Add(new VirtualButton.PadDPadUp(0));
Input.MenuDown = new VirtualButton(0);
Input.MenuDown.Nodes.Add(new VirtualButton.PadDPadDown(0));
Input.MenuLeft = new VirtualButton(0);
Input.MenuLeft.Nodes.Add(new VirtualButton.PadDPadLeft(0));
Input.MenuRight = new VirtualButton(0);
Input.MenuRight.Nodes.Add(new VirtualButton.PadDPadRight(0));
var manipulator = (ILContext.Manipulator)typeof(Hooks).GetMethod("ILMenuConfirmation", flags)!
    .CreateDelegate(typeof(ILContext.Manipulator));
using var hook = new ILHook(typeof(TextMenu).GetMethod("orig_Update", flags)!, manipulator);
using var silence = new Hook(typeof(Audio).GetMethod("Play", new[] { typeof(string) })!,
    (Func<string, FMOD.Studio.EventInstance>)(_ => null!));
Check(ChapterResetGuard.Available, "Production menu hook did not install");
int confirmed = 0, pressed = 0;
Tags.PauseUpdate = new BitTag("pause");
Tags.HUD = new BitTag("hud");
TextMenu Menu(params string[] labels) {
    var menu = new TextMenu { Focused = true, AutoScroll = false };
    foreach (var label in labels) {
        var button = new ProbeButton(label, () => confirmed++);
        button.OnPressed = () => pressed++;
        button.Container = menu;
        button.SelectWiggler = Wiggler.Create(.1f, 1);
        menu.Items.Add(button);
    }
    menu.Selection = 0;
    return menu;
}
StepJob Job(bool allow = false, bool exit = false) {
    var job = new StepJob(1, new() { InputFrame.Neutral, InputFrame.Neutral }, Array.Empty<string>(), 1,
        false, "level", new SnapshotOptions(), false, allowChapterReset: allow, allowMapExit: exit);
    typeof(Bridge).GetField("job", flags)!.SetValue(null, job);
    return job;
}
void Feed(string keys, bool tas = false) => InputInjector.Feed(tas ? InputFrame.ParseTas(keys) : InputFrame.Parse(keys));
foreach (string dangerous in new[] { "menu_pause_restartarea", "menu_restart_continue", "menu_pause_return", "menu_return_continue", "menu_pause_savequit", "intro_vignette_quit" }) {
    foreach (bool tas in new[] { false, true }) {
        Feed("", tas); var menu = Menu(dangerous); var job = Job();
        int before = pressed; job.NextInput(); Feed(tas ? "J" : "C", tas); menu.orig_Update(); job.AfterFrame(1);
        Check(pressed == before && confirmed == before, $"Dangerous {dangerous} callback executed");
        Check(job.StopReason == "chapter_reset_blocked", "Missing blocked result");
        using var buffer = new MemoryStream();
        using (var writer = new System.Text.Json.Utf8JsonWriter(buffer)) {
            writer.WriteStartObject(); job.WriteResult(writer); writer.WriteEndObject();
        }
        using var result = System.Text.Json.JsonDocument.Parse(buffer.ToArray());
        Check(result.RootElement.GetProperty("consumed").GetInt32() == 1, "Blocked frame accounting wrong");
        Check(result.RootElement.GetProperty("planned").GetInt32() == 2 && job.Finished, "Remaining plan not stopped");
    }
}
foreach (var (keys, tas) in new[] { ("J", false), ("Q", true), ("K", true) }) {
    Feed("", tas); var menu = Menu("menu_pause_restartarea"); var job = Job(); int before = pressed;
    Feed(keys, tas); menu.orig_Update();
    Check(pressed == before && job.StopReason == null, "Non-confirm/room-retry input was blocked");
}
foreach (string safe in new[] { "menu_pause_resume", "menu_pause_retry", "menu_pause_skip_cutscene", "menu_restart_cancel", "menu_return_cancel", "ordinary settings" }) {
    Feed(""); var menu = Menu(safe); var job = Job(); int before = pressed;
    Feed("C"); menu.orig_Update();
    Check(pressed == before + 1 && confirmed == pressed && job.StopReason == null, "Safe confirmation blocked: " + safe);
}
// Same frame navigation must use the real TextMenu selection order, both ways.
foreach (bool intoDanger in new[] { true, false }) {
    Feed(""); var menu = intoDanger ? Menu("menu_pause_resume", "menu_pause_restartarea") : Menu("menu_pause_restartarea", "menu_pause_resume");
    var job = Job(); int before = pressed;
    Feed("D,C"); menu.orig_Update();
    Check(menu.Selection == 1, "Native navigation did not run before confirmation");
    Check(pressed == before + (intoDanger ? 0 : 1), "Combined direction+confirm used stale selection");
    Check((job.StopReason == "chapter_reset_blocked") == intoDanger, "Combined key result wrong");
}
foreach (bool allow in new[] { true, false }) {
    Feed(""); var menu = Menu("menu_pause_return"); var job = Job(allow, exit: !allow); int before = pressed;
    Feed("C"); menu.orig_Update(); Check(pressed == before + 1, "Configured/verified departure blocked");
}
Feed(""); var restartMenu = Menu("menu_pause_restartarea"); var restartJob = Job(exit: true);
Feed("C"); restartMenu.orig_Update(); Check(restartJob.StopReason == "chapter_reset_blocked", "Handoff allowed chapter restart");
Console.WriteLine("PASS: production IL hook + real TextMenu + virtual buttons + StepJob: native/TAS resets, secondary confirmations, safe callbacks, simultaneous direction+confirm, configured allow and verified departure");

sealed class ProbeButton(string label, Action onConfirm) : TextMenu.Button(label) {
    public override void ConfirmPressed() => onConfirm();
}
