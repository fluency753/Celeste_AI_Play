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
    internal static bool HasVisibleStoryHud(Scene? scene) => StoryPlayback.HasVisibleStoryHud(scene);
    internal static IEnumerable<T> SceneEntities<T>(Scene scene) where T : Entity {
        if (scene.Entities == null) yield break;
        foreach (var entity in scene.Entities)
            if (entity is T match && !scene.Entities.toRemove.Contains(entity)) yield return match;
    }
    internal static T? SceneEntity<T>(Scene scene) where T : Entity {
        foreach (var entity in SceneEntities<T>(scene)) return entity;
        return null;
    }
    // RenderOnly still draws a queued removal in this frame; its input and
    // presentation are already retired, but its last pixels can remain visible.
    internal static T? RenderedEntity<T>(Scene scene) where T : Entity => scene.Entities?.FindFirst<T>();
    private static readonly Dictionary<(Type, string), HashSet<Type>> IteratorTypes = new();

    // Read the current iterator without running it. Everest wraps iterators in
    // Flattened, whose innermost enumerator is the routine actually executing.
    internal static IEnumerator? Current(Coroutine routine) {
        if (!routine.Active || routine.Finished) return null;
        IEnumerator? current = routine.Current;
        for (int depth = 0; depth < 8 && current is Celeste.Mod.SwapImmediatelyExtension.Flattened flat; depth++)
            current = flat.enums.Count > 0 ? flat.enums.Peek() : null;
        return current;
    }

    internal static bool IsRoutine(IEnumerator? current, Type owner, string method) {
        var key = (owner, method);
        if (!IteratorTypes.TryGetValue(key, out var iterators)) {
            iterators = new HashSet<Type>();
            var native = owner.GetMethod(method, BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            if (native != null) foreach (var attribute in native.GetCustomAttributes<IteratorStateMachineAttribute>())
                iterators.Add(attribute.StateMachineType);
            IteratorTypes[key] = iterators;
        }
        return current != null && iterators.Contains(current.GetType());
    }

    internal static int? IteratorState(IEnumerator iterator) => iterator.GetType()
        .GetField("<>1__state", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(iterator) as int?;
    internal static bool IteratorFinished(IEnumerator iterator) => IteratorState(iterator) == -1;

    internal static bool CompletedChild(IEnumerator? parent, object owner, Type type, params string[] methods) {
        if (parent?.Current is not IEnumerator child || !IteratorFinished(child)) return false;
        if (!ReferenceEquals(child.GetType().GetField("<>4__this", BindingFlags.Instance |
            BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(child), owner)) return false;
        foreach (string method in methods) if (IsRoutine(child, type, method)) return true;
        return false;
    }

    private static Coroutine? MemoRoutine(Level level, CS03_Memo.MemoPage page) {
        foreach (var entity in level.Entities) {
            if (entity is not CS03_Memo owner || level.Entities.toRemove.Contains(owner) || !ReferenceEquals(owner.memo, page) || owner.Components == null) continue;
            foreach (var component in owner.Components)
                if (component is Coroutine routine && Current(routine) != null) return routine;
        }
        return null;
    }

    internal static bool MemoReady(Level level, CS03_Memo.MemoPage page) => page.Visible && page.alpha > 0f && !page.easingOut
        && MemoRoutine(level, page) is { } routine && routine.waitTimer <= 0f
        && IsRoutine(Current(routine), typeof(CS03_Memo.MemoPage), "Wait");

    private static Postcard? PostcardField(Scene? scene) => scene switch {
        LevelEnter enter => enter.postcard,
        OverworldLoader loader => loader.postcard,
        _ => null,
    };

    internal static Postcard? ScenePostcard(Scene? scene) {
        Postcard? card = PostcardField(scene);
        // RemoveSelf queues removal; Scene is cleared only by the next native
        // BeforeUpdate. A queued card must not freeze that very update forever.
        return card != null && ReferenceEquals(card.Scene, scene) && scene?.Entities is { } entities &&
            !entities.toRemove.Contains(card) ? card : null;
    }

    private static bool IsCardRoutine(IEnumerator? current, Postcard card, string method) =>
        IsRoutine(current, typeof(Postcard), method) && ReferenceEquals(current!.GetType()
            .GetField("<>4__this", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(current), card);

    private static Coroutine? PostcardRoutine(Scene scene, Postcard card) {
        if (scene.Entities == null) return null;
        foreach (var entity in scene.Entities) foreach (var component in entity.Components) {
            if (component is not Coroutine routine) continue;
            var current = Current(routine);
            if (IsCardRoutine(current, card, "DisplayRoutine") || IsCardRoutine(current, card, "EaseIn") ||
                IsCardRoutine(current, card, "EaseOut")) return routine;
        }
        return null;
    }

    internal static bool PostcardReady(Scene scene, Postcard card) =>
        ReferenceEquals(ScenePostcard(scene), card) && card is { Visible: true, alpha: >= 0.999f, buttonEase: > 0f } &&
        PostcardRoutine(scene, card) is { waitTimer: <= 0f } routine &&
        IsCardRoutine(Current(routine), card, "DisplayRoutine") && Current(routine)?.Current == null;

    private static bool IntroExitCallback(IntroVignette intro, Action callback) {
        Type? owner = callback.Method.DeclaringType;
        if (owner != typeof(IntroVignette) && owner?.DeclaringType != typeof(IntroVignette)) return false;
        if (!callback.Method.Name.StartsWith("<StartGame>b__", StringComparison.Ordinal) &&
            !callback.Method.Name.StartsWith("<ReturnToMap>b__", StringComparison.Ordinal)) return false;
        return ReferenceEquals(callback.Target, intro) || ReferenceEquals(callback.Target?.GetType()
            .GetField("<>4__this", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(callback.Target), intro);
    }

    private static bool IntroExitWipe(IntroVignette intro) {
        if (intro.RendererList is not { } renderers) return false;
        bool Matches(Renderer renderer) => renderer is FadeWipe { WipeIn: false, ending: false, OnComplete: not null } wipe
            && ReferenceEquals(wipe.Scene, intro) && IntroExitCallback(intro, wipe.OnComplete) && !renderers.removing.Contains(wipe);
        // StartGame adds its wipe after base.Update has flushed the renderer
        // list. Completed also needs one more native update to call OnComplete.
        return renderers.Renderers.Exists(Matches) || renderers.adding.Exists(Matches);
    }

    private static (string State, string Source) IntroPlayback(IntroVignette intro) {
        if (intro.menu != null) return ("unknown", "intro_menu");
        if (intro.exiting)
            return IntroExitWipe(intro) ? ("playing", "intro_exit_wipe") : ("unknown", "intro_unrecognized_exit");
        // Native Update advances this timer even after TextSequence finishes.
        // Verify its real routine as well: an unknown replacement may ask for
        // input and must not inherit permission merely from this scene's type.
        if (!intro.started && float.IsFinite(intro.timer) && intro.timer >= 0f && intro.timer < 18.683f &&
            intro.textCoroutine is { } routine &&
            (IsRoutine(Current(routine), typeof(IntroVignette), "TextSequence") ||
                (routine.Finished && intro.textStart == int.MaxValue)))
            return ("playing", "intro_text_sequence");
        return ("unknown", "intro_unrecognized_routine");
    }

    internal static (string State, string Source) PlaybackState(Scene? scene) {
        if (InputNode(scene) is { } input) return ("input", input);
        if (scene is IntroVignette intro) return IntroPlayback(intro);
        if (scene is Level level) {
            if (level.FrozenOrPaused || level.SkippingCutscene || level.RetryPlayerCorpse != null)
                return ("unknown", "level_blocked");
            if (SceneEntity<CS03_Memo.MemoPage>(level) is { Visible: true } memo) {
                var current = MemoRoutine(level, memo) is { } routine ? Current(routine) : null;
                if (IsRoutine(current, typeof(CS03_Memo.MemoPage), "EaseIn") || IsRoutine(current, typeof(CS03_Memo.MemoPage), "EaseOut"))
                    return ("playing", "memo_animation");
                if (IsRoutine(current, typeof(CS03_Memo), "Routine") &&
                    CompletedChild(current, memo, typeof(CS03_Memo.MemoPage), "EaseIn", "Wait", "EaseOut"))
                    return ("playing", "memo_resume");
                return ("unknown", "memo_unrecognized_routine");
            }
            var box = SceneEntity<Textbox>(level);
            if (box != null && !level.Entities.toRemove.Contains(box) && (!box.Visible || box.runRoutine == null))
                return ("unknown", "dialogue_unrecognized_routine");
            if (box is { Visible: true, runRoutine: { } textRoutine } && !level.Entities.toRemove.Contains(box)) {
                var current = Current(textRoutine);
                if (IsRoutine(current, typeof(Textbox), "EaseOpen") || IsRoutine(current, typeof(Textbox), "EaseClose"))
                    return ("playing", "dialogue_animation");
                if (IsRoutine(current, typeof(Textbox), "RunRoutine") &&
                    StoryPlayback.CompletedDialogueChild(level, box, current!)) return ("playing", "dialogue_resume");
                if (IsRoutine(current, typeof(Textbox), "RunRoutine") && !box.isInTrigger) {
                    if (box.waitingForInput && textRoutine.waitTimer > 0f) return ("playing", "dialogue_debounce");
                    // RunRoutine increments index before yielding its timed
                    // reveal/wait; Nodes[index] already refers to the next node.
                    if (!box.waitingForInput && current?.Current is float delay && delay >= 0f)
                        return ("playing", "dialogue_delay");
                    // Portrait changes can hold RunRoutine on null while the
                    // native begin animation plays. Its node has not advanced
                    // yet, unlike the timed character/reveal yields above.
                    if (!box.waitingForInput && !box.canSkip && box.portraitExists && current?.Current == null && IteratorState(current!) == 4 &&
                        box.text?.Nodes is { } nodes && box.index >= 0 && box.index < nodes.Count &&
                        nodes[box.index] is FancyText.Portrait portrait && ReferenceEquals(box.portrait, portrait) &&
                        box.portraitSprite is { Active: true })
                        // Textbox updates its sprite after RunRoutine. On the
                        // last animation frame Animating may already be false;
                        // native state 4 still needs to leave that same loop.
                        return ("playing", "dialogue_portrait_animation");
                }
                if (box.isInTrigger) {
                    if (StoryPlayback.Presentation(level) is { } triggerPresentation) return triggerPresentation;
                    if (StoryPlayback.Automatic(level, textRoutine, true))
                        return ("playing", "dialogue_trigger_animation");
                }
                return ("unknown", "dialogue_unrecognized_routine");
            }
            if (StoryPlayback.Presentation(level) is { } presentation) return presentation;
            var player = SceneEntity<Player>(level);
            if (player != null && StoryPlayback.PlayerIntro(player)) return ("playing", "player_intro_animation");
            foreach (var entity in level.Entities) {
                if (!entity.Active || entity.Components == null) continue;
                foreach (var component in entity.Components)
                    if (component is Coroutine routine && StoryPlayback.Automatic(level, routine))
                        return ("playing", "cutscene_animation");
            }
            return level.InCutscene ? ("unknown", "unrecognized_cutscene") : ("none", "no_cutscene");
        }
        if (scene != null && PostcardField(scene) is { } card) {
            if (PostcardRoutine(scene, card) is { } routine) {
                var current = Current(routine);
                if (ReferenceEquals(ScenePostcard(scene), card) &&
                    (IsRoutine(current, typeof(Postcard), "EaseIn") || IsRoutine(current, typeof(Postcard), "EaseOut")))
                    return ("playing", "postcard_animation");
                // DisplayRoutine has a final 1.2s delay after RemoveSelf. That
                // live coroutine may still advance, without reviving its UI.
                if (IsRoutine(current, typeof(Postcard), "DisplayRoutine") && routine.waitTimer > 0f)
                    return ("playing", "postcard_delay");
                if (IsCardRoutine(current, card, "DisplayRoutine")) {
                    if (current?.Current is float delay && delay >= 0f) return ("playing", "postcard_delay");
                    if (CompletedChild(current, card, typeof(Postcard), "EaseIn", "EaseOut"))
                        return ("playing", "postcard_resume");
                }
            }
            return ("unknown", "postcard_unrecognized_routine");
        }
        return scene is LevelEnter ? ("unknown", "unrecognized_story_scene") : ("none", "no_cutscene");
    }
    // Textbox sets waitingForInput before yielding a 0.1s debounce. During that
    // coroutine wait a confirm press is discarded, although the page is visible.
    internal static bool DialogueReady(Textbox? box) => box is { Visible: true, waitingForInput: true, disableInput: false }
        && Ease.CubeInOut(box.ease) >= 0.05f
        && box.runRoutine is { Finished: false, waitTimer: <= 0f };

    public static string? InputNode(Scene? scene) {
        if (scene is Level level && !level.FrozenOrPaused && !level.SkippingCutscene && level.RetryPlayerCorpse == null) {
            var campfire = SceneEntity<CS06_Campfire>(level);
            if (campfire?.currentOptions is { Count: > 0 })
                return campfire.optionEase >= 0.999f ? "choice" : null;
            var box = SceneEntity<Textbox>(level);
            if (box is { Visible: true })
                return DialogueReady(box) ? "dialogue" : box.isInTrigger ? StoryPlayback.InputNode(level) : null;
            var memo = SceneEntity<CS03_Memo.MemoPage>(level);
            if (memo != null && MemoReady(level, memo)) return "memo";
            var tutorial = StoryPlayback.SceneTutorial(level);
            var player = SceneEntity<Player>(level);
            // Only a tutorial that a cutscene is waiting on is an input node. Ambient birds (1A climbing)
            // stay open during respawn and room transitions, when the player is briefly not controllable.
            if (tutorial is { Visible: true, Open: true, Scale: >= 0.999f } && tutorial.controls.Count > 0
                && player != null && !PlayerControl.IsControllable(level, player)
                && (level.InCutscene || player.StateMachine.State == Player.StDummy)
                && StoryPlayback.TutorialReady(level, tutorial))
                return "tutorial";
            return StoryPlayback.InputNode(level);
        } else if (scene != null && ScenePostcard(scene) is { } card && PostcardReady(scene, card)) {
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
        if (scene is IntroVignette { exiting: false, menu: null, timer: >= 0f and < 16.683f }) return "skip";
        if (scene is Level level && level.InCutscene && level.CanPause
            && !level.FrozenOrPaused && !level.Transitioning && !level.SkippingCutscene && level.RetryPlayerCorpse == null) {
            Player? player = SceneEntity<Player>(level);
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
            var box = SceneEntity<Textbox>(level);
            if (box is { Visible: true } && Ease.CubeInOut(box.ease) >= 0.05f) {
                Entry(w, "dialogue", ReadPage(box.text, box.Start, Ease.CubeInOut(box.ease)), DialogueReady(box),
                    box.Page, box.portraitExists ? box.portrait : null);
            }
            var ending = SceneEntity<PrologueEndingText>(level);
            if (ending is { Visible: true }) Entry(w, "ending", ReadPage(ending.text, 0));
            foreach (var memorial in SceneEntities<MemorialText>(level))
                Entry(w, "memorial", ReadMemorial(memorial, level.Completed));
            var tutorial = StoryPlayback.SceneTutorial(level);
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
        } else if (scene != null && ScenePostcard(scene) is { Visible: true } card && ReferenceEquals(card.Scene, scene)) {
            Entry(w, "postcard", ReadPage(card.text, 0, card.alpha));
        }
        w.WriteEndArray();
        WriteUi(w, scene);
    }

    // UI observation supplies facts only. It does not press keys or assume a
    // visible/fading Poem is already accepting confirmation (fake hearts differ).
    internal static void WriteUi(Utf8JsonWriter w, Scene? scene) {
        if (scene is Level storyLevel && !storyLevel.FrozenOrPaused && !storyLevel.SkippingCutscene &&
            storyLevel.RetryPlayerCorpse == null && StoryPlayback.WriteUi(w, storyLevel)) return;
        if (scene != null && ScenePostcard(scene) is { Visible: true } card && ReferenceEquals(card.Scene, scene)) {
            w.WriteStartObject("ui");
            w.WriteString("type", "Postcard");
            w.WriteNumber("alpha", card.alpha);
            w.WriteBoolean("inputReady", PostcardReady(scene, card));
            if (scene is OverworldLoader loader) w.WriteBoolean("backgroundLoaded", System.Threading.Volatile.Read(ref loader.loaded));
            w.WriteString("controls", "C: confirm when inputReady");
            w.WriteEndObject();
        } else if (scene is Level memoLevel && !memoLevel.FrozenOrPaused && !memoLevel.SkippingCutscene && memoLevel.RetryPlayerCorpse == null
            && SceneEntity<CS03_Memo.MemoPage>(memoLevel) is { Visible: true, alpha: > 0f } memo) {
            w.WriteStartObject("ui");
            w.WriteString("type", "MemoPage");
            w.WriteNumber("alpha", memo.alpha);
            w.WriteBoolean("inputReady", MemoReady(memoLevel, memo));
            w.WriteString("controls", "U: previous section; D/C: next section; C on final section or B: close");
            w.WriteEndObject();
        } else if (scene is Level level && SceneEntity<Poem>(level) is { Visible: true, Alpha: > 0f } poem) {
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
