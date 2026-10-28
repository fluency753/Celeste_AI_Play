using Celeste;
using System.Linq;
using Monocle;

namespace CelesteAI.Bridge;

internal static class SceneControl {
    internal static bool FreezeChapterSelect = true, FreezeFileSelect, FreezeMainMenu;
    internal static bool ChapterScene(Scene? scene) => scene is Level or AreaComplete or IntroVignette or LevelEnter;
    internal static bool MenuReady(Overworld world) => !UserIO.Saving && world.Next == null &&
        world.Current is { Focused: true, Selected: true } && (world.Current switch {
            OuiMainMenu main => main.Buttons?.Any(b => b.Selected && b.canAcceptInput) == true,
            OuiFileSelect files => files.Slots != null && !files.Slots.Any(slot => slot?.StartingGame == true),
            _ => true
        });
    // Chapter presentation and results belong to the run, unlike Overworld menus.
    internal static bool FreezeWhenIdle(Scene? scene) =>
        ChapterScene(scene) || ReloadBoundary.LoaderPresentation(scene) ||
        scene is Overworld world && MenuReady(world) && (world.Current switch {
            OuiChapterSelect or OuiChapterPanel => FreezeChapterSelect,
            OuiFileSelect => FreezeFileSelect,
            OuiMainMenu => FreezeMainMenu,
            _ => false
        });

    internal static bool HaltEngine(bool active, bool halted, bool freeFrame, Scene? scene) =>
        active && (halted || (freeFrame && FreezeWhenIdle(scene)));

    internal static bool OwnInput(bool active, Scene? scene, bool hasJob) =>
        active && (hasJob || FreezeWhenIdle(scene));
}
