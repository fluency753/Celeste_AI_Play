using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Celeste;
using Celeste.Mod;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using Monocle;
using MonoMod.RuntimeDetour;
using CelesteSettings = Celeste.Settings;
using GameInput = Celeste.Input;

namespace CelesteAI.Bridge;

/// <summary>
/// 接管期间改用一套固定的按键绑定，再逐帧写入合成的手柄/键盘状态。
/// 绑定思路参考 CelesteTAS（MIT）：先改绑定、重建虚拟输入，再把设置字段还原，
/// 这样玩家自己的按键设置不会被保存成桥接用的绑定。
/// </summary>
public static class InputInjector {
    // 游戏内动作：手柄
    private const Buttons JumpButton = Buttons.A;
    private const Buttons DashButton = Buttons.B;
    private const Buttons GrabButton = Buttons.X;
    private const Buttons TalkButton = Buttons.Y;
    private const Buttons CrouchDashButton = Buttons.RightShoulder;
    // 第二套键（摇杆按下与左肩键在接管期间没有别的绑定）
    private const Buttons Jump2Button = Buttons.LeftShoulder;
    private const Buttons Dash2Button = Buttons.LeftStick;
    private const Buttons CrouchDash2Button = Buttons.RightStick;

    // 菜单动作：键盘，避免与游戏内动作共用按钮
    private const Keys ConfirmKey = Keys.NumPad0;
    private const Keys BackKey = Keys.NumPad1;
    private const Keys PauseKey = Keys.NumPad2;
    private const Keys JournalKey = Keys.NumPad3;
    // These independent channels preserve TAS shared bindings without making
    // native J confirm menus or native X cancel them.
    private const Keys TasJump = Keys.F13, TasJump2 = Keys.F14;
    private const Keys TasDash = Keys.F15, TasDash2 = Keys.F16;
    private const Keys TasGrab = Keys.F17, TasGrab2 = Keys.F18;
    private const Keys TasJournal = Keys.F19, TasPause = Keys.F20, TasRestart = Keys.F21;

    private static bool installed;
    private static GrabModes origGrabMode;
    private static CrouchDashModes origCrouchDashMode;
    private static bool origControllerHasFocus;
    private static bool origAttached;
    private static readonly List<(VirtualButton button, Binding original)> ModBindingBackup = new();

    // Everest 每次注册 Mod（包括受控重载后的新桥接）都会调用 Input.Initialize，按玩家设置重建全部虚拟按键，
    // 并让其他 Mod 的快捷键重新生效。接管期间要在这之后重新套用桥接绑定，否则合成输入会按玩家自己的绑定解释。
    private delegate void OrigInitialize();
    private static Hook? initializeHook;
    [ThreadStatic] private static int ownInitialize;   // 本类自己调用 Input.Initialize 时不再触发重新套用
    private static volatile bool reinitPending;         // 非主线程上的 Initialize：交给下一次主线程节拍处理
    private static readonly Dictionary<string, Binding> Expected = new();
    // 核对的游戏按键：设置字段名 → 游戏里实际使用的虚拟按键
    private static readonly (string Setting, string Name, Func<VirtualButton?> Live)[] Checked = {
        ("Jump", "Jump", () => GameInput.Jump),
        ("Dash", "Dash", () => GameInput.Dash),
        ("Grab", "Grab", () => GameInput.Grab),
        ("Talk", "Talk", () => GameInput.Talk),
        ("DemoDash", "CrouchDash", () => GameInput.CrouchDash),
        ("Confirm", "MenuConfirm", () => GameInput.MenuConfirm),
        ("Cancel", "MenuCancel", () => GameInput.MenuCancel),
        ("Pause", "Pause", () => GameInput.Pause),
        ("Journal", "MenuJournal", () => GameInput.MenuJournal),
    };

    public static bool Installed => installed;
    /// <summary>Input.Initialize 的重新套用钩子已安装（核心 1.0.2 起）。</summary>
    public static bool ReinitializeHooked => initializeHook != null;
    /// <summary>接管期间因外部 Input.Initialize 重新套用桥接绑定的次数。</summary>
    public static int Reapplied { get; private set; }

    public static void Load() {
        try {
            MethodInfo? initialize = typeof(GameInput).GetMethod("Initialize", BindingFlags.Public | BindingFlags.Static,
                null, Type.EmptyTypes, null);
            if (initialize == null) {
                Log.Error("Input.Initialize not found; bridge bindings cannot be re-applied after a Mod reload");
                return;
            }
            initializeHook = new Hook(initialize,
                typeof(InputInjector).GetMethod(nameof(OnInitialize), BindingFlags.Static | BindingFlags.NonPublic)!);
        } catch (Exception ex) {
            initializeHook = null;
            Log.Error("installing the Input.Initialize hook failed: " + ex);
        }
    }

