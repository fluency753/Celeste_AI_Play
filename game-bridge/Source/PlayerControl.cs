using Celeste;

namespace CelesteAI.Bridge;

internal static class PlayerControl {
    public static bool IsControllable(Level? level, Player? player) {
        if (level == null || player == null || player.Dead) return false;
        // InCutscene can coexist with real player control (notably the prologue tutorial).
        if (level.Paused || level.Transitioning || level.Frozen || !player.InControl) return false;
        return player.StateMachine.State is not (Player.StDummy or Player.StIntroWalk or Player.StIntroJump
            or Player.StIntroRespawn or Player.StIntroWakeUp or Player.StIntroMoonJump or Player.StIntroThinkForABit);
    }
}
