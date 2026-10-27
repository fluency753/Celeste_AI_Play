using Celeste;

namespace CelesteAI.Bridge;

/// <summary>Translate actual menu items to semantic actions; policy is separate.</summary>
internal static class ChapterResetGuard {
    public static bool Available { get; set; }
    private static readonly (string Key, string Action)[] Actions = {
        ("menu_pause_restartarea", "restart_chapter"), ("menu_restart_continue", "restart_chapter"),
        ("menu_pause_return", "return_to_map"), ("menu_return_continue", "return_to_map"),
        ("menu_pause_savequit", "save_quit"), ("intro_vignette_quit", "return_to_map"),
    };

    // Known pause-menu items that never restart or leave the chapter (resume, retry = respawn in this room, skip the
    // cutscene, and the cancel buttons of the two confirmation menus).
    private static readonly string[] Safe = {
        "menu_pause_resume", "menu_pause_retry", "menu_pause_skip_cutscene", "intro_vignette_resume",
        "intro_vignette_skip", "menu_restart_cancel", "menu_return_cancel",
    };

    public static bool IsSafe(TextMenu.Item item) {
        if (item is not TextMenu.Button button) return false;
        foreach (string key in Safe)
            if (Dialog.Has(key) && button.Label == Dialog.Clean(key)) return true;
        return false;
    }

    public static string? LabelOf(TextMenu.Item item) => item is TextMenu.Button button ? button.Label : item.SearchLabel();

    public static string? ActionOf(TextMenu.Item item) {
        if (item is not TextMenu.Button button) return null;
        foreach (var (key, action) in Actions)
            if (Dialog.Has(key) && button.Label == Dialog.Clean(key)) return action;
        return null;
    }
}
