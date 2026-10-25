using System;
using System.Text;
using System.Text.Json;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>Read the rendered page, never the dialog script or unrevealed glyphs.</summary>
internal static class StoryText {
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
        w.WriteStartArray("story");
        if (scene is Level level && !level.FrozenOrPaused && !level.SkippingCutscene && level.RetryPlayerCorpse == null) {
            var box = level.Entities.FindFirst<Textbox>();
            if (box is { Visible: true } && Ease.CubeInOut(box.ease) >= 0.05f) {
                Entry(w, "dialogue", ReadPage(box.text, box.Start, Ease.CubeInOut(box.ease)), box.waitingForInput,
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
