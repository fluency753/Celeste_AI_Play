using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>Read the rendered page, never the dialog script or unrevealed glyphs.</summary>
internal static class StoryText {
    private static readonly Dictionary<(Type, string), Type?> IteratorTypes = new();

    // Read the current iterator without running it. Everest wraps iterators in
    // Flattened, whose innermost enumerator is the routine actually executing.
    private static IEnumerator? Current(Coroutine routine) {
        if (!routine.Active || routine.Finished) return null;
        IEnumerator? current = routine.Current;
        for (int depth = 0; depth < 8 && current is Celeste.Mod.SwapImmediatelyExtension.Flattened flat; depth++)
            current = flat.enums.Count > 0 ? flat.enums.Peek() : null;
        return current;
    }

    private static bool IsRoutine(IEnumerator? current, Type owner, string method) {
        var key = (owner, method);
        if (!IteratorTypes.TryGetValue(key, out Type? iterator)) {
            iterator = owner.GetMethod(method, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?
                .GetCustomAttribute<IteratorStateMachineAttribute>()?.StateMachineType;
            IteratorTypes[key] = iterator;
        }
        return iterator != null && current?.GetType() == iterator;
    }

    private static Coroutine? MemoRoutine(Level level, CS03_Memo.MemoPage page) {
        foreach (var entity in level.Entities) {
            if (entity is not CS03_Memo owner || !ReferenceEquals(owner.memo, page) || owner.Components == null) continue;
            foreach (var component in owner.Components)
                if (component is Coroutine routine && Current(routine) != null) return routine;
        }
        return null;
    }

    internal static bool MemoReady(Level level, CS03_Memo.MemoPage page) => page.Visible && page.alpha > 0f && !page.easingOut
        && MemoRoutine(level, page) is { } routine && routine.waitTimer <= 0f
        && IsRoutine(Current(routine), typeof(CS03_Memo.MemoPage), "Wait");

    internal static (string State, string Source) PlaybackState(Scene? scene) {
        if (InputNode(scene) is { } input) return ("input", input);
        if (scene is Level level) {
            if (level.FrozenOrPaused || level.SkippingCutscene || level.RetryPlayerCorpse != null)
                return ("unknown", "level_blocked");
            if (level.Entities.FindFirst<CS03_Memo.MemoPage>() is { Visible: true } memo) {
                var current = MemoRoutine(level, memo) is { } routine ? Current(routine) : null;
                if (IsRoutine(current, typeof(CS03_Memo.MemoPage), "EaseIn") || IsRoutine(current, typeof(CS03_Memo.MemoPage), "EaseOut"))
                    return ("playing", "memo_animation");
                return ("unknown", "memo_unrecognized_routine");
            }
            if (level.Entities.FindFirst<Textbox>() is { Visible: true, runRoutine: { } textRoutine } box) {
                var current = Current(textRoutine);
                if (IsRoutine(current, typeof(Textbox), "EaseOpen") || IsRoutine(current, typeof(Textbox), "EaseClose"))
                    return ("playing", "dialogue_animation");
                if (IsRoutine(current, typeof(Textbox), "RunRoutine") && !box.isInTrigger) {
                    if (box.waitingForInput && textRoutine.waitTimer > 0f) return ("playing", "dialogue_debounce");
                    // RunRoutine increments index before yielding its timed
                    // reveal/wait; Nodes[index] already refers to the next node.
                    if (!box.waitingForInput && current?.Current is float delay && delay >= 0f)
                        return ("playing", "dialogue_delay");
                }
                return ("unknown", "dialogue_unrecognized_routine");
            }
            return level.InCutscene ? ("unknown", "unrecognized_cutscene") : ("none", "no_cutscene");
        }
        if (scene is LevelEnter enter && enter.postcard is { } card) {
            foreach (var entity in enter.Entities) foreach (var component in entity.Components) {
                if (component is not Coroutine routine) continue;
                var current = Current(routine);
                if (IsRoutine(current, typeof(Postcard), "EaseIn") || IsRoutine(current, typeof(Postcard), "EaseOut"))
                    return ("playing", "postcard_animation");
                if (IsRoutine(current, typeof(Postcard), "DisplayRoutine") && routine.waitTimer > 0f)
                    return ("playing", "postcard_delay");
            }
            return ("unknown", "postcard_unrecognized_routine");
        }
        return scene is IntroVignette or LevelEnter ? ("unknown", "unrecognized_story_scene") : ("none", "no_cutscene");
    }
    // Textbox sets waitingForInput before yielding a 0.1s debounce. During that
    // coroutine wait a confirm press is discarded, although the page is visible.
    internal static bool DialogueReady(Textbox? box) => box is { Visible: true, waitingForInput: true, disableInput: false }
        && Ease.CubeInOut(box.ease) >= 0.05f
        && box.runRoutine is { Finished: false, waitTimer: <= 0f };

    public static string? InputNode(Scene? scene) {
        if (scene is Level level && !level.FrozenOrPaused && !level.SkippingCutscene && level.RetryPlayerCorpse == null) {
            var campfire = level.Entities.FindFirst<CS06_Campfire>();
            if (campfire?.currentOptions is { Count: > 0 })
                return campfire.optionEase >= 0.999f ? "choice" : null;
            var box = level.Entities.FindFirst<Textbox>();
            if (box is { Visible: true })
                return DialogueReady(box) ? "dialogue" : null;
            var memo = level.Entities.FindFirst<CS03_Memo.MemoPage>();
            if (memo != null && MemoReady(level, memo)) return "memo";
            var tutorial = level.Entities.FindFirst<BirdTutorialGui>();
            var player = level.Entities.FindFirst<Player>();
            // Only a tutorial that a cutscene is waiting on is an input node. Ambient birds (1A climbing)
            // stay open during respawn and room transitions, when the player is briefly not controllable.
            if (tutorial is { Visible: true, Open: true, Scale: >= 0.999f } && tutorial.controls.Count > 0
                && player != null && !PlayerControl.IsControllable(level, player)
                && (level.InCutscene || player.StateMachine.State == Player.StDummy))
                return "tutorial";
        } else if (scene is LevelEnter enter && enter.postcard is { Visible: true, alpha: >= 0.999f, buttonEase: > 0f }) {
            return "postcard";
        }
        return null;
    }

    // A skippable-cutscene decision is earlier than a dialogue input node. This
    // only offers an attempt through the pause menu; it does not promise that
    // this particular cutscene has a Skip item or press anything for the agent.
    public static string? DecisionNode(Scene? scene) {
        string? input = InputNode(scene);
        if (input != null) return input;
        if (scene is IntroVignette { exiting: false, menu: null }) return "skip";
        if (scene is Level level && level.InCutscene && level.CanPause
            && !level.FrozenOrPaused && !level.Transitioning && !level.SkippingCutscene && level.RetryPlayerCorpse == null) {
            Player? player = level.Entities.FindFirst<Player>();
            if (player is { Dead: false } && !PlayerControl.IsControllable(level, player)) return "skip";
        }
        return null;
    }

    internal static string ReadMemorial(MemorialText? memorial, bool completed = false) {
        // MemorialText.Render draws a prefix of its localized message, not FancyText.
        // Show may already be false while text is fading out; it is still visible then.
        if (memorial == null || !memorial.Visible || completed || memorial.index <= 0f
            || memorial.alpha <= 0f || (Microsoft.Xna.Framework.Color.White * Ease.CubeInOut(memorial.alpha)).A == 0
            || string.IsNullOrEmpty(memorial.message)) return "";
        string message = memorial.message;
        int count = (int)Math.Min(message.Length, memorial.index);
        var result = new StringBuilder(count);
        for (int i = 0; i < count; i++) {
            char ch = message[i];
            if (memorial.Dreamy && ch != ' ' && ch != '-' && ch != '\n') {
                // Mirror the displayed scrambled glyphs; don't expose the clear inscription.
                int offset = (int)(Math.Sin(memorial.timer * 2f + i / 8f) * 4d);
                ch = message[(i + offset + message.Length) % message.Length];
            }
            result.Append(ch);
        }
        // Never serialize half a UTF-16 pair when the reveal index falls between its units.
        if (result.Length > 0 && char.IsHighSurrogate(result[result.Length - 1])) result.Length--;
        return result.ToString().Trim();
    }

    internal static string ReadPage(FancyText.Text? text, int start, float alpha = 1f) {
        if (text?.Nodes == null || alpha <= 0f) return "";
        var result = new StringBuilder();
        for (int i = Math.Max(0, start); i < text.Nodes.Count; i++) {
            var node = text.Nodes[i];
            if (node is FancyText.NewPage) break;
            if (node is FancyText.NewLine) {
                if (result.Length > 0) result.Append('\n');
            } else if (node is FancyText.Char ch && ch.Fade * alpha * ch.Color.A >= 1f) {
                if (ch.Character is >= 0 and <= 0x10ffff && ch.Character is not (>= 0xd800 and <= 0xdfff))
                    result.Append(char.ConvertFromUtf32(ch.Character));
            }
        }
        return result.ToString().Trim();
    }

    public static void Write(Utf8JsonWriter w, Scene? scene) {
        var playback = PlaybackState(scene);
        w.WriteStartObject("storyPlaybackState");
        w.WriteString("state", playback.State);
        w.WriteString("source", playback.Source);
        w.WriteEndObject();
        w.WriteStartArray("story");
        if (scene is Level level && !level.FrozenOrPaused && !level.SkippingCutscene && level.RetryPlayerCorpse == null) {
            var box = level.Entities.FindFirst<Textbox>();
            if (box is { Visible: true } && Ease.CubeInOut(box.ease) >= 0.05f) {
                Entry(w, "dialogue", ReadPage(box.text, box.Start, Ease.CubeInOut(box.ease)), DialogueReady(box),
                    box.Page, box.portraitExists ? box.portrait : null);
            }
            var ending = level.Entities.FindFirst<PrologueEndingText>();
            if (ending is { Visible: true }) Entry(w, "ending", ReadPage(ending.text, 0));
            foreach (var memorial in level.Entities.FindAll<MemorialText>())
                Entry(w, "memorial", ReadMemorial(memorial, level.Completed));
            var tutorial = level.Entities.FindFirst<BirdTutorialGui>();
            if (tutorial is { Visible: true, Scale: > 0f }) {
                w.WriteStartObject();
                w.WriteString("kind", "tutorial");
                w.WriteString("text", tutorial.info as string ?? "");
                w.WriteStartArray("controls");
                foreach (var control in tutorial.controls) {
                    if (control is BirdTutorialGui.ButtonPrompt button) w.WriteStringValue(button.ToString());
                    else if (control is Microsoft.Xna.Framework.Vector2 aim) {
                        w.WriteStartObject(); w.WriteNumber("x", aim.X); w.WriteNumber("y", aim.Y); w.WriteEndObject();
                    } else if (control is string label) w.WriteStringValue(label);
                }
                w.WriteEndArray();
                w.WriteEndObject();
            }
        } else if (scene is IntroVignette intro) {
            Entry(w, "intro", ReadPage(intro.text, intro.textStart, intro.textAlpha * (1f - intro.pauseFade)));
        } else if (scene is LevelEnter enter && enter.postcard is { Visible: true } card) {
            Entry(w, "postcard", ReadPage(card.text, 0, card.alpha));
        }
        w.WriteEndArray();
        WriteUi(w, scene);
    }

    // UI observation supplies facts only. It does not press keys or assume a
    // visible/fading Poem is already accepting confirmation (fake hearts differ).
    internal static void WriteUi(Utf8JsonWriter w, Scene? scene) {
        if (scene is Level memoLevel && !memoLevel.FrozenOrPaused && !memoLevel.SkippingCutscene && memoLevel.RetryPlayerCorpse == null
            && memoLevel.Entities.FindFirst<CS03_Memo.MemoPage>() is { Visible: true, alpha: > 0f } memo) {
            w.WriteStartObject("ui");
            w.WriteString("type", "MemoPage");
            w.WriteNumber("alpha", memo.alpha);
            w.WriteBoolean("inputReady", MemoReady(memoLevel, memo));
            w.WriteString("controls", "U: previous section; D/C: next section; C on final section or B: close");
            w.WriteEndObject();
        } else if (scene is Level level && level.Entities.FindFirst<Poem>() is { Visible: true, Alpha: > 0f } poem) {
            w.WriteStartObject("ui");
            w.WriteString("type", "Poem");
            w.WriteNumber("alpha", poem.Alpha);
            w.WriteNumber("textAlpha", poem.TextAlpha);
            if (poem.TextAlpha > 0f && !string.IsNullOrEmpty(poem.text)) w.WriteString("text", poem.text);
            w.WriteEndObject();
        } else if (scene is AreaComplete results) {
            w.WriteStartObject("ui");
            w.WriteString("type", "AreaComplete");
            w.WriteBoolean("confirmReady", results.finishedSlide && results.canConfirm);
            w.WriteEndObject();
        }
    }

    private static void Entry(Utf8JsonWriter w, string kind, string text, bool waiting = false,
                              int? page = null, FancyText.Portrait? portrait = null) {
        if (text.Length == 0) return;
        w.WriteStartObject();
        w.WriteString("kind", kind);
        w.WriteString("text", text);
        w.WriteBoolean("waitingForInput", waiting);
        if (page.HasValue) w.WriteNumber("page", page.Value);
        if (portrait != null) {
            w.WriteStartObject("speakerPortrait");
            w.WriteString("sprite", portrait.Sprite);
            w.WriteString("expression", portrait.Animation);
            w.WriteEndObject();
        }
        w.WriteEndObject();
    }
}
