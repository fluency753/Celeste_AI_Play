"""Authorize executor requests and deliver Claude hook reminders.

Simulation allowance belongs to the host simulation entry, not this transport.
"""
import re


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
    from .token_drift import token_observe_notice
    if notice := token_observe_notice(session):
        from .game_policy import GAME_ACTIONS
        if tool_name.removeprefix('mcp__celeste__') not in GAME_ACTIONS:
            context = '\n'.join(value for value in (context, notice) if value)
    return {'decision': 'allow', 'reason': '', 'context': context}


GATE_HOST = re.compile(r'(127\.0\.0\.1|localhost|host\.docker\.internal)(:\d+)?')


def add_gate_route(app, metrics=None, live_remind_s: float | None = None, session=None) -> None:
    """容器里的 PreToolUse 钩子（agent-container/celeste-gate.py）每次调用工具前 POST 到这里。
    实时展示时（live_remind_s 不为空）兼作解说更新计时器的提醒通道：思考状态累计满 live_remind_s 秒没有 narrate，
    回复里带上 context，钩子把它作为 additionalContext 交给 Claude Code，出现在这次工具结果旁边。"""
    from starlette.requests import Request  # noqa: WPS433
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def endpoint(request: Request):
        # 只认容器钩子的请求：JSON 类型（网页不经预检发不出这种请求）且 Host 是本机或容器看到的宿主名
        # （网页把自己的域名解析到本机再来访问时，Host 仍是它的域名）。提醒一经取出就算送达，不能让外人取走。
        content_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
        if content_type != 'application/json' or not GATE_HOST.fullmatch(request.headers.get('host', '').strip().lower()):
            return JSONResponse({'decision': 'deny', 'reason': '工具门禁只接受本机钩子的 JSON 请求，本次未执行。', 'context': ''},
                                status_code=403)
        try:
            data = await request.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            return JSONResponse({'decision': 'deny', 'reason': '工具门禁请求必须是对象，本次未执行。', 'context': ''})
        if data.get('engine') == 'codex':
            callback = getattr(session, 'native_tool_hook', None)
            if callback is None:
                return JSONResponse({'decision': 'deny', 'reason': '没有正在接管的 Codex 会话', 'context': ''})
            return JSONResponse(await callback(data))
        owner_guard = getattr(session, 'workspace_hook_guard', None)
        if owner_guard is None and session is not None:
            return JSONResponse({'decision': 'deny', 'reason': '没有正在接管的 Claude 工具会话，本次未执行。', 'context': ''})
        return JSONResponse(await decide_tool(str(data.get('tool_name') or ''),
                                              metrics=metrics, live_remind_s=live_remind_s, session=session,
                                              execution_guard=(lambda: owner_guard(data)) if owner_guard else None))

    app.router.routes.insert(0, Route("/gate", endpoint, methods=["POST"]))
