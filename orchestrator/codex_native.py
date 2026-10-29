"""Adapt native Codex hooks to the same tool gate used by Claude Code."""
import asyncio
import math
import secrets


def _denied(reason):
    return {'decision': 'deny', 'reason': reason, 'context': ''}


def _timeout(cfg, arguments):
    from .agent import bash_limits_ms
    maximum = bash_limits_ms(cfg)[1] / 1000
    requested = arguments.get('timeout_ms')
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) and math.isfinite(requested) and requested > 0:
        maximum = min(maximum, requested / 1000)
    return max(0.001, maximum)


async def native_tool_hook(runner, data):
    """Return a gate decision; execution and result delivery remain native Codex tools.

    Pre/Post adaptation owns timing and replay bookkeeping. Business rules belong
    to tool_gate.decide_tool, not to a second Codex-specific permission policy.
    """
    lock = runner.__dict__.setdefault('_native_hook_lock', asyncio.Lock())
    async with lock:
        token = data.get('token')
        if (not isinstance(token, str) or not getattr(runner, 'native_token', None)
                or not secrets.compare_digest(token, runner.native_token)):
            return _denied('原生工具门禁身份验证失败；本次未执行。')
        event, name = data.get('hook_event_name'), data.get('tool_name')
        ident = str(data.get('tool_use_id') or '')
        if event not in ('PreToolUse', 'PostToolUse') or name not in ('Bash', 'apply_patch', 'view_image') or not ident:
            return _denied('无效的原生工具门禁请求；本次未执行。')
        if data.get('session_id') != runner.session_id:
            return _denied('原生工具所属会话不匹配；本次未执行。')
        calls = runner.__dict__.setdefault('_native_tool_calls', {})
        previous = calls.get(ident)
        if previous and (previous['name'] != name or previous['turn'] != data.get('turn_id')):
            return _denied('工具回执与原请求不匹配；请核对状态。')
        if event == 'PostToolUse':
            if previous and previous['running']:
                previous['running'] = False
                runner.metrics.tool_end(ident)
                if runner.playback and previous.get('canonical', name) == 'Bash':
                    runner.playback.note_tool_result(ident)
                notes = getattr(getattr(runner, 'native_session', runner.session), 'note_reminders', None)
                if notes is not None:
                    notes.observe_file()
            return {'decision': 'allow', 'reason': '', 'context': ''}
        turn = data.get('turn_id')
        if (not runner.turn_active or runner.turn_done.is_set() or not turn
                or (runner.turn_id is not None and turn != runner.turn_id)):
            return _denied('发起此请求的轮次已结束，本次未执行；等待当前轮次的新指令。')
        session = getattr(runner, 'native_session', runner.session)
        if previous:
            # Revalidate protocol ownership before reusing an in-flight receipt.
            # A completed tool must never become a fresh execution authorization.
            if not previous['running']:
                if previous['decision'].get('decision') == 'allow':
                    return _denied('此工具调用已经执行完毕，不重复执行；请读取原结果。')
                return previous['decision']
            guard = getattr(session, 'program_guard', getattr(session, 'control_guard', None))
            if previous.get('canonical', name) == 'Bash' and guard and (reason := guard()):
                return _denied(reason)
            return previous['decision']
        arguments = data.get('tool_input') or {}
        if not isinstance(arguments, dict):
            return _denied('原生工具参数必须是对象；本次未执行。')
        if name == 'Bash' and not isinstance(arguments.get('command'), str):
            return _denied('原生 Shell 缺少 command；本次未执行。')
        if name == 'Bash':
            shell = arguments.get('shell')
            if shell is not None and shell not in ('/bin/bash', '/bin/sh', '/usr/bin/bash', '/usr/bin/sh'):
                return _denied('原生 Shell 只能使用受信的系统 shell；本次未执行。')
            if any(arguments.get(key) not in (None, False) for key in ('tty', 'pty')):
                return _denied('本次只开放受限时控制的前台程序，不支持 PTY；本次未执行。')
            if arguments.get('login') not in (None, False):
                return _denied('原生程序使用非登录 shell，请省略 login 或设为 false；本次未执行。')
        runner.metrics.add(tool_calls=1)
        from .tool_gate import decide_tool
        canonical = {'Bash': 'Bash', 'apply_patch': 'Write', 'view_image': 'Read'}[name]
        from .native_reading import parse_read_command
        intent = parse_read_command(arguments['command'], data.get('cwd') or '/workspace') if name == 'Bash' else None
        if intent is not None:
            canonical = 'Read'
        sync = getattr(runner, '_sync_native_reads', None)
        if sync is not None:
            await sync()
        gate = runner.gate
        if gate is None:
            from .pausegate import PauseGate
            gate = runner.__dict__.get('_native_gate')
            if gate is None:
                gate = runner._native_gate = PauseGate(runner.metrics, enabled=False)
        gate.note_tool_use(ident, ident)
        decision = await decide_tool(gate, canonical, ident, metrics=runner.metrics,
                live_remind_s=runner.cfg.live_remind_s if runner.cfg.live else None,
                session=session)
        if decision.get('decision') == 'allow':
            decision = {**decision, 'timeout_s': _timeout(runner.cfg, arguments)}
            detail = arguments.get('description') or arguments.get('command') or arguments.get('path') or ''
            runner.metrics.tool_begin(ident, game_tool=False,
                                      label=name + (' · ' + ' '.join(str(detail).split())[:120] if detail else ''))
            if runner.playback and canonical == 'Bash':
                runner.playback.note_tool_use(ident, ident, 'Bash', arguments)
        calls[ident] = {'name': name, 'turn': turn, 'decision': decision,
                        'canonical': canonical,
                        'read_command': arguments['command'] if intent is not None else None,
                        'input_keys': sorted(arguments),
                        'running': decision.get('decision') == 'allow'}
        # Keep in-flight calls, with a small retry window for completed requests.
        if len(calls) > 1024:
            for old in list(calls):
                if old != ident and not calls[old]['running']:
                    del calls[old]
                    if len(calls) <= 512:
                        break
        return decision


def finish_native_tools(runner):
    """Balance timing if interruption/shutdown prevents native PostToolUse delivery."""
    for ident, call in runner.__dict__.get('_native_tool_calls', {}).items():
        if call['running']:
            call['running'] = False
            runner.metrics.tool_end(ident)
            if runner.playback and call.get('canonical', call['name']) == 'Bash':
                runner.playback.note_tool_result(ident)
