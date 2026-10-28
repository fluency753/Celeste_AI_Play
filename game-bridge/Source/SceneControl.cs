using Celeste;
using Monocle;

namespace CelesteAI.Bridge;

internal static class SceneControl {
    // Chapter presentation and results belong to the run, unlike Overworld menus.
    internal static bool FreezeWhenIdle(Scene? scene) =>
        scene is Level or AreaComplete or IntroVignette or LevelEnter || ReloadBoundary.LoaderPresentation(scene);

    internal static bool HaltEngine(bool active, bool halted, bool freeFrame, Scene? scene) =>
        active && (halted || (freeFrame && FreezeWhenIdle(scene)));

    internal static bool OwnInput(bool active, Scene? scene, bool hasJob) =>
        active && (hasJob || FreezeWhenIdle(scene));
}
