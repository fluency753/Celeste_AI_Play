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
    "intro_vignette_quit", "menu_return_continue", "menu_pause_restartarea", "menu_restart_continue", "menu_return_title", "menu_restart_title" };
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
Check(ChapterResetGuard.Initialize(), "Native callback identity initialization failed");
// A method without the confirmation dispatch (a game/Everest update, or another mod rewriting orig_Update): the
// manipulator must not throw — that would abort the bridge's Load and every other hook with it — and must report
// the guard unavailable (hello then reports action_policy=0 and the controller refuses to start).
using (var probe = new ILHook(typeof(TextMenu.Item).GetMethod("ConfirmPressed", flags)!, manipulator)) {
    Check(!ChapterResetGuard.Available, "Guard reported available without its injection point");
}
using var hook = new ILHook(typeof(TextMenu).GetMethod("orig_Update", flags)!, manipulator);
using var silence = new Hook(typeof(Audio).GetMethod("Play", new[] { typeof(string) })!,
    (Func<string, FMOD.Studio.EventInstance>)(_ => null!));
using var noFontLayout = new Hook(typeof(TextMenu).GetMethod("RecalculateSize", flags)!, (Action<TextMenu>)(_ => { }));
Check(ChapterResetGuard.Available, "Production menu hook did not install");
int confirmed = 0, pressed = 0;
var nativeCallbacks = new Dictionary<string, (MethodInfo Method, object Binding)>();
foreach (System.Collections.DictionaryEntry pair in (System.Collections.IDictionary)typeof(ChapterResetGuard).GetField("bindings", flags)!.GetValue(null)!) {
    object binding = pair.Value!;
    nativeCallbacks.Add((string)binding.GetType().GetProperty("Key")!.GetValue(binding)!, ((MethodInfo)pair.Key, binding));
}
// Replace only the external effects of actual native callbacks; dispatch and
// callback identity/owner recognition remain production code.
var callbackEffects = nativeCallbacks.Values.Select(value => new Hook(value.Method,
    (Action<object>)(_ => { pressed++; confirmed++; }))).ToList();
Tags.PauseUpdate = new BitTag("pause");
Tags.HUD = new BitTag("hud");
Action NativeCallback(string key) {
    if (key == "menu_return_continue") key = "menu_restart_continue";
    if (key == "menu_return_cancel") key = "menu_restart_cancel";
    var (method, binding) = nativeCallbacks[key];
    FieldInfo[] owner = (FieldInfo[])binding.GetType().GetProperty("Owner")!.GetValue(binding)!;
    object target = owner.Length == 0 ? Engine.Scene : RuntimeHelpers.GetUninitializedObject(method.DeclaringType!);
    object current = target;
    for (int i = 0; i < owner.Length; i++) {
        object child = i == owner.Length - 1 ? Engine.Scene : RuntimeHelpers.GetUninitializedObject(owner[i].FieldType);
        owner[i].SetValue(current, child);
        current = child;
    }
    return (Action)method.CreateDelegate(typeof(Action), target);
}
TextMenu Menu(params string[] labels) {
    var menu = new TextMenu { Focused = true, AutoScroll = false };
    foreach (var label in labels) {
        bool intro = label.StartsWith("intro_");
        if (intro && Engine.Scene is not IntroVignette) Scene((IntroVignette)RuntimeHelpers.GetUninitializedObject(typeof(IntroVignette)));
        if (label.StartsWith("menu_") && Engine.Scene is not Celeste.Level) Scene(Level());
        var button = new TextMenu.Button(Dialog.Has(label) ? Dialog.Clean(label) : label);
        string key = label == "menu_return_continue" ? "menu_restart_continue" : label == "menu_return_cancel" ? "menu_restart_cancel" : label;
        if (nativeCallbacks.TryGetValue(key, out var info)) {
            button.OnPressed = NativeCallback(label);
            if (info.Binding.GetType().GetProperty("RestartArea")!.GetValue(info.Binding) is FieldInfo restart)
                restart.SetValue(button.OnPressed.Target, label == "menu_restart_continue");
        } else button.OnPressed = () => { pressed++; confirmed++; };
        button.Container = menu;
        button.SelectWiggler = Wiggler.Create(.1f, 1);
        menu.Items.Add(button);
    }
    menu.Selection = 0;
    return menu;
}
StepJob Job(bool allow = false, bool exit = false, ActionPermissions? permissions = null, MapEntryGuard? entry = null) {
    var job = new StepJob(1, new() { InputFrame.Neutral, InputFrame.Neutral }, Array.Empty<string>(), 1,
        false, "level", new SnapshotOptions(), false,
        actionPolicy: new ActionPolicy(permissions ?? new(allow, allow, allow), entry, exit));
    typeof(Bridge).GetField("job", flags)!.SetValue(null, job);
    return job;
}
void Feed(string keys, bool tas = false) => InputInjector.Feed(tas ? InputFrame.ParseTas(keys) : InputFrame.Parse(keys));
void Scene(Scene scene) => typeof(Engine).GetField("scene", flags)!.SetValue(engine, scene);
Level Level(Session? session = null) {
    var level = (Level)RuntimeHelpers.GetUninitializedObject(typeof(Level));
    level.Session = session ?? (Session)RuntimeHelpers.GetUninitializedObject(typeof(Session));
    level.Paused = true;
    var tracker = (Tracker)RuntimeHelpers.GetUninitializedObject(typeof(Tracker));
    tracker.Entities = new() { [typeof(Player)] = new() };
    typeof(Scene).GetField("<Tracker>k__BackingField", flags)!.SetValue(level, tracker);
    typeof(Scene).GetField("<Entities>k__BackingField", flags)!.SetValue(level, new EntityList(level));
    return level;
}
foreach (string yes in new[] { "是", "Yes" }) {
    foreach (string key in new[] { "menu_restart_continue", "menu_return_continue" }) {
        Dialog.Language.Dialog[key.ToUpperInvariant()] = yes;
        Dialog.Language.Cleaned[key.ToUpperInvariant()] = yes;
    }
    foreach (bool restart in new[] { false, true }) {
        for (int mask = 0; mask < 8; mask++) {
            var level = Level(); Scene(level);
            typeof(Level).GetMethod("GiveUp", flags)!.Invoke(level, new object[] { 0, restart, false, false });
            var confirmation = level.Entities.ToAdd.OfType<TextMenu>().Single();
            Check(((TextMenu.Button)confirmation.Items[1]).Label == yes, "Native confirmation did not use shared real translation");
            Check(ChapterResetGuard.ActionOf(confirmation.Items[1]) == (restart ? "restart_chapter" : "return_to_map"),
                "Actual native GiveUp callback was misclassified");
            Check(ChapterResetGuard.IsSafe(confirmation.Items[2]), "Actual native GiveUp cancel was not safe");
            var permissions = new ActionPermissions((mask & 1) != 0, (mask & 2) != 0, (mask & 4) != 0);
            Feed(""); var job = Job(permissions: permissions); int before = pressed;
            Feed("C"); confirmation.orig_Update();
            bool allowed = restart ? permissions.RestartChapter : permissions.ReturnToMap;
            Check((pressed == before + 1) == allowed && (job.StopReason == null) == allowed,
                "Shared confirmation translation coupled independent action permissions");
            // Existing native menus survive registry reconstruction after reload.
            Check(ChapterResetGuard.Initialize() && ChapterResetGuard.ActionOf(confirmation.Items[1]) ==
                (restart ? "restart_chapter" : "return_to_map"), "Existing native menu identity lost after reinitialize");
        }
    }
}
Console.WriteLine("PASS: actual native GiveUp menus share Chinese/English Yes but preserve independent restart/return permissions and survive identity reconstruction");
var actualIntro = (IntroVignette)RuntimeHelpers.GetUninitializedObject(typeof(IntroVignette));
typeof(Scene).GetField("<Entities>k__BackingField", flags)!.SetValue(actualIntro, new EntityList(actualIntro));
Scene(actualIntro);
actualIntro.OpenMenu();
var actualIntroMenu = (TextMenu)typeof(IntroVignette).GetField("menu", flags)!.GetValue(actualIntro)!;
Check(ChapterResetGuard.IsSafe(actualIntroMenu.Items[0]) && ChapterResetGuard.IsSafe(actualIntroMenu.Items[1]) &&
    ChapterResetGuard.ActionOf(actualIntroMenu.Items[2]) == "return_to_map", "Actual IntroVignette construction identity is wrong");
