"""One pre-execution policy for Claude hooks and native Codex tools."""

from .execution import WORKSPACE_TOOLS


async def decide_tool(gate, tool_name, tool_use_id, *, metrics=None, live_remind_s=None, session=None,
                      reminders=True, execution_guard=None):
    """reminders=False：调用方自己把提醒放进模型确定会收到的结果，这里不取、不确认送达（Codex）。"""
    if execution_guard and (reason := execution_guard()):
        return {'decision': 'deny', 'reason': reason, 'context': ''}
    # Gameplay phase and planning budgets do not own workspace capability.
    allow, reason = (True, '') if tool_name in WORKSPACE_TOOLS else await gate.decide(tool_name, tool_use_id)
    if allow and execution_guard and (denied := execution_guard()):
        return {'decision': 'deny', 'reason': denied, 'context': ''}
    if not allow and getattr(session, 'workspace_paused', lambda: False)():
        from .pausegate import PauseGate
        if isinstance(gate, PauseGate):
            reason = gate.deny_reason(workspace_paused=True)
    context = ''
    if not reminders:
        return {'decision': 'allow' if allow else 'deny', 'reason': reason, 'context': context}
    if allow and live_remind_s and metrics is not None:
        if tool_name.endswith('__narrate'):
            metrics.narrated()
        elif (waited := metrics.take_live_reminder(live_remind_s)) is not None:
            from .server import narrate_reminder
            context = narrate_reminder(getattr(session, 'cfg', None), waited)
            print(f'[live] 提醒更新 HUD 思考（已思考 {waited:.0f} 秒，调用 {tool_name}）', flush=True)
    notes = getattr(session, 'note_reminders', None)
    offered = notes.peek() if allow and notes is not None else ()
    if offered:
        context = '\n'.join(value for value in (context, '\n'.join(text for _, text in offered)) if value)
    if offered:
        notes.acknowledge(offered)
    return {'decision': 'allow' if allow else 'deny', 'reason': reason, 'context': context}
