using System;
using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

/// <summary>Inspect the actual selected item immediately before its confirmation callbacks.</summary>
internal static class ChapterResetGuard {
    private static readonly string[] Safe = {
        "menu_pause_resume", "menu_pause_retry", "menu_pause_skip_cutscene",
        "intro_vignette_resume", "intro_vignette_skip",
        "menu_restart_cancel", "menu_return_cancel",
    };
    private static readonly string[] Exit = {
        "menu_pause_return", "menu_pause_savequit", "intro_vignette_quit",
        "menu_return_continue",
    };
    private static readonly string[] Restart = { "menu_pause_restartarea", "menu_restart_continue" };

    public static bool Available { get; set; }

    // Called at the actual ConfirmPressed call site, after TextMenu has applied
    // this frame's navigation and native virtual-button edge/repeat handling.
    public static string? Check(TextMenu.Item item, bool allowMapExit) {
        return CheckMenu(Resolve(Label(item)), allowMapExit);
    }

    private static string? Label(TextMenu.Item item) => item switch {
        TextMenu.Button button => button.Label,
        TextMenu.Header header => header.Title,
        _ => item.SearchLabel(),
    };

    private static string? Resolve(string? label) {
        if (label == null) return null;
        foreach (string[] group in new[] { Safe, Exit, Restart }) {
            foreach (string key in group) {
                if (Dialog.Has(key) && label == Dialog.Clean(key)) return key;
            }
        }
        return null;
    }

    internal static string? CheckMenu(string? selected, bool completed) {
        foreach (string key in Safe) {
            if (selected == key) return null;
        }
        foreach (string key in Restart) {
            if (selected == key)
                return "chapter_reset_blocked: restarting the chapter is disabled by the controller";
        }
        foreach (string key in Exit) {
            if (selected == key)
                return completed ? null : "chapter_reset_blocked: returning to the map or saving and quitting before completion is disabled";
        }
        return null;
    }
}
