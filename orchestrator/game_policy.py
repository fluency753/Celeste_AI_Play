"""One gameplay permission policy; transports separately own request identity."""
from __future__ import annotations

GAME_ACTIONS = frozenset({'observe', 'act', 'cutscene', 'exit_level', 'travel', 'simulate', 'finish_goal'})
CONTROL_TOOLS = frozenset({'narrate', 'set_effort'})
REPORT_TOOLS = frozenset({'stop_run'})


def tool_name(name):
    return str(name).removeprefix('mcp__celeste__').removeprefix('celeste_')


def is_game_tool(name):
    return tool_name(name) in GAME_ACTIONS | CONTROL_TOOLS | REPORT_TOOLS


def effort_denial(controller, value):
    fixed = getattr(controller, 'manual_effort', None)
    if fixed and value != fixed:
        return f'用户已固定推理强度为 {fixed}；先在 HUD 恢复自动调整。'
    return None


def denial(controller, name, arguments=None):
    """Read current facts rather than maintaining another lifecycle state machine."""
    requested = str(name)
    name = tool_name(name)
    mismatch = getattr(controller, 'model_mismatch', None)
    if mismatch:
        # 服务模型被换掉（agent.ClaudeRunner._model_switched）：换回指定模型之前什么都不执行，包括 HUD 思考语句
        return (f"服务模型被自动换成了 {mismatch['actual']}（指定 {mismatch['expected']}），主控已拦下，"
                f"本次 {requested} 未执行。")
    if name in CONTROL_TOOLS:
        return effort_denial(controller, arguments.get('level')) if name == 'set_effort' and arguments else None
    if name not in GAME_ACTIONS | REPORT_TOOLS:
        return f'当前未开放工具 {name}，本次未执行。'
    session = controller.session
    if getattr(controller, 'prepare_only', False):
        return '本次仅作知识库准备，不允许操作游戏或报告试玩阻塞。'
    if controller.chat_only:
        return '当前为聊天阶段，没有在执行试玩，本次游戏工具未执行。'
    if session is None:
        return '当前没有接管试玩，本次游戏工具未执行。'
    goals = getattr(session, 'goals', None)
    stop_motion = getattr(goals, 'stop_motion', None)
    if (getattr(session, 'done', False) or controller.goal_finishing
            or stop_motion is not None and stop_motion.is_set()):
        return '本次试玩已结束或正在收尾，本次游戏工具未执行。'
    if controller.stop_requested:
        return '用户已中断本次运行，本次游戏工具未执行。'
    if name == 'stop_run':
        return None
    if getattr(controller, 'model_waiting', False):
        return '模型服务恢复等待中，本次游戏工具未执行。'
    if controller.paused or getattr(controller, 'pausing', False):
        return '用户已暂停试玩，不能观察、操作游戏或请求模拟预测；工作区仍可使用。'
    from .modes import game_tools
    if name not in game_tools(controller.cfg):
        return f'当前试玩配置未启用 {requested}，本次未执行；请使用当前已启用的工具。'
    return None
