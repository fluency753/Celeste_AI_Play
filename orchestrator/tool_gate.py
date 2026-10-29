"""One pre-execution policy for Claude hooks and native Codex tools."""


async def decide_tool(gate, tool_name, tool_use_id, *, metrics=None, live_remind_s=None, session=None):
    guard = getattr(session, 'program_guard', getattr(session, 'control_guard', None))
    if tool_name == 'Bash' and guard and (reason := guard()):
        return {'decision': 'deny', 'reason': reason, 'context': ''}
    allow, reason = await gate.decide(tool_name, tool_use_id)
    if allow and tool_name == 'Bash' and session is not None and hasattr(session, 'program_run_denied'):
        if denied := session.program_run_denied():
            return {'decision': 'deny', 'reason': denied, 'context': ''}
    context = ''
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