Check(ChapterResetGuard.Initialize() && ChapterResetGuard.ActionOf(actualIntroMenu.Items[2]) == "return_to_map",
    "Preserved actual intro menu identity was lost on reload");
Check(!Job().AllowMenuConfirmation(actualIntroMenu.Items[2]) &&
    Job(permissions: new(false, true, false)).AllowMenuConfirmation(actualIntroMenu.Items[2]),
    "Actual intro return permission is coupled to restart/save permission");
Console.WriteLine("PASS: actual native IntroVignette menu construction and preserved menu re-identification retain safe callbacks and independent return permission");
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
foreach (string safe in new[] { "menu_pause_resume", "menu_pause_retry", "menu_pause_skip_cutscene", "menu_restart_cancel", "menu_return_cancel" }) {
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
    Scene(Level());
    Feed(""); var menu = Menu("menu_pause_return"); var job = Job(allow, exit: !allow); int before = pressed;
    Feed("C"); menu.orig_Update(); Check(pressed == before + 1, "Configured/verified departure blocked");
}
Feed(""); var restartMenu = Menu("menu_pause_restartarea"); var restartJob = Job(exit: true);
Feed("C"); restartMenu.orig_Update(); Check(restartJob.StopReason == "chapter_reset_blocked", "Handoff allowed chapter restart");

