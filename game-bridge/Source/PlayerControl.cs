using Celeste;

namespace CelesteAI.Bridge;

internal static class PlayerControl {
    // A missing player during a scene change is not a death. In normal Celeste
    // the player remains present with Dead=true during the death animation.
    public static bool IsDeathTransition(bool wasAlive, Player? player) => wasAlive && player is { Dead: true };
    public static bool IsControllable(Level? level, Player? player) {
        if (level == null || player == null || player.Dead) return false;
        // InCutscene can coexist with real player control (notably the prologue tutorial).
        if (level.Paused || level.Transitioning || level.Frozen || !player.InControl) return false;
        return player.StateMachine.State is not (Player.StDummy or Player.StIntroWalk or Player.StIntroJump
            or Player.StIntroRespawn or Player.StIntroWakeUp or Player.StIntroMoonJump or Player.StIntroThinkForABit);
    }
}
