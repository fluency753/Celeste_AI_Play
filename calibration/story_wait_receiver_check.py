"""Opt-in real model receiver check for bounded story waiting.

Only the external game bridge is a fixture. Never connects to a live game,
OBS, Docker or an existing model conversation; evidence stays beneath local/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from calibration.story_wait_test import StoryBridge, observation
from orchestrator.codex_agent import CodexRunner
from orchestrator.codex_rpc import CodexRPC
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


def received_json(reply):
    reply = reply.strip()
    if reply.startswith('```'):
        reply = '\n'.join(reply.splitlines()[1:-1])
    return json.loads(reply)


async def check(model, directory):
    directory = directory.resolve()
    if (ROOT/'local').resolve() not in directory.parents:
        raise ValueError('Evidence must be in a new directory beneath ignored local/')
    directory.mkdir(parents=True, exist_ok=False)
    cfg = RunConfig(agent='story-wait-receiver', save='test', model=model, engine='codex',
                    runtime='host', record=False, live=False, collision_image='off',
                    knowledge_profile='explore', cutscene_mode='agent', chapter=3)
    metrics = Metrics(directory/'metrics.json', cfg.agent, model, 'low', 180)
    session = GameSession(cfg, directory, metrics)
    session.workspace = directory/'workspace'
    (session.workspace/'game/rooms').mkdir(parents=True)
    bridge = StoryBridge(observation())
    session.client, session.last_obs = bridge, observation()
    runner = CodexRunner(cfg, directory, session, metrics, None, game_server=build_server(session))
    report = {'model': model, 'thread': None, 'stages': [],
              'boundary': 'Real Codex receiver, runner, MCP and GameSession; external game bridge is a fixture.'}
    current = None
    done = asyncio.Event()

    def event(method, params):
        item = params.get('item') or {}
        if current is not None and method == 'item/completed' and item.get('type') == 'agentMessage':
            current['reply'] += item.get('text', '')
        if method == 'turn/completed':
            current['status'] = (params.get('turn') or {}).get('status')
            done.set()

    async def request(method, params):
        if (params.get('tool') != 'celeste_cutscene'
                or params.get('arguments') != {'action': current['action']}
                or current['calls']):
            raise RuntimeError('Only the single requested isolated cutscene call is allowed')
        before = bridge.frames
        response = await runner._request(method, params)
        current['calls'].append({'tool': params['tool'], 'arguments': params['arguments'],
                                 'frames_before': before, 'frames_after': bridge.frames,
                                 'response': response})
        return response

    rpc = CodexRPC(directory/'rpc', on_event=event, on_request=request)
    runner.rpc = rpc
    try:
        await rpc.start()
        thread = await rpc.request('thread/start', {
            'model': model, 'cwd': str(directory), 'sandbox': 'read-only',
            'approvalPolicy': 'never', 'selectedCapabilityRoots': [],
            'baseInstructions': 'You are an independent receiver acceptance agent in an isolated fixture. '
                                'Use only the single tool requested for each turn. No files, shell, web, '
                                'real game, retries or extra tool calls. Read the tool result and report it.',
            'config': {'model_reasoning_effort': 'low'}, 'dynamicTools': await runner.tool_specs()})
        report['thread'] = runner.session_id = thread['thread']['id']
        for name, action in [('unknown_watch', 'watch'), ('unknown_advance', 'advance'), ('memo_advance', 'advance')]:
            if name == 'memo_advance':
                bridge.obs = observation(node='memo')
                bridge.obs['ui'] = {'type': 'MemoPage', 'inputReady': True,
                                    'controls': ['C: confirm/next', 'U/D: scroll', 'B: close']}
                bridge.obs['storyPlaybackState'] = {'state': 'input', 'source': 'memo'}
                session.last_obs = bridge.obs.copy()
            current = {'stage': name, 'action': action, 'calls': [], 'reply': ''}
            report['stages'].append(current)
            done.clear()
            runner.turn_active, runner.turn_id = True, None
            prompt = ('Stage ' + name + '. Call celeste_cutscene exactly once with {"action":"' + action + '"}. '
                      'Then output ONLY a JSON object, no fences: {"stage":"' + name + '",'
                      '"reason":"a verbatim Chinese clause explaining the actual returned outcome",'
                      '"frames":<explicit input frames if reported, otherwise null>,'
                      '"auto_frames":<automatic waiting frames if reported, otherwise null>,'
                      '"input_node":<reported story_input if present, otherwise null>}. '
                      'For a failed confirmation report the actual refusal and the already-waited frame count '
                      'in auto_frames, and quote the clause explaining whether input was sent in reason; '
                      'never guess unseen values. Do not retry or call any other tool.')
            turn = await rpc.request('turn/start', {'threadId': runner.session_id, 'effort': 'low',
                'serviceTierForTurn': 'default', 'input': [{'type': 'text', 'text': prompt}]})
            runner.turn_id = turn['turn']['id']
            await asyncio.wait_for(done.wait(), 180)
            parsed = received_json(current['reply'])
            current['received'] = parsed
            calls = current['calls']
            if len(calls) != 1:
                raise AssertionError('Expected one actual tool call per stage')
            response = calls[0]['response']
            text = '\n'.join(item.get('text', '') for item in response['contentItems'])
            reason = parsed.get('reason', '')
            common = (current.get('status') == 'completed' and parsed.get('stage') == name
                      and len(reason) >= 4 and reason in text and parsed.get('auto_frames') == 0)
            if name == 'unknown_watch':
                passed = common and response['success'] and bridge.frames == 0 and '未知界面' in reason
                passed = passed and parsed.get('frames') == 0
            elif name == 'unknown_advance':
                passed = common and not response['success'] and bridge.frames == 0
                passed = passed and '输入未发送' in reason
            else:
                passed = common and response['success'] and parsed.get('frames') == 2
                passed = passed and parsed.get('input_node') == 'memo' and bridge.steps == [['1,C', '1']]
            current['passed'] = bool(passed)
            print(json.dumps({'stage': name, 'passed': passed, 'reply': parsed}, ensure_ascii=False), flush=True)
        report.update(steps=bridge.steps, frames=bridge.frames)
        report['passed'] = all(stage['passed'] for stage in report['stages']) and bridge.steps == [['1,C', '1']]
        return report['passed']
    except Exception as exc:
        report.update(error=f'{type(exc).__name__}: {exc}', passed=False)
        raise
    finally:
        await rpc.close()
        session.close(release=False)
        runner.close_events()
        (directory/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    return 0 if asyncio.run(check(args.model, args.output)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