// One job may span loading frames. Its old Level grant must not reach the new
// Level, even if the game reuses Session or returns to the original instance.
foreach (bool reuseSession in new[] { false, true }) {
    var old = Level(); Scene(old); var job = Job(exit: true);
    Check(job.AllowMenuConfirmation(Menu("menu_pause_return").Items[0]), "Old Level main exit rejected");
    Check(job.AllowMenuConfirmation(Menu("menu_return_continue").Items[0]), "Second confirmation consumed grant early");
    job.NextInput();
    Bridge.AdvanceFrame(() => Scene((LevelLoader)RuntimeHelpers.GetUninitializedObject(typeof(LevelLoader))), InputFrame.Neutral);
    var next = Level(reuseSession ? old.Session : null);
    Bridge.AdvanceFrame(() => Scene(next), InputFrame.Neutral);
    job.NextInput();
    Feed(""); var menu = Menu("menu_pause_savequit"); int before = pressed;
    Feed("C"); menu.orig_Update();
    Check(job.Finished && pressed == before, "Old departure permission leaked into a new Level");
    Scene(old);
    Check(!job.AllowMenuConfirmation(Menu("menu_pause_return").Items[0]), "Revoked grant revived on old Level");
}
Scene((Scene)RuntimeHelpers.GetUninitializedObject(typeof(Scene)));
var fromMenu = Job(exit: true); Scene(Level());
Check(!fromMenu.AllowMenuConfirmation(Menu("menu_pause_return").Items[0]), "Menu job bound a future Level");
var replacedSession = Level(); Scene(replacedSession); var sessionGrant = Job(exit: true);
replacedSession.Session = (Session)RuntimeHelpers.GetUninitializedObject(typeof(Session));
Check(!sessionGrant.AllowMenuConfirmation(Menu("menu_pause_return").Items[0]), "Replacing Session preserved departure grant");
// Every permission controls only its own semantic action.
for (int mask = 0; mask < 8; mask++) {
    var permission = new ActionPermissions((mask & 1) != 0, (mask & 2) != 0, (mask & 4) != 0);
    foreach (var (label, allowed) in new[] { ("menu_pause_restartarea", permission.RestartChapter),
            ("menu_pause_return", permission.ReturnToMap), ("menu_pause_savequit", permission.SaveQuit),
            ("intro_vignette_quit", permission.ReturnToMap) }) {
        Scene(Level()); var job = Job(permissions: permission);
        Check(job.AllowMenuConfirmation(Menu(label).Items[0]) == allowed, "Permissions are coupled");
    }
}
// Display text, replacement callbacks and overridden ConfirmPressed cannot
// impersonate a trusted native action, even with a completed/departure grant.
foreach (bool completed in new[] { false, true }) {
    foreach (bool departure in new[] { false, true }) {
        var level = Level(); level.Completed = completed; Scene(level);
        var native = (TextMenu.Button)Menu("menu_pause_restartarea").Items[0];
        native.Label = "menu_pause_resume";
        Check(!Job(exit: departure).AllowMenuConfirmation(native), "Renaming restart made it safe");
        native.OnPressed += () => { };
        Check(!Job(exit: departure).AllowMenuConfirmation(native), "Multicast native action bypassed protection");
        native.OnPressed = () => { };
        Check(!Job(exit: departure).AllowMenuConfirmation(native), "Replaced native action bypassed protection");
        var forged = new TextMenu.Button(Dialog.Clean("menu_pause_resume")) { OnPressed = () => { } };
        Check(!Job(exit: departure).AllowMenuConfirmation(forged), "Safe text impersonated native callback");
        var safe = (TextMenu.Button)Menu("menu_pause_resume").Items[0];
        var derived = new ProbeButton(safe.Label, () => { }) { OnPressed = safe.OnPressed };
        Check(!Job(exit: departure).AllowMenuConfirmation(derived), "Derived ConfirmPressed bypassed native callback authentication");
        Scene(Level());
        Check(!Job(exit: departure).AllowMenuConfirmation(safe), "Old scene native callback acquired current owner grant");
    }
}
var introOwner = (IntroVignette)RuntimeHelpers.GetUninitializedObject(typeof(IntroVignette)); Scene(introOwner);
var introQuit = (TextMenu.Button)Menu("intro_vignette_quit").Items[0];
introQuit.OnPressed += () => { };
Check(!Job().AllowMenuConfirmation(introQuit), "Multicast intro quit bypassed independent return permission");
introQuit.OnPressed = () => { };
Check(!Job().AllowMenuConfirmation(introQuit), "Replaced intro quit bypassed independent return permission");
typeof(ChapterResetGuard).GetField("identitiesReady", flags)!.SetValue(null, false);
ChapterResetGuard.Available = true;
Check(!ChapterResetGuard.Available, "Dispatch availability hid failed native identity initialization");
Check(ChapterResetGuard.Initialize() && ChapterResetGuard.Available, "Identity initialization failed to restore capability");
Console.WriteLine("PASS: native owner/callback/type authentication rejects forged, modified and derived buttons across completion/departure/intro grants");
// Unfinished Level, no departure grant, not every action allowed: items that map to no known action pass only if
// they are known safe ones. A button a mod adds (or a renamed one) that restarts or leaves must not slip through.
foreach (var (label, ok) in new[] { ("menu_pause_resume", true), ("menu_pause_retry", true), ("menu_pause_skip_cutscene", true),
        ("intro_vignette_resume", true), ("intro_vignette_skip", true), ("menu_restart_cancel", true), ("menu_return_cancel", true),
        ("ordinary settings", false) }) {
    Scene(Level()); Feed(""); var menu = Menu(label); var job = Job(); int before = pressed;
    Feed("C"); menu.orig_Update();
    Check((pressed == before + 1) == ok && (job.StopReason == "chapter_reset_blocked") == !ok, "Unfinished-level whitelist: " + label);
}
{
    Scene(Level()); var partial = Job(permissions: new(false, true, false));
    Check(!partial.AllowMenuConfirmation(Menu("ordinary settings").Items[0]), "Partial permission opened unknown items");
    Check(partial.AllowMenuConfirmation(Menu("menu_pause_return").Items[0]), "Allowed return to map blocked");
    Scene(Level()); Check(Job(allow: true).AllowMenuConfirmation(Menu("ordinary settings").Items[0]), "All-allowed run checked unknown items");
    Scene(Level()); Check(!Job(exit: true).AllowMenuConfirmation(Menu("ordinary settings").Items[0]), "Verified departure allowed unknown items");
    var done = Level(); done.Completed = true; Scene(done);
    Check(!Job().AllowMenuConfirmation(Menu("ordinary settings").Items[0]), "Finished level allowed unknown items");
    Scene((Scene)RuntimeHelpers.GetUninitializedObject(typeof(Scene)));
    Check(Job().AllowMenuConfirmation(Menu("ordinary settings").Items[0]), "Menus outside a Level were whitelisted");
}
Scene((Scene)RuntimeHelpers.GetUninitializedObject(typeof(Scene)));
int entries = 0;
// Replace only the scene-loading effect, then wrap it with the production hook.
using var entryEffect = new Hook(typeof(OuiChapterPanel).GetMethod("Start")!,
    (Action<OuiChapterPanel, string?>)((panel, checkpoint) => { entries++; panel.Focused = false; }));