    public static void Unload() {
        initializeHook?.Dispose();
        initializeHook = null;
        reinitPending = false;
    }

    private static void OnInitialize(OrigInitialize orig) {
        orig();
        if (ownInitialize > 0) return;
        if (!MainThreadHelper.IsMainThread) {
            // Mod 重载的工作线程：此时主线程可能正在喂输入，留到主线程节拍开头再套用
            reinitPending = true;
        } else if (installed) {
            Reapply("Input.Initialize");
        }
    }

    /// <summary>主线程调用：补做其他线程上的 Input.Initialize 之后应当进行的重新套用。</summary>
    public static void ServicePending() {
        if (!reinitPending) return;
        reinitPending = false;
        if (installed) Reapply("Input.Initialize on a worker thread");
    }

    private static void Reapply(string reason) {
        try {
            ApplyBindings();
            Reapplied++;
            Log.Info($"input bindings re-applied after {reason}");
        } catch (Exception ex) {
            Log.Error("re-applying input bindings failed: " + ex);
        }
    }

    private static void InitializeOwn() {
        ownInitialize++;
        try {
            GameInput.Initialize();
        } finally {
            ownInitialize--;
        }
    }

    /// <summary>按桥接绑定重建虚拟按键并清空其他 Mod 的快捷键；不改模式设置，也不改玩家保存的绑定。</summary>
    private static void ApplyBindings() {
        CelesteSettings settings = CelesteSettings.Instance;
        FieldInfo[] bindingFields = typeof(CelesteSettings)
            .GetFields(BindingFlags.Public | BindingFlags.Instance)
            .Where(f => f.FieldType == typeof(Binding))
            .ToArray();
        var backup = bindingFields.ToDictionary(f => f, f => f.GetValue(settings));
        reinitPending = false;   // 此后其他线程再调用 Initialize 仍会重新标记

        try {
            foreach (FieldInfo f in bindingFields) {
                f.SetValue(settings, new Binding());
            }
            ConfigureBindings(settings);
            Expected.Clear();
            foreach (var (setting, _, _) in Checked) {
                if (typeof(CelesteSettings).GetField(setting)?.GetValue(settings) is Binding expected) Expected[setting] = expected;
            }

            InitializeOwn();
        } finally {
            // 还原设置字段：虚拟输入已按上面的绑定建好，玩家的设置文件保持不变
            foreach (var (field, value) in backup) {
                field.SetValue(settings, value);
            }
        }

        ClearModBindings();
    }

    /// <summary>接管期间实际生效的游戏按键与桥接绑定不一致的项；未接管时为空。</summary>
    public static string[] BindingMismatches() {
        if (!installed) return Array.Empty<string>();
        var result = new List<string>();
        foreach (var (setting, name, live) in Checked) {
            Binding? actual = live()?.Binding;
            if (!Expected.TryGetValue(setting, out Binding? expected) || actual == null || !SameBinding(expected, actual))
                result.Add(name);
        }
        foreach (string mod in LiveModBindings()) result.Add("mod:" + mod);
        return result.ToArray();
    }

    private static bool SameBinding(Binding expected, Binding actual) =>
        ReferenceEquals(expected, actual) ||
        new HashSet<Buttons>(expected.Controller).SetEquals(actual.Controller) &&
        new HashSet<Keys>(expected.Keyboard).SetEquals(actual.Keyboard);

    public static void Install() {
        if (installed) {
            return;
        }

        CelesteSettings settings = CelesteSettings.Instance;
        ApplyBindings();

        origGrabMode = settings.GrabMode;
        origCrouchDashMode = settings.CrouchDashMode;
        settings.GrabMode = GrabModes.Hold;
        settings.CrouchDashMode = CrouchDashModes.Press;

        origControllerHasFocus = MInput.ControllerHasFocus;
        MInput.ControllerHasFocus = true;
        MInput.Active = true;
        MInput.Disabled = false;
        origAttached = MInput.GamePads[GameInput.Gamepad].Attached;
        MInput.GamePads[GameInput.Gamepad].Attached = true;

        installed = true;
        Log.Info("input bindings installed");
    }

