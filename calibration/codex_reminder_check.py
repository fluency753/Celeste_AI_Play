"""Opt-in real receiver check for reminders hidden by code-mode tool returns.

Uses the production CodexRunner dispatch/receipts and real App Server/model.
Only the file worker is replaced; no game, Docker or model catalog is contacted.
The host RPC transport is an acceptance fixture, not a production sandbox check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.codex_agent import CodexRunner, LIVE_NOTE
from orchestrator.codex_rpc import CodexRPC
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics


async def check(model: str, directory: Path) -> bool:
    directory = directory.resolve()
    if (ROOT / 'local').resolve() not in directory.parents:
        raise ValueError('Evidence must be in a new ignored local/ directory')
    directory.mkdir(parents=True, exist_ok=False)
    report = {'model': model, 'stages': [], 'boundary': __doc__}
    cfg = RunConfig(agent='reminder-receiver', save='unused', model=model, engine='codex', live=True)
    cfg.live_remind_s = 1
    metrics = Metrics(directory / 'metrics.json', cfg.agent, model, 'low', 180)
    runner = CodexRunner(cfg, directory, None, metrics, None)
    done = asyncio.Event()
    stage = None

    def event(method, params):
        runner._event(method, params)
        if stage is None:
            return
        item = params.get('item') or {}
        if method == 'item/completed' and item.get('type') == 'agentMessage':
            stage['reply'] += item.get('text', '')
        if method in ('item/started', 'item/completed') and item.get('type') == 'userMessage':
            stage['recorded_inputs'].append(item)
        if method == 'rawResponseItem/completed' and item.get('type') == 'custom_tool_call_output':
            # Retain final wrapper outputs, never reasoning or raw model input.
            stage['outer_tool_outputs'].append(item.get('output'))
        if method == 'turn/completed':
            stage['status'] = (params.get('turn') or {}).get('status')
            done.set()

    async def request(method, params):
        if params.get('tool') != stage['tool'] or stage['calls']:
            raise RuntimeError('Only one requested isolated tool is allowed')
        stage['calls'] += 1
        # Simulate only the external file worker, retaining gate/dispatch/consumer.
        with patch('orchestrator.codex_agent.read_file', return_value={'kind': 'error', 'error': 'fixture missing'}):
            response = await runner._request(method, params)
        stage['tool_response'] = response
        stage['pending_after_queue'] = bool(runner.note_reminders.peek())
        return response

    rpc = CodexRPC(directory / 'rpc', on_event=event, on_request=request)
    runner.rpc = rpc
    try:
        await rpc.start()
        thread = await rpc.request('thread/start', {
            'model': model, 'ephemeral': True, 'experimentalRawEvents': True,
            'cwd': str(directory), 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'selectedCapabilityRoots': [], 'dynamicTools': await runner.tool_specs(),
            'baseInstructions': 'You are an independent receiver in an isolated acceptance fixture. '
                                'Call only the requested client tool once. Never use shell, files, web, or game. '
                                'Use code-mode and discard the entire tool return without printing it. '
                                'After receiving an application reminder, follow the user reporting instructions.',
            'config': {'model_reasoning_effort': 'low'}})
        runner.session_id = thread['thread']['id']
        for tool, args in [('celeste_narrate', {'thought': 'isolated acceptance'}),
                           ('read', {'file_path': '/workspace/missing'}), ('celeste_observe', {})]:
            secret = secrets.token_hex(12)
            stage = {'tool': tool, 'calls': 0, 'reply': '', 'expected_secret': secret,
                     'recorded_inputs': [], 'outer_tool_outputs': []}
            report['stages'].append(stage)
            runner.chat_only = tool == 'celeste_observe'
            runner.note_reminders.pending['receiver'] = (1, '接收校验码：' + secret)
            # A real elapsed reminder is due on the read/refusal stages; narrate resets its clock.
            metrics.restore_live_reminder(30.0)
            done.clear()
            runner.turn_done.clear()
            runner.turn_id, runner.turn_active = None, True
            prompt = ('Call ' + tool + ' exactly once with ' + json.dumps(args) + '. '
                      'In code-mode await the call but do not print/text/inspect its return. '
                      'Then report the exact fresh 接收校验码 from the separate application reminder. '
                      'If the reminder asks for public narration, quote that instruction too. '
                      'Never guess a code or reuse a prior stage code. Do not retry the tool.')
            turn = await rpc.request('turn/start', {'threadId': runner.session_id, 'effort': 'low',
                'input': [{'type': 'text', 'text': prompt}]})
            runner.turn_id = turn['turn']['id']
            await asyncio.wait_for(done.wait(), 180)
            outer = json.dumps(stage['outer_tool_outputs'], ensure_ascii=False)
            stage['passed'] = (stage['calls'] == 1 and stage.get('status') == 'completed'
                and secret in stage['reply'] and not runner.note_reminders.peek()
                and secret not in json.dumps(stage['tool_response']) and secret not in outer
                and bool(stage['outer_tool_outputs'])
                and (tool == 'celeste_narrate' or '请先更新一句公开解说，再继续规划' in stage['reply']))
            print(json.dumps({'tool': tool, 'passed': stage['passed'], 'reply': stage['reply']}, ensure_ascii=False), flush=True)
        report['passed'] = all(row['passed'] for row in report['stages'])
        return report['passed']
    finally:
        await rpc.close()
        runner.close_events()
        (directory / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(args.model, args.output)) else 1)
