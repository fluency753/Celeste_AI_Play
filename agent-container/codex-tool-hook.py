#!/usr/bin/env python3
"""Translate native Codex tool hooks to the existing controller gate; fail closed."""
import base64
import json
import math
import os
import shlex
import sys
import urllib.request


def deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def post_notice():
    message = '工具已执行，但主控未确认收尾回执；已完成操作不要重做，请核对结果并报告。'
    return {'systemMessage': message, 'hookSpecificOutput': {
            'hookEventName': 'PostToolUse', 'additionalContext': message}}


def translate(data, decision):
    event = data.get('hook_event_name')
    if event == 'PostToolUse':
        if decision.get('decision') != 'allow':
            return post_notice()
        return {}
    if decision.get('decision') != 'allow':
        return deny(str(decision.get('reason') or '主控未允许本次工具调用；没有执行。'))
    result = {'hookEventName': 'PreToolUse'}
    if decision.get('context'):
        result['additionalContext'] = str(decision['context'])
    if data.get('tool_name') == 'Bash':
        source = data.get('tool_input') or {}
        command = source.get('command')
        seconds = decision.get('timeout_s')
        if not isinstance(command, str) or not command.strip():
            return deny('原生 Shell 缺少有效 command，本次未执行。')
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            return deny('主控未提供有效的程序时长上限，本次未执行。')
        encoded = base64.b64encode(command.encode('utf-8')).decode('ascii')
        wrapped = ('/usr/bin/python3 /usr/local/lib/celeste/codex-shell.py --seconds '
                   + shlex.quote(f'{seconds:g}') + ' --command-base64 ' + shlex.quote(encoded))
        result.update(permissionDecision='allow', updatedInput={'command': wrapped})
    return {'hookSpecificOutput': result}


def main():
    event = None
    try:
        data = json.load(sys.stdin)
        event = data.get('hook_event_name')
        if event not in ('PreToolUse', 'PostToolUse'):
            raise ValueError('未知的原生工具钩子阶段')
        port = int(os.environ.get('MCP_PORT', '47780'))
        if not 1 <= port <= 65535:
            raise ValueError('无效的主控端口')
        payload = {key: data.get(key) for key in ('hook_event_name', 'session_id', 'turn_id',
                    'tool_use_id', 'tool_name', 'tool_input', 'cwd')}
        payload['engine'] = 'codex'
        payload['token'] = os.environ['CELESTE_NATIVE_TOKEN']
        request = urllib.request.Request(f'http://host.docker.internal:{port}/gate',
                  data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        # Ignore user proxy variables for the trusted local controller channel.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=25) as response:
            decision = json.load(response)
        print(json.dumps(translate(data, decision), ensure_ascii=False))
        return 0
    except Exception as exc:
        reason = f'主控工具门禁不可用：{type(exc).__name__}。本次未获执行授权，请结束本轮并报告。'
        if event == 'PostToolUse':
            # The operation has already happened; never describe it as unexecuted.
            print(json.dumps(post_notice(), ensure_ascii=False))
        else:
            print(json.dumps(deny(reason), ensure_ascii=False))
        return 0


if __name__ == '__main__':
    sys.exit(main())
