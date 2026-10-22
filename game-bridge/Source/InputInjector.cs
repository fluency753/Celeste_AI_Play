using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Celeste;
using Celeste.Mod;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using Monocle;
using CelesteSettings = Celeste.Settings;
using GameInput = Celeste.Input;

namespace CelesteAI.Bridge;

/// <summary>
/// 接管期间改用一套固定的按键绑定，再逐帧写入合成的手柄/键盘状态。
/// 绑定思路参考 CelesteTAS（MIT）：先改绑定、重建虚拟输入，再把设置字段还原，
/// 这样玩家自己的按键设置不会被保存成桥接用的绑定。
/// </summary>
internal static class InputInjector {
    // 游戏内动作：手柄
    private const Buttons JumpButton = Buttons.A;
    private const Buttons DashButton = Buttons.B;
    private const Buttons GrabButton = Buttons.X;
    private const Buttons TalkButton = Buttons.Y;
    private const Buttons CrouchDashButton = Buttons.RightShoulder;

    // 菜单动作：键盘，避免与游戏内动作共用按钮
    private const Keys ConfirmKey = Keys.NumPad0;
    private const Keys BackKey = Keys.NumPad1;
    private const Keys PauseKey = Keys.NumPad2;
    private const Keys JournalKey = Keys.NumPad3;

    private static bool installed;
    private static GrabModes origGrabMode;
    private static CrouchDashModes origCrouchDashMode;
    private static bool origControllerHasFocus;
    private static bool origAttached;
    private static readonly List<(VirtualButton button, Binding original)> ModBindingBackup = new();

    public static void Install() {
        if (installed) {
            return;
        }

        CelesteSettings settings = CelesteSettings.Instance;
        FieldInfo[] bindingFields = typeof(CelesteSettings)
            .GetFields(BindingFlags.Public | BindingFlags.Instance)
            .Where(f => f.FieldType == typeof(Binding))
            .ToArray();
        var backup = bindingFields.ToDictionary(f => f, f => f.GetValue(settings));

        foreach (FieldInfo f in bindingFields) {
            f.SetValue(settings, new Binding());
        }
        Set(settings, "Left", Buttons.DPadLeft, Buttons.LeftThumbstickLeft);
        Set(settings, "Right", Buttons.DPadRight, Buttons.LeftThumbstickRight);
        Set(settings, "Up", Buttons.DPadUp, Buttons.LeftThumbstickUp);
        Set(settings, "Down", Buttons.DPadDown, Buttons.LeftThumbstickDown);
        Set(settings, "MenuLeft", Buttons.DPadLeft, Buttons.LeftThumbstickLeft);
        Set(settings, "MenuRight", Buttons.DPadRight, Buttons.LeftThumbstickRight);
        Set(settings, "MenuUp", Buttons.DPadUp, Buttons.LeftThumbstickUp);
        Set(settings, "MenuDown", Buttons.DPadDown, Buttons.LeftThumbstickDown);
        Set(settings, "Jump", JumpButton);
        Set(settings, "Dash", DashButton);
        Set(settings, "Grab", GrabButton);
        Set(settings, "Talk", TalkButton);
        Set(settings, "DemoDash", CrouchDashButton);
        SetKey(settings, "Confirm", ConfirmKey);
        SetKey(settings, "Cancel", BackKey);
        SetKey(settings, "Pause", PauseKey);
        SetKey(settings, "Journal", JournalKey);

        GameInput.Initialize();

        // 还原设置字段：虚拟输入已按上面的绑定建好，玩家的设置文件保持不变
        foreach (var (field, value) in backup) {
            field.SetValue(settings, value);
        }

        ClearModBindings();

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
        GameInput.Initialize();

        foreach (var (button, original) in ModBindingBackup) {
            button.Binding = original;
        }
        ModBindingBackup.Clear();

        MInput.ControllerHasFocus = origControllerHasFocus;
        MInput.GamePads[GameInput.Gamepad].Attached = origAttached;

        installed = false;
        Log.Info("input bindings restored");
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
        if (input.Has(Act.Jump)) buttons |= JumpButton;
        if (input.Has(Act.Dash)) buttons |= DashButton;
        if (input.Has(Act.Grab)) buttons |= GrabButton;
        if (input.Has(Act.Talk)) buttons |= TalkButton;
        if (input.Has(Act.CrouchDash)) buttons |= CrouchDashButton;
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
        if (input.Has(Act.Pause)) keys.Add(PauseKey);
        if (input.Has(Act.Journal)) keys.Add(JournalKey);
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
        ModBindingBackup.Clear();
        foreach (EverestModule module in Everest.Modules) {
            if (module == BridgeModule.Instance || module.SettingsType is not { } type || module._Settings is not { } moduleSettings) {
                continue;
            }
            foreach (PropertyInfo p in type.GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)) {
                if (p.PropertyType != typeof(ButtonBinding) || p.GetGetMethod(true) == null) {
                    continue;
                }
                // 只改运行时的虚拟按键，不动设置里保存的绑定
                if (p.GetValue(moduleSettings) is ButtonBinding { Button: { } button }) {
                    ModBindingBackup.Add((button, button.Binding));
                    button.Binding = new Binding();
                }
            }
        }
    }
}