    public static void Uninstall() {
        if (!installed) {
            return;
        }

        CelesteSettings settings = CelesteSettings.Instance;
        settings.GrabMode = origGrabMode;
        settings.CrouchDashMode = origCrouchDashMode;
        InitializeOwn();

        // Input.Initialize 已按 Mod 设置重建了它们的按键；这里再把仍在用的旧按键对象还原，两种情况都回到玩家绑定
        foreach (var (button, original) in ModBindingBackup) {
            button.Binding = original;
        }
        ModBindingBackup.Clear();

        MInput.ControllerHasFocus = origControllerHasFocus;
        MInput.GamePads[GameInput.Gamepad].Attached = origAttached;

        installed = false;
        reinitPending = false;
        Expected.Clear();
        Log.Info("input bindings restored");
    }

    internal static void ConfigureBindings(CelesteSettings settings) {
        Set(settings, "Left", Buttons.DPadLeft, Buttons.LeftThumbstickLeft);
        Set(settings, "Right", Buttons.DPadRight, Buttons.LeftThumbstickRight);
        Set(settings, "Up", Buttons.DPadUp, Buttons.LeftThumbstickUp);
        Set(settings, "Down", Buttons.DPadDown, Buttons.LeftThumbstickDown);
        Set(settings, "MenuLeft", Buttons.DPadLeft, Buttons.LeftThumbstickLeft);
        Set(settings, "MenuRight", Buttons.DPadRight, Buttons.LeftThumbstickRight);
        Set(settings, "MenuUp", Buttons.DPadUp, Buttons.LeftThumbstickUp);
        Set(settings, "MenuDown", Buttons.DPadDown, Buttons.LeftThumbstickDown);
        Set(settings, "Jump", JumpButton, Jump2Button);
        Set(settings, "Dash", DashButton, Dash2Button);
        Set(settings, "Grab", GrabButton);
        Set(settings, "Talk", TalkButton);
        Set(settings, "DemoDash", CrouchDashButton, CrouchDash2Button);
        SetKey(settings, "Confirm", ConfirmKey);
        SetKey(settings, "Cancel", BackKey);
        SetKey(settings, "Pause", PauseKey);
        SetKey(settings, "Journal", JournalKey);
        AddKeys(settings, "Jump", TasJump, TasJump2);
        AddKeys(settings, "Dash", TasDash, TasDash2);
        AddKeys(settings, "Grab", TasGrab, TasGrab2);
        AddKeys(settings, "Talk", TasDash, TasJournal);
        AddKeys(settings, "Confirm", TasJump);
        AddKeys(settings, "Cancel", TasDash, TasDash2);
        AddKeys(settings, "Pause", TasPause);
        AddKeys(settings, "Journal", TasJournal);
        SetKey(settings, "QuickRestart", TasRestart);

    }

    /// <summary>写入一帧的合成输入并更新所有虚拟按键（含缓冲计时）。</summary>
    public static void Feed(InputFrame input) {
        GamePadDPad dpad;
        Vector2 stick = Vector2.Zero;
        if (input.AngleDeg is { } deg) {
            float rad = MathHelper.ToRadians(deg);
            stick = new Vector2(MathF.Cos(rad), MathF.Sin(rad)); // 手柄 Y 轴向上为正
            dpad = new GamePadDPad(ButtonState.Released, ButtonState.Released, ButtonState.Released, ButtonState.Released);
        } else {
            dpad = new GamePadDPad(
                input.Has(Act.Up) ? ButtonState.Pressed : ButtonState.Released,
                input.Has(Act.Down) ? ButtonState.Pressed : ButtonState.Released,
                input.Has(Act.Left) ? ButtonState.Pressed : ButtonState.Released,
                input.Has(Act.Right) ? ButtonState.Pressed : ButtonState.Released);
        }

        Buttons buttons = 0;
        if (!input.Tas && input.Has(Act.Jump)) buttons |= JumpButton;
        if (!input.Tas && input.Has(Act.Dash)) buttons |= DashButton;
        if (!input.Tas && input.Has(Act.Grab)) buttons |= GrabButton;
        if (input.Has(Act.Talk)) buttons |= TalkButton;
        if (input.Has(Act.CrouchDash)) buttons |= CrouchDashButton;
        if (!input.Tas && input.Has(Act.Jump2)) buttons |= Jump2Button;
        if (!input.Tas && input.Has(Act.Dash2)) buttons |= Dash2Button;
        if (input.Has(Act.CrouchDash2)) buttons |= CrouchDash2Button;
        if (input.AngleDeg == null) {
            if (input.Has(Act.Up)) buttons |= Buttons.DPadUp;
            if (input.Has(Act.Down)) buttons |= Buttons.DPadDown;
            if (input.Has(Act.Left)) buttons |= Buttons.DPadLeft;
            if (input.Has(Act.Right)) buttons |= Buttons.DPadRight;
        }

        var state = new GamePadState(
            new GamePadThumbSticks(stick, Vector2.Zero),
            new GamePadTriggers(0f, 0f),
            new GamePadButtons(buttons),
            dpad);

        MInput.GamePadData pad = MInput.GamePads[GameInput.Gamepad];
        pad.PreviousState = pad.CurrentState;
        pad.CurrentState = state;

        var keys = new List<Keys>(4);
        if (input.Has(Act.Confirm)) keys.Add(ConfirmKey);
        if (input.Has(Act.Back)) keys.Add(BackKey);
        if (input.Has(Act.Pause)) keys.Add(input.Tas ? TasPause : PauseKey);
        if (input.Has(Act.Journal)) keys.Add(input.Tas ? TasJournal : JournalKey);
        if (input.Tas) {
            if (input.Has(Act.Jump)) keys.Add(TasJump);
            if (input.Has(Act.Jump2)) keys.Add(TasJump2);
            if (input.Has(Act.Dash)) keys.Add(TasDash);
            if (input.Has(Act.Dash2)) keys.Add(TasDash2);
            if (input.Has(Act.Grab)) keys.Add(TasGrab);
            if (input.Has(Act.Grab2)) keys.Add(TasGrab2);
            if (input.Has(Act.Restart)) keys.Add(TasRestart);
        }
        MInput.Keyboard.PreviousState = MInput.Keyboard.CurrentState;
        MInput.Keyboard.CurrentState = new KeyboardState(keys.ToArray());

        MInput.UpdateVirtualInputs();
    }

