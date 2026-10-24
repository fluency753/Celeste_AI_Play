"""Early, factual exploration hints; never spends simulation energy automatically."""

GEOMETRY = ("【几何核验】先用本次返回的精确 JSON 核对 player.hitbox、bounds、solids、实体和出口："
            "通路是否连通、开口是否容得下碰撞箱、下一落点是否安全。出口邻接不等于可达。"
            "截图只作补充，不凭视觉猜测墙后通路；已有数据不必重复 observe。")
REPLAN = ("先核对碰撞箱与挡路地形，换一个可验证的假设，用短动作立即试验。"
          "若几何仍无法区分两种路线，且模拟体力足够，可 simulate 比较少量有区别的短路线；"
          "不重复预测同一受阻方向，不为避免失败而长时间空想。")


def room_key(obs):
    level = obs.get("level") or {}
    return level.get("sid"), level.get("mode"), level.get("room")


def action_hint(before, after, result, inputs, decision_wait=0):
    """First failure is enough; no multi-death/stuck counter to delay intervention.

    The displacement check is deliberately conservative: a directional action that
    moves vertically is useful wall traversal, and short taps/idle/story aren't failures.
    """
    if not after.get("controllable"):
        return ""
    same_room = before and room_key(before) == room_key(after)
    stop = result.get("stop")
    reasons = []
    if stop in ("death", "deviate"):
        reasons.append("本次动作死亡或偏离预期，不必等待再次失败")
    elif same_room and result.get("consumed", 0) >= 12:
        a = (before.get("player") or {}).get("pos")
        b = (after.get("player") or {}).get("pos")
        buttons = {part.strip() for line in (inputs or []) for part in line.split(",")[1:]}
        if a and b and bool(buttons & {"L", "R"}) and not {"L", "R"} <= buttons:
            if abs(b[0] - a[0]) <= 2 and abs(b[1] - a[1]) <= 2:
                reasons.append("本次方向输入后净位移很小，可能受阻（也可能路线回到了原点）")
    if decision_wait >= 20:
        reasons.append(f"上一次游戏工具前等待了 {decision_wait:.0f} 秒；这包含请求、输出和压缩，不能全归为思考")
    if not reasons:
        return ""
    return "【立即复查】" + "；".join(reasons) + "。" + REPLAN
