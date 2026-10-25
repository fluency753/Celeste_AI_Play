using System;
using Monocle;
using Mono.Cecil.Cil;
using MonoMod.Cil;

namespace CelesteAI.Bridge;

internal static class EntityRenderDepthHook {
    public static bool Inject(ILContext il, Action<Entity,EntityList,int> before, Action<EntityList,int> finish) {
        var cursor = new ILCursor(il);
        // 原版先检查 Visible；必须在检查之前插入，隐藏实体也提供深度边界。
        if (!cursor.TryGotoNext(MoveType.Before, instruction => instruction.MatchLdfld<Entity>("Visible"))) return false;
        cursor.Emit(OpCodes.Dup);
        cursor.Emit(OpCodes.Ldarg_0);
        cursor.Emit(OpCodes.Ldarg_1);
        cursor.EmitDelegate(before);
        // foreach 的 leave 会直接跳到 ret；插在标签之后，退出分支也必须经过收尾。
        while (cursor.TryGotoNext(MoveType.AfterLabel, instruction => instruction.OpCode == OpCodes.Ret)) {
            cursor.Emit(OpCodes.Ldarg_0);
            cursor.Emit(OpCodes.Ldarg_1);
            cursor.EmitDelegate(finish);
            cursor.Index++;
        }
        return true;
    }
}
