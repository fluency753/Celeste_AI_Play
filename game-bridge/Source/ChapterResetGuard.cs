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

    public static string? ActionOf(TextMenu.Item item) {
        if (item is not TextMenu.Button button) return null;
        foreach (var (key, action) in Actions)
            if (Dialog.Has(key) && button.Label == Dialog.Clean(key)) return action;
        return null;
    }
}