    private static void Set(CelesteSettings settings, string field, params Buttons[] buttons) {
        var binding = new Binding();
        binding.Add(buttons);
        SetField(settings, field, binding);
    }

    private static void SetKey(CelesteSettings settings, string field, params Keys[] keys) {
        var binding = new Binding();
        binding.Add(keys);
        SetField(settings, field, binding);
    }

    private static void AddKeys(CelesteSettings settings, string field, params Keys[] keys) {
        var binding = (Binding?)typeof(CelesteSettings).GetField(field)?.GetValue(settings);
        if (binding == null) throw new InvalidOperationException($"Missing TAS binding {field}");
        binding.Add(keys);
    }

    private static void SetField(CelesteSettings settings, string field, Binding binding) {
        FieldInfo? f = typeof(CelesteSettings).GetField(field, BindingFlags.Public | BindingFlags.Instance);
        if (f == null) {
            Log.Warn($"settings field '{field}' not found; binding skipped");
            return;
        }
        f.SetValue(settings, binding);
    }

    // 其他 Mod 的快捷键（例如存读档工具）可能绑在手柄按钮上，接管期间全部清空，退出时还原
    private static void ClearModBindings() {
        var previous = ModBindingBackup.ToList();
        ModBindingBackup.Clear();
        foreach (var (_, button) in ModButtons()) {
            // 只改运行时的虚拟按键，不动设置里保存的绑定。重新套用时同一个按键对象已被清空过，
            // 沿用第一次备份的玩家绑定；Input.Initialize 新建的按键对象则备份它当前（玩家）的绑定。
            int known = previous.FindIndex(entry => ReferenceEquals(entry.button, button));
            ModBindingBackup.Add((button, known >= 0 ? previous[known].original : button.Binding));
            button.Binding = new Binding();
        }
    }

    private static IEnumerable<string> LiveModBindings() {
        foreach (var (label, button) in ModButtons()) {
            if (button.Binding is { } binding && (binding.Controller.Count > 0 || binding.Keyboard.Count > 0)) yield return label;
        }
    }

    private static IEnumerable<(string Label, VirtualButton Button)> ModButtons() {
        foreach (EverestModule module in Everest.Modules) {
            if (module.Metadata?.Name is "CelesteAIBridge" or "CelesteAIRuntime" ||
                    module.SettingsType is not { } type || module._Settings is not { } moduleSettings) {
                continue;
            }
            foreach (PropertyInfo p in type.GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)) {
                if (p.PropertyType != typeof(ButtonBinding) || p.GetGetMethod(true) == null) {
                    continue;
                }
                if (p.GetValue(moduleSettings) is ButtonBinding { Button: { } button }) {
                    yield return ($"{module.Metadata?.Name}.{p.Name}", button);
                }
            }
        }
    }
}