using var entryHook = new Hook(typeof(OuiChapterPanel).GetMethod("Start")!, typeof(Hooks).GetMethod("OnChapterStart", flags)!);
AreaData.Areas = new() { new AreaData { ID = 0, SID = "test/old" }, new AreaData { ID = 1, SID = "test/target" } };
SaveData.Instance = (SaveData)RuntimeHelpers.GetUninitializedObject(typeof(SaveData));
SaveData.Instance.FileSlot = 10;
OuiChapterPanel Panel(int checkpoint = 0) {
    var panel = (OuiChapterPanel)RuntimeHelpers.GetUninitializedObject(typeof(OuiChapterPanel));
    typeof(Entity).GetField("<Components>k__BackingField", flags)!.SetValue(panel, new ComponentList(panel));
    var world = (Overworld)RuntimeHelpers.GetUninitializedObject(typeof(Overworld));
    typeof(Entity).GetProperty("Scene", flags)!.SetValue(panel, world);
    world.Current = panel;
    panel.Area = new AreaKey(0); panel.initialized = true; panel.Focused = true; panel.selectingMode = false;
    panel.wiggler = Wiggler.Create(.1f, 1);
    panel.heart = (HeartGemDisplay)RuntimeHelpers.GetUninitializedObject(typeof(HeartGemDisplay));
    panel.strawberries = (StrawberriesCounter)RuntimeHelpers.GetUninitializedObject(typeof(StrawberriesCounter));
    panel.deaths = (DeathsCounter)RuntimeHelpers.GetUninitializedObject(typeof(DeathsCounter));
    panel.checkpoints = new(); panel.checkpoint = checkpoint;
    foreach (string name in new[] { "start", "test/target|checkpoint" }) {
        var option = (OuiChapterPanel.Option)RuntimeHelpers.GetUninitializedObject(typeof(OuiChapterPanel.Option));
        option.CheckpointLevelName = name; panel.checkpoints.Add(option);
    }
    return panel;
}
foreach (bool tas in new[] { false, true }) {
    Feed("", tas); var panel = Panel(); var job = Job(entry: new("test/target", 10)); int before = entries;
    Feed(tas ? "R,J" : "R,C", tas); panel.orig_Update();
    Check(panel.checkpoint == 1 && entries == before && !job.Finished && panel.Focused, "Direction+confirm was misidentified as entry");
    // Still holding confirm creates no new native edge: nothing should start.
    Feed(tas ? "J" : "C", tas); panel.orig_Update();
    Check(entries == before && !job.Finished, "Held confirmation became a new entry");
    Feed("", tas); Feed(tas ? "J" : "C", tas); panel.orig_Update();
    Check(entries == before + 1 && !job.Finished, "Checkpoint destination prefix was not resolved");
    Feed("", tas); panel = Panel(1); job = Job(entry: new("test/old", 10)); before = entries;
    Feed(tas ? "R,J" : "R,C", tas); panel.orig_Update();
    Check(job.StopReason == "entry_blocked" && entries == before && panel.Focused, "Direction at edge bypassed actual wrong-map entry guard");
}
Feed(""); var wrongSlot = Panel(1); var slotJob = Job(entry: new("test/target", 11)); int prior = entries;
Feed("C"); wrongSlot.orig_Update(); Check(slotJob.Finished && entries == prior && wrongSlot.Focused, "Wrong-slot entry executed side effects");
foreach (string invalid in new[] {
    "{\"version\":2}", "{\"version\":true}", "{\"version\":\"1\"}",
    "{\"version\":1,\"permissions\":{\"restart_chapter\":false,\"return_to_map\":false,\"save_quit\":false,\"unknown\":true},\"departure\":\"none\"}",
    "{\"version\":1,\"permissions\":{\"restart_chapter\":1,\"return_to_map\":false,\"save_quit\":false},\"departure\":\"none\"}",
    "{\"version\":1,\"permissions\":{\"restart_chapter\":false,\"return_to_map\":false,\"save_quit\":false},\"departure\":\"always\"}",
}) {
    using var data = System.Text.Json.JsonDocument.Parse("{\"action_policy\":" + invalid + "}");
    try { ActionPolicy.Read(data.RootElement); throw new Exception("Malformed policy accepted"); }
    catch (FormatException) { }
}
using (var data = System.Text.Json.JsonDocument.Parse("""
    {"allow_chapter_reset":true,"allow_map_exit":true,"action_policy":{"version":1,
     "permissions":{"restart_chapter":false,"return_to_map":false,"save_quit":false},"departure":"none",
     "entry_target":{"sid":"test/target","slot":10}}}
    """)) {
    var policy = ActionPolicy.Read(data.RootElement);
    Check(!policy.Permissions.RestartChapter && policy.CheckMenu(Menu("menu_pause_savequit").Items[0]) != null,
        "Legacy arguments overrode canonical permissions");
    Check(policy.Entry?.Check("wrong", 10)?.RuleId == "target.map", "Canonical mandatory target missing");
}
Console.WriteLine("PASS: production IL hook + real TextMenu + virtual buttons + StepJob: native/TAS resets, secondary confirmations, safe callbacks, simultaneous direction+confirm, configured allow and verified departure");
Console.WriteLine("PASS: cross-Level/loading grants revoked, split permissions, actual Panel direction priority/held input/checkpoint destination/save-slot entry hooks");
Console.WriteLine("PASS: missing menu injection point only disables the guard; unfinished-level whitelist (partial/all/departure/finished/outside Level)");

