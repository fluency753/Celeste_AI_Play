"""Opt-in real Codex receiver check; only the external game bridge is a fixture.

No game, OBS, Docker or existing model session is connected. Evidence stays in
an explicitly chosen new directory below local/. Requires a logged-in Codex CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
from pathlib import Path
import secrets
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from calibration.collision_contract_test import FakeBridge, observation
from orchestrator.codex_agent import CodexRunner
from orchestrator.codex_rpc import CodexRPC
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


async def check(model: str, directory: Path) -> bool:
    directory = directory.resolve()
    if (ROOT / 'local').resolve() not in directory.parents:
        raise ValueError('Evidence directory must be below this repository\'s ignored local/')
    directory.mkdir(parents=True, exist_ok=False)
    code = ''.join(str(secrets.randbelow(10)) for _ in range(4))
    picture = Image.new('RGB', (380, 150), 'white')
    font = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 100)
    ImageDraw.Draw(picture).text((30, 12), code, font=font, fill='black')
    picture.save(directory / 'receiver-secret.png')
    stream = io.BytesIO()
    picture.save(stream, format='PNG')
    encoded = base64.b64encode(stream.getvalue()).decode()

    class Bridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.calls = []

        def call(self, command, **options):
            self.calls.append((command, options))
            if command == 'hello':
                return {'action_policy': 1, 'entry_guard': True, 'input_formats': ['celeste_ai']}
            result = super().call(command, **options)
            if options.get('image'):
                result['image'] = encoded
            return result

    cfg = RunConfig(agent='reload-model-acceptance', save='unused', model=model, engine='codex',
                    runtime='host', record=False, collision_image='off', cutscene_mode='agent')
    # This fixture tests the reload gate, independently of normal 40-frame minimums.
    cfg.min_frames_per_act = 1
    metrics = Metrics(directory / 'metrics.json', cfg.agent, model, 'low', 180)
    session = GameSession(cfg, directory, metrics)
    bridge = Bridge()
    session.client = bridge
    session.entry_guard_supported = True
    session.chapter_reset_guard_supported = True
    session.action_policy_supported = 1
    server = build_server(session)
    session.after_bridge_reload(observation())
    runner = CodexRunner(cfg, directory, session, metrics, None, game_server=server)
    runner.turn_active = True
    state = {'boundary': 'Real Codex model + production CodexRunner/MCP GameSession; only the external '
             'game bridge is a fixture. No live game connection.', 'model': model, 'calls': [],
             'reply': '', 'image_inputs': 0, 'base64_text_outputs': 0}
    done = asyncio.Event()

    def event(method, params):
        runner._event(method, params)
        item = params.get('item') or {}
        if method == 'item/completed' and item.get('type') == 'agentMessage':
            state['reply'] += item.get('text', '')
        if method == 'turn/completed':
            state['status'] = (params.get('turn') or {}).get('status')
            done.set()
        if method == 'rawResponseItem/completed':
            content = item.get('content') or []
            state['image_inputs'] += sum(isinstance(b, dict) and b.get('type') == 'input_image'
                                         for b in content)
            if item.get('type') == 'custom_tool_call_output':
                output = item.get('output') or []
                text = output if isinstance(output, str) else '\n'.join(
                    b.get('text', '') for b in output if isinstance(b, dict))
                state['base64_text_outputs'] += int('data:image/' in text or 'iVBORw0KGgo' in text)

    async def request(method, params):
        name = params.get('tool')
        if name not in {'celeste_act', 'celeste_observe'}:
            raise RuntimeError('Only isolated observe/act tools are allowed')
        before = sum(command == 'step' for command, _ in bridge.calls)
        response = await runner._request(method, params)
        state['calls'].append({'tool': name, 'arguments': params.get('arguments'), 'response': response,
                               'steps_before': before,
                               'steps_after': sum(command == 'step' for command, _ in bridge.calls),
                               'required': session.reload_observation_required})
        return response

    rpc = CodexRPC(directory, on_event=event, on_request=request)
    runner.rpc = rpc
    try:
        await rpc.start()
        tools = [
            {'name': 'celeste_act', 'description': 'Advance the isolated fixture; first observe is required after reload.',
             'inputSchema': {'type': 'object', 'properties': {
                 'inputs': {'type': 'array', 'items': {'type': 'string'}}, 'thought': {'type': 'string'}},
                 'required': ['inputs', 'thought'], 'additionalProperties': False}},
            {'name': 'celeste_observe', 'description': 'Read the fixture; its screenshot arrives as a same-turn native image.',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        ]
        thread = await rpc.request('thread/start', {
            'model': model, 'ephemeral': True, 'experimentalRawEvents': True, 'cwd': str(directory),
            'sandbox': 'read-only', 'approvalPolicy': 'never',
            'baseInstructions': 'You are an independent acceptance agent. Use only the two provided client game tools. '
                                'This is an isolated fixture, never a user game.',
            'selectedCapabilityRoots': [], 'config': {'model_reasoning_effort': 'low'}, 'dynamicTools': tools})
        runner.session_id = thread['thread']['id']
        prompt = ('Verify reload recovery in this order: (1) intentionally call celeste_act with inputs ["1,R"] '
                  'and thought "probe stale plan" once before observing, and inspect the rejection; '
                  '(2) call celeste_observe with no arguments, deliberately print the returned value using text(r), '
                  'then READ the four random digits from the actual application screenshot attachment; '
                  '(3) after seeing the image, call celeste_act with inputs ["1,R"] and thought '
                  '"new action after fresh observation" once. Finish with a short report containing the four image '
                  'digits, why the first act was refused, and whether the final act executed. Do not read files, '
                  'use shell, guess digits or call anything else.')
        turn = await rpc.request('turn/start', {'threadId': runner.session_id, 'effort': 'low',
            'serviceTierForTurn': 'default', 'input': [{'type': 'text', 'text': prompt}]})
        runner.turn_id = turn['turn']['id']
        print('Running isolated real-model reload receiver check', flush=True)
        await asyncio.wait_for(done.wait(), 180)
        calls = state['calls']
        state.update(expected_image_code=code, thread=runner.session_id, turn=runner.turn_id,
                     bridge_steps=sum(command == 'step' for command, _ in bridge.calls))
        state['passed'] = (state.get('status') == 'completed'
                           and [c['tool'] for c in calls] == ['celeste_act', 'celeste_observe', 'celeste_act']
                           and calls[0]['steps_after'] == 0 and calls[-1]['steps_after'] == 1
                           and calls[0]['response']['success'] is False
                           and '桥接已重载' in json.dumps(calls[0]['response'], ensure_ascii=False)
                           and calls[1]['response']['success'] is True
                           and calls[2]['response']['success'] is True
                           and code in state['reply'] and state['image_inputs'] >= 1
                           and not state['base64_text_outputs'])
        print(json.dumps({k: v for k, v in state.items() if k != 'calls'}, ensure_ascii=False), flush=True)
        return state['passed']
    except Exception as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        await rpc.close()
        session.close(release=False)
        runner.close_events()
        (directory / 'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory below ignored local/')
    args = parser.parse_args()
    return 0 if asyncio.run(check(args.model, args.output)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
