"""Opt-in real Codex receiver check for one thread across lifecycle stages.

Uses real runner dispatch, tool schemas, MCP consumers and simulation ledger.
Never connects to a game, OBS, Docker or an existing model conversation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.codex_agent import CodexRunner
from orchestrator.codex_rpc import CodexRPC
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


def stage_passed(result, expected_tools, expected_text, balance):
    calls = result['calls']
    enabled = result['stage'] == 'play_enabled'
    if (result.get('status') != 'completed'
            or [call['tool'] for call in calls] != expected_tools):
        return False
    for call in calls:
        response = call['response']
        text = '\n'.join(item.get('text', '') for item in response['contentItems'])
        if (response['success'] is not enabled or expected_text not in text
                or call['tool'] not in result['reply']):
            return False
        if not enabled and text not in result['reply']:
            return False
    return not enabled or re.search(r'(?<![\d.])' + str(balance) + r'(?![\d.])', result['reply']) is not None


async def check(model: str, directory: Path) -> bool:
    directory = directory.resolve()
    if (ROOT/'local').resolve() not in directory.parents:
        raise ValueError('Evidence must be a new directory beneath ignored local/')
    directory.mkdir(parents=True, exist_ok=False)
    report = {'model': model, 'thread': None, 'stages': [], 'bridge_calls': [],
              'boundary': 'Real Codex receiver, runner dispatch, MCP and ledger; no game connection.'}
    balance = secrets.randbelow(70) + 20
    stages = [
        ('prepare', False, False, ['celeste_simulate'], '只做知识库准备'),
        ('chat', False, False, ['celeste_exit_level'], '只提供聊天'),
        ('play_disabled', False, False, ['celeste_simulate', 'celeste_exit_level'], '未启用'),
        ('play_enabled', True, True, ['celeste_simulate'], 'balance'),
        ('paused', True, True, ['celeste_simulate'], '用户已暂停'),
    ]

    class NoBridge:
        def call(self, command, **args):
            report['bridge_calls'].append({'command': command, 'arguments': args})
            raise AssertionError('No game calls are permitted in this check')

        def close(self):
            pass

    try:
        for stage, live, sim, expected_tools, expected_text in stages:
            folder = directory/stage
            folder.mkdir()
            cfg = RunConfig(agent='session-lifecycle-acceptance', save='unused', model=model,
                            engine='codex', runtime='host', record=False, live=live,
                            sim_assist=sim, knowledge_profile='explore', cutscene_mode='auto')
            metrics = Metrics(folder/'metrics.json', cfg.agent, model, 'low', 180)
            session = GameSession(cfg, folder, metrics)
            session.client = NoBridge()
            if session.sim_energy:
                session.sim_energy.reconfigure(session.assist_rules, balance)
            server = build_server(session)
            runner = CodexRunner(cfg, folder, session, metrics, None, game_server=server,
                                 prepare_only=stage == 'prepare', chat_only=stage == 'chat',
                                 start_paused=stage == 'paused')
            result = {'stage': stage, 'calls': [], 'reply': '', 'resumed': bool(report['thread'])}
            report['stages'].append(result)
            done = asyncio.Event()

            def event(method, params):
                item = params.get('item') or {}
                if method == 'item/completed' and item.get('type') == 'agentMessage':
                    result['reply'] += item.get('text', '')
                if method == 'turn/completed':
                    result['status'] = (params.get('turn') or {}).get('status')
                    done.set()

            async def request(method, params):
                if params.get('tool') not in expected_tools:
                    raise RuntimeError('Only the requested isolated tools may be called')
                response = await runner._request(method, params)
                result['calls'].append({'tool': params.get('tool'), 'arguments': params.get('arguments'),
                                        'response': response})
                return response

            rpc = CodexRPC(folder/'rpc', on_event=event, on_request=request)
            runner.rpc = rpc
            try:
                await rpc.start()
                params = {'model': model, 'cwd': str(directory), 'sandbox': 'read-only',
                          'approvalPolicy': 'never', 'selectedCapabilityRoots': [],
                          'baseInstructions': 'You are an independent acceptance agent in an isolated fixture. '
                                              'Use only the tools requested in the latest user instruction. '
                                              'Never use files, shell, web, or a real game. Report observed results.',
                          'config': {'model_reasoning_effort': 'low'}}
                if report['thread']:
                    params['threadId'] = report['thread']
                    thread = await runner._resume_thread(params)
                    runner._validate_resumed_tools(thread['thread'])
                else:
                    params['dynamicTools'] = await runner.tool_specs()
                    result['registered_tools'] = [tool['name'] for tool in params['dynamicTools']]
                    thread = await rpc.request('thread/start', params)
                    report['thread'] = thread['thread']['id']
                if thread['thread']['id'] != report['thread']:
                    raise AssertionError('Lifecycle check must resume exactly the original thread')
                runner.session_id = report['thread']
                runner.turn_active = True
                prompt = ('This is stage ' + stage + '. Call each of these tools exactly once, in order, '
                          'using {} for simulate and {"reason":"isolated acceptance"} for exit_level: '
                          + ', '.join(expected_tools) + '. '
                          'For simulate do not submit candidates, only query its balance. Do not retry a refusal. '
                          'Then report each tool name, whether it succeeded, and the exact Chinese refusal reason '
                          'received; if it succeeds report the exact numeric balance from the result. '
                          'You must actually read this turn\'s returned values, not infer them from earlier stages.')
                turn = await rpc.request('turn/start', {'threadId': runner.session_id, 'effort': 'low',
                    'serviceTierForTurn': 'default', 'input': [{'type': 'text', 'text': prompt}]})
                runner.turn_id = turn['turn']['id']
                await asyncio.wait_for(done.wait(), 180)
                result['passed'] = stage_passed(result, expected_tools, expected_text, balance)
                print(json.dumps({'stage': stage, 'passed': result['passed'], 'reply': result['reply']},
                                 ensure_ascii=False), flush=True)
            finally:
                await rpc.close()
                session.close(release=False)
                runner.close_events()
        report['expected_balance'] = balance
        report['passed'] = all(stage['passed'] for stage in report['stages']) and not report['bridge_calls']
        return report['passed']
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['passed'] = False
        raise
    finally:
        (directory/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    return 0 if asyncio.run(check(args.model, args.output)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