// Exercise both production tick paths. Only external input-binding, audio and
// snapshot/IPC boundaries are replaced; loading, jobs, accounting and freezing
// keep their production implementations.
using (var noInstall = new Hook(typeof(InputInjector).GetMethod("Install", flags)!, (Action)(() => { })))
using (var noUninstall = new Hook(typeof(InputInjector).GetMethod("Uninstall", flags)!, (Action)(() => { })))
using (var noAudio = new Hook(typeof(Bridge).GetMethod("SetAudio", flags)!, (Action<Bridge.AudioMode>)(_ => { })))
using (var snapshot = new Hook(typeof(StateExtractor).GetMethod("WriteSnapshot", flags)!,
        (Action<System.Text.Json.Utf8JsonWriter, SnapshotOptions>)((w, _) => w.WriteString("scene", Engine.Scene.GetType().Name)))) {
    string? received = null;
    using var send = new Hook(typeof(IpcServer).GetMethod("Send", flags)!, (Action<string>)(s => received = s));
    using var tokens = new Hook(typeof(ClockServer).GetMethod("TryTakeToken", flags)!,
        (TestToken)((int _, out long token) => { token = 1; return true; }));
    typeof(Bridge).GetProperty("Active", flags)!.SetValue(null, true);
    foreach (bool locked in new[] { false, true }) {
        Scene((LevelLoader)RuntimeHelpers.GetUninitializedObject(typeof(LevelLoader)));
        var job = new StepJob(99, new() { InputFrame.Neutral, InputFrame.Parse("C"), InputFrame.Neutral },
            new[] { "chapter_loaded" }, 4, false, "level", new SnapshotOptions(), false);
        typeof(Bridge).GetField("job", flags)!.SetValue(null, job);
        var loaded = Level(); int updates = 0; received = null;
        typeof(Bridge).GetMethod(locked ? "LockstepTick" : "NormalTick", flags)!.Invoke(null,
            new object[] { (Action)(() => { updates++; Scene(loaded); }) });
        Check(updates == 1 && job.Finished && job.StopReason == "chapter_loaded" && Bridge.Frozen,
            "Loading failed to freeze before leftover menu confirmation: " + locked);
        using var response = System.Text.Json.JsonDocument.Parse(received!);
        var result = response.RootElement.GetProperty("result");
        Check(result.GetProperty("consumed").GetInt32() == 0 && job.LoadingFrames == 1,
            "Loading consumed leftover menu input: " + locked);
        Check(!job.AllowNextInput(), "Frozen job still accepts menu input");
        typeof(Bridge).GetField("pendingAck", flags)!.SetValue(null, null);
    }
    foreach (Scene loaded in new Scene[] { Level(),
            (IntroVignette)RuntimeHelpers.GetUninitializedObject(typeof(IntroVignette)),
            (LevelEnter)RuntimeHelpers.GetUninitializedObject(typeof(LevelEnter)) }) {
        Scene(loaded);
        var job = new StepJob(100, new() { InputFrame.Parse("C") }, new[] { "chapter_loaded" }, 1,
            false, "level", new SnapshotOptions(), false);
        Check(job.Finished && job.StopReason == "chapter_loaded", "Existing chapter did not stop before input");
    }
    foreach (bool locked in new[] { false, true }) {
        foreach (Scene loading in new Scene[] {
                (LevelExit)RuntimeHelpers.GetUninitializedObject(typeof(LevelExit)),
                (LevelLoader)RuntimeHelpers.GetUninitializedObject(typeof(LevelLoader)),
                (OverworldLoader)RuntimeHelpers.GetUninitializedObject(typeof(OverworldLoader)) })
        foreach (Scene loaded in new Scene[] { Level(),
                (AreaComplete)RuntimeHelpers.GetUninitializedObject(typeof(AreaComplete)),
                (Overworld)RuntimeHelpers.GetUninitializedObject(typeof(Overworld)) }) {
            Scene(loading);
            var job = new StepJob(101, new() { InputFrame.Parse("C") }, new[] { "reload_ready" }, 4,
                false, "level", new SnapshotOptions(), false);
            typeof(Bridge).GetField("job", flags)!.SetValue(null, job);
            int updates = 0; received = null;
            typeof(Bridge).GetMethod(locked ? "LockstepTick" : "NormalTick", flags)!.Invoke(null,
                new object[] { (Action)(() => { updates++; Scene(loaded); }) });
            using var response = System.Text.Json.JsonDocument.Parse(received!);
            Check(updates == 1 && job.Finished && job.StopReason == "reload_ready" && job.LoadingFrames == 1 &&
                response.RootElement.GetProperty("result").GetProperty("consumed").GetInt32() == 0,
                "Reload boundary consumed input or advanced the loaded scene: " + loaded.GetType().Name);
            typeof(Bridge).GetField("pendingAck", flags)!.SetValue(null, null);
            var alreadyLoaded = new StepJob(102, new() { InputFrame.Parse("C") }, new[] { "reload_ready" }, 1,
                false, "level", new SnapshotOptions(), false);
            Check(alreadyLoaded.StopReason == "reload_ready", "Already loaded scene failed to stop before input");
        }
    }
    // Native save completion belongs to Celeste.Update, before Engine.Update.
    // Maintenance must let that outer update finish without consuming a menu
    // placeholder or advancing gameplay/recording while disk I/O is pending.
    foreach (bool locked in new[] { false, true })
    foreach (Scene savingScene in new Scene[] { Level(), (Overworld)RuntimeHelpers.GetUninitializedObject(typeof(Overworld)) }) {
        Scene(savingScene);
        typeof(UserIO).GetProperty("Saving", flags)!.SetValue(null, true);
        var waiting = new StepJob(103, new() { InputFrame.Parse("C") }, new[] { "reload_ready" }, 4,
            false, "level", new SnapshotOptions(), false);
        typeof(Bridge).GetField("job", flags)!.SetValue(null, waiting);
        Check(waiting.WaitingForReloadSave && !waiting.Finished, "Pending save did not enter maintenance wait");
        long frame = Bridge.Frame; received = null;
        typeof(Bridge).GetMethod(locked ? "LockstepTick" : "NormalTick", flags)!.Invoke(null,
            new object[] { (Action)(() => {
                Check(Bridge.ShouldHaltEngineUpdate(), "Saving wait advanced the scene");
                typeof(UserIO).GetProperty("Saving", flags)!.SetValue(null, false);
            }) });
        using var response = System.Text.Json.JsonDocument.Parse(received!);
        Check(waiting.StopReason == "reload_ready" && waiting.LoadingFrames == 0 && Bridge.Frame == frame &&
            response.RootElement.GetProperty("result").GetProperty("consumed").GetInt32() == 0,
            "Save completion consumed placeholder input or recorded a frame");
    }
    typeof(Bridge).GetField("job", flags)!.SetValue(null, null);
    foreach (bool locked in new[] { false, true }) {
        var loader = (OverworldLoader)RuntimeHelpers.GetUninitializedObject(typeof(OverworldLoader));
        typeof(Scene).GetField("<Entities>k__BackingField", flags)!.SetValue(loader, new EntityList(loader));
        var card = (Postcard)RuntimeHelpers.GetUninitializedObject(typeof(Postcard));
        card.Visible = true;
        loader.postcard = card;
        Scene(loader);
        var wait = new StepJob(108, new() { InputFrame.Parse("C") }, new[] { "reload_ready" }, 4,
            false, "level", new SnapshotOptions(), false);
        typeof(Bridge).GetField("job", flags)!.SetValue(null, wait);
        long frame = Bridge.Frame; int updates = 0; received = null;
        typeof(Bridge).GetMethod(locked ? "LockstepTick" : "NormalTick", flags)!.Invoke(null,
            new object[] { (Action)(() => {
                updates++; loader.loaded = true;
                typeof(Entity).GetProperty("Scene", flags)!.SetValue(card, loader);
            }) });
        using var response = System.Text.Json.JsonDocument.Parse(received!);
        Check(updates == 1 && wait.StopReason == "reload_ready" && wait.LoadingFrames == 1 &&
            response.RootElement.GetProperty("result").GetProperty("consumed").GetInt32() == 0,
            "Loader postcard consumed its placeholder confirmation");
        typeof(Bridge).GetField("pendingAck", flags)!.SetValue(null, null);
        Check(Bridge.Frozen && Bridge.OwnsInput && !Bridge.IsLoading() && ReloadBoundary.Capture().requires_origin_hold,
            "Stable loader presentation did not become a controlled preserve boundary");
        typeof(Bridge).GetMethod(locked ? "LockstepTick" : "NormalTick", flags)!.Invoke(null,
            new object[] { (Action)(() => Check(Bridge.ShouldHaltEngineUpdate(), "Idle loader presentation advanced")) });
        Check(Bridge.Frame == frame + 1, "Idle loader presentation advanced a recording frame");
        loader.Entities.current.Add(card);
        card.alpha = 0;
        Check(ReloadBoundary.LoaderPresentation(loader), "Zero-alpha attached animation lost its preserve boundary");
        card.RemoveSelf();
        Check(ReferenceEquals(card.Scene, loader) && loader.Entities.toRemove.Contains(card) && !ReloadBoundary.LoaderPrompt(loader) &&
            Bridge.IsLoading(), "Queued postcard removal froze the update needed to detach it");
        typeof(Entity).GetProperty("Scene", flags)!.SetValue(card, null);
        Check(!ReloadBoundary.LoaderPresentation(loader) && Bridge.IsLoading(), "Removed postcard retained a stale preserve boundary");
    }
    Console.WriteLine("PASS: background loader becomes a controlled postcard boundary at zero consumed input, then remains frozen until explicit action");
    foreach (bool locked in new[] { false, true }) {
        var loader = (OverworldLoader)RuntimeHelpers.GetUninitializedObject(typeof(OverworldLoader));
        typeof(Scene).GetField("<Entities>k__BackingField", flags)!.SetValue(loader, new EntityList(loader));
        var card = (Postcard)RuntimeHelpers.GetUninitializedObject(typeof(Postcard));
        card.Visible = true; loader.postcard = card;
        Scene(loader);
        var menuWait = new StepJob(109, new() { InputFrame.Parse("C") }, new[] { "menu_prompt", "chapter_loaded" }, 4,
            false, "level", new SnapshotOptions(), false);
        typeof(Bridge).GetField("job", flags)!.SetValue(null, menuWait);
        int updates = 0; received = null;
        typeof(Bridge).GetMethod(locked ? "LockstepTick" : "NormalTick", flags)!.Invoke(null,
            new object[] { (Action)(() => {
                updates++; typeof(Entity).GetProperty("Scene", flags)!.SetValue(card, loader);
            }) });
        using var response = System.Text.Json.JsonDocument.Parse(received!);
        Check(updates == 1 && menuWait.StopReason == "menu_prompt" && menuWait.LoadingFrames == 1 &&
            response.RootElement.GetProperty("result").GetProperty("consumed").GetInt32() == 0 && !loader.loaded,
            "Menu macro consumed a retained confirm at the first attached loader prompt");
        typeof(Bridge).GetField("pendingAck", flags)!.SetValue(null, null);
        var alreadyPrompt = new StepJob(110, new() { InputFrame.Parse("C") }, new[] { "menu_prompt" }, 4,
            false, "level", new SnapshotOptions(), false);
        Check(alreadyPrompt.StopReason == "menu_prompt", "Existing loader prompt did not stop menu macro at zero input");
        var legacyLoad = new StepJob(111, new() { InputFrame.Parse("C") }, new[] { "chapter_loaded" }, 4,
            false, "level", new SnapshotOptions(), false);
        Check(legacyLoad.StopReason == "chapter_loaded", "Legacy chapter boundary missed an attached loader prompt");
    }
    typeof(Bridge).GetField("job", flags)!.SetValue(null, null);
    Console.WriteLine("PASS: menu-specific prompt stops discard no input and never confirm an attached loader card, even before background loading finishes");
    foreach (Scene preserved in new Scene[] {
            (Overworld)RuntimeHelpers.GetUninitializedObject(typeof(Overworld)),
            (LevelEnter)RuntimeHelpers.GetUninitializedObject(typeof(LevelEnter)),
            (IntroVignette)RuntimeHelpers.GetUninitializedObject(typeof(IntroVignette)),
            (Scene)RuntimeHelpers.GetUninitializedObject(typeof(Scene)) }) {
        Scene(preserved); SaveData.Instance = null;
        var boundary = ReloadBoundary.Capture();
        Check(boundary.mode == "preserve" && boundary.save == null && boundary.session == null,
            "Non-Level scene requires a save/session or gets room replacement");
        using var acquireArgs = System.Text.Json.JsonDocument.Parse("{\"enabled\":true}");
        typeof(Bridge).GetMethod("Handle", flags)!.Invoke(null, new object[] { new Request(104, "reload_boundary", acquireArgs.RootElement) });
        using var acquireReply = System.Text.Json.JsonDocument.Parse(received!);
        Check(acquireReply.RootElement.GetProperty("acquired").GetBoolean() && Bridge.Frozen && Bridge.OwnsInput && Bridge.ShouldHaltEngineUpdate(),
            "Menu maintenance boundary failed to freeze/own input");
        long frame = Bridge.Frame;
        typeof(Bridge).GetMethod("NormalTick", flags)!.Invoke(null, new object[] { (Action)(() => Check(Bridge.ShouldHaltEngineUpdate(), "Boundary advanced menu")) });
        Check(Bridge.Frame == frame, "Boundary incremented menu clock");
        string exported = Bridge.ExportReloadState();
        Check(!(bool)typeof(Bridge).GetField("reloadBoundaryHeld", flags)!.GetValue(null)!, "Export retained the payload boundary after transfer");
        Bridge.ImportReloadState(exported);
        using var identity = System.Text.Json.JsonDocument.Parse(exported);
        Check(identity.RootElement.GetProperty("identity").GetProperty("scene").GetString() == preserved.GetType().FullName,
            "Scene identity lost its full type");
        Scene((OverworldLoader)RuntimeHelpers.GetUninitializedObject(typeof(OverworldLoader)));
        try { Bridge.ImportReloadState(exported); throw new Exception("Changed reload identity was accepted"); }
        catch (InvalidOperationException) { }
    }
    Scene((Overworld)RuntimeHelpers.GetUninitializedObject(typeof(Overworld)));
    using (var acquireArgs = System.Text.Json.JsonDocument.Parse("{\"enabled\":true}"))
    using (var releaseArgs = System.Text.Json.JsonDocument.Parse("{\"enabled\":false}")) {
        typeof(Bridge).GetMethod("Handle", flags)!.Invoke(null, new object[] { new Request(105, "reload_boundary", acquireArgs.RootElement) });
        typeof(Bridge).GetMethod("Handle", flags)!.Invoke(null, new object[] { new Request(106, "reload_boundary", releaseArgs.RootElement) });
        Check(!Bridge.Frozen && !Bridge.OwnsInput, "Release retained menu freeze or input capture");
        typeof(UserIO).GetProperty("Saving", flags)!.SetValue(null, true);
        typeof(Bridge).GetMethod("Handle", flags)!.Invoke(null, new object[] { new Request(107, "reload_boundary", acquireArgs.RootElement) });
        using var busyReply = System.Text.Json.JsonDocument.Parse(received!);
        Check(!busyReply.RootElement.GetProperty("acquired").GetBoolean() && !Bridge.Frozen,
            "Acquisition froze a pending save before it could finish");
        typeof(UserIO).GetProperty("Saving", flags)!.SetValue(null, false);
    }
    Scene(Level());
    Check(ReloadBoundary.Capture().mode == "replace", "Level failed native replacement policy");
    Scene((LevelLoader)RuntimeHelpers.GetUninitializedObject(typeof(LevelLoader)));
    Check(ReloadBoundary.Capture().mode == "wait" && !Bridge.CanReload(), "Loader was allowed to reload");
    typeof(Bridge).GetProperty("Active", flags)!.SetValue(null, false);
}
Console.WriteLine("PASS: save waits halt scenes without consuming/recording input; nullable menu/entry identity and short maintenance freeze transfer verified");
Console.WriteLine("PASS: production normal/lockstep loading ticks freeze at first chapter scene, preserve input plan and loading accounting");
Console.WriteLine("PASS: reload_ready stops LevelLoader/LevelExit/OverworldLoader to Level/AreaComplete/Overworld before consuming confirmation in normal and lockstep ticks");

