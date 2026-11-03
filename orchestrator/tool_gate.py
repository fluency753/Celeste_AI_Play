"""Authorize executor requests and deliver Claude hook reminders.

Simulation allowance belongs to the host simulation entry, not this transport.
"""


async def decide_tool(tool_name, *, metrics=None, live_remind_s=None, session=None,
                      reminders=True, execution_guard=None):
    """reminders=False：调用方自己把提醒放进模型确定会收到的结果，这里不取、不确认送达（Codex）。"""
    if execution_guard and (reason := execution_guard()):
        return {'decision': 'deny', 'reason': reason, 'context': ''}
    context = ''
    if not reminders:
        return {'decision': 'allow', 'reason': '', 'context': context}
    if live_remind_s and metrics is not None:
        if tool_name.endswith('__narrate'):
            metrics.narrated()
        elif (waited := metrics.take_live_reminder(live_remind_s)) is not None:
            from .server import narrate_reminder
            context = narrate_reminder(getattr(session, 'cfg', None), waited)
            print(f'[live] 提醒更新 HUD 思考（已思考 {waited:.0f} 秒，调用 {tool_name}）', flush=True)
    notes = getattr(session, 'note_reminders', None)
    offered = notes.peek() if notes is not None else ()
    if offered:
        context = '\n'.join(value for value in (context, '\n'.join(text for _, text in offered)) if value)
    if offered:
        notes.acknowledge(offered)
    return {'decision': 'allow', 'reason': '', 'context': context}