// Install production hooks on both actual PICO-8 entry methods. The blocked path
// uses the original methods, with no substituted interaction/cutscene callback.
typeof(Hooks).GetMethod("InstallPico8Guard", flags)!.Invoke(null, null);
Check(Hooks.Pico8GuardAvailable, "PICO-8 entry hooks unavailable");
var console = (PicoConsole)RuntimeHelpers.GetUninitializedObject(typeof(PicoConsole));
var main = (OuiMainMenu)RuntimeHelpers.GetUninitializedObject(typeof(OuiMainMenu));
var interact = typeof(PicoConsole).GetMethod("OnInteract", flags)!;
var menuEntry = typeof(OuiMainMenu).GetMethod("OnPico8", flags)!;
foreach (bool fromConsole in new[] { true, false }) {
    var level = Level(); level.PauseLock = false; Scene(level);
    var job = Job(); job.NextInput(); main.Focused = true;
    Settings.Instance.Pico8OnMainMenu = false;
    typeof(Bridge).GetProperty("Active", flags)!.SetValue(null, true);
    if (fromConsole) interact.Invoke(console, new object?[] { null });
    else menuEntry.Invoke(main, null);
    Check(job.StopReason == "unsupported_scene_blocked" && !job.AllowNextInput(), "PICO entry did not stop plan");
    Check(ReferenceEquals(Engine.Scene, level) && !level.PauseLock && main.Focused &&
        !(bool)typeof(PicoConsole).GetField("talking", flags)!.GetValue(console)! &&
        !Settings.Instance.Pico8OnMainMenu, "PICO entry applied side effects before refusal");
    using var buffer = new MemoryStream();
    using (var writer = new System.Text.Json.Utf8JsonWriter(buffer)) {
        writer.WriteStartObject(); job.WriteResult(writer); writer.WriteEndObject();
    }
    using var result = System.Text.Json.JsonDocument.Parse(buffer.ToArray());
    Check(result.RootElement.GetProperty("consumed").GetInt32() == 1 &&
        result.RootElement.GetProperty("block").GetProperty("action").GetString() == "enter_pico8",
        "PICO block response lost accounting or reason");
    Console.WriteLine("PICO_BLOCK_JSON:" + System.Text.Encoding.UTF8.GetString(buffer.ToArray()));
}
// The manual console path with talking=true is a safe native no-op. It must
// bypass the guard both when inactive and when no action is pending.
typeof(PicoConsole).GetField("talking", flags)!.SetValue(console, true);
var manualJob = Job();
typeof(Bridge).GetProperty("Active", flags)!.SetValue(null, false);
interact.Invoke(console, new object?[] { null });
Check(manualJob.StopReason == null, "Inactive bridge blocked manual console");
// Stop manual menu entry at the external audio boundary, before graphics/wipes.
int menuAudio = 0;
using (var audioProbe = new Hook(typeof(Audio).GetMethod("Play", new[] { typeof(string) })!,
        (Func<string, FMOD.Studio.EventInstance>)(_ => { menuAudio++; throw new InvalidOperationException("manual menu reached audio"); }))) {
    try { menuEntry.Invoke(main, null); }
    catch (TargetInvocationException e) when (e.InnerException?.Message == "manual menu reached audio") { }
    Check(menuAudio == 1 && manualJob.StopReason == null, "Inactive bridge blocked manual main-menu entry");
}
typeof(Bridge).GetProperty("Active", flags)!.SetValue(null, true);
typeof(Bridge).GetField("job", flags)!.SetValue(null, null);
Check(Bridge.AllowPico8Entry(), "Idle bridge blocked manual menu");
interact.Invoke(console, new object?[] { null });
typeof(Bridge).GetProperty("Active", flags)!.SetValue(null, false);
Console.WriteLine("PASS: both native PICO-8 entry hooks stop before side effects, preserve plan accounting and manual control");

sealed class ProbeButton(string label, Action onConfirm) : TextMenu.Button(label) {
    public override void ConfirmPressed() => onConfirm();
}

delegate bool TestToken(int timeout, out long token);
