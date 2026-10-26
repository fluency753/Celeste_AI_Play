"""Opt-in live Codex transport check. No game, Docker, OBS or persistent thread."""
import argparse
import asyncio
import base64
import io
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw, ImageFont
from mcp.types import CallToolResult

from orchestrator.codex_agent import CodexRunner
from orchestrator.codex_rpc import CodexRPC
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics


async def check(model, directory):
    directory.mkdir(parents=True, exist_ok=False)
    expected, blocks = [], [{'type': 'text', 'text': '[frame 901] isolated observation fixture'}]
    for role in ('screenshot', 'collision'):
        digits = ''.join(str(secrets.randbelow(10)) for _ in range(4))
        expected.append(digits)
        picture = Image.new('RGB', (380, 150), 'white')
        font_path = Path('C:/Windows/Fonts/arialbd.ttf')
        font = ImageFont.truetype(str(font_path), 100) if font_path.exists() else ImageFont.load_default(size=90)
        ImageDraw.Draw(picture).text((30, 12), digits, font=font, fill='black')
        picture.save(directory/(role+'.png'))
        stream = io.BytesIO()
        picture.save(stream, format='PNG')
        blocks.append({'type': 'image', 'mimeType': 'image/png',
                       'data': base64.b64encode(stream.getvalue()).decode(),
                       '_meta': {'celeste': {'frame': 901, 'sid': 'fixture', 'room': 'fixture', 'role': role}}})
    fixture = CallToolResult.model_validate({'content': blocks})
    cfg = RunConfig(agent='image-transport-check', save='unused', model=model, engine='codex')
    metrics = Metrics(directory/'metrics.json', cfg.agent, model, 'low', 180)
    runner = CodexRunner(cfg, directory, None, metrics, None,
                         game_server=SimpleNamespace(call_tool=AsyncMock(return_value=fixture)))
    state = {'calls': 0, 'reply': '', 'image_inputs': 0, 'outer_chars': 0, 'base64_text_outputs': 0}
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
            state['image_inputs'] += sum(b.get('type') == 'input_image' for b in content if isinstance(b, dict))
            if item.get('type') == 'custom_tool_call_output':
                output = item.get('output') or []
                text = output if isinstance(output, str) else '\n'.join(b.get('text', '') for b in output if isinstance(b, dict))
                state['outer_chars'] += len(text)
                state['base64_text_outputs'] += int('data:image/' in text or 'iVBORw0KGgo' in text)
    async def request(method, params):
        if params.get('tool') != 'celeste_observe':
            raise RuntimeError('Only fixture observation is allowed')
        state['calls'] += 1
        response = await runner._request(method, params)
        state['tool_result_chars'] = len(json.dumps(response))
        state['tool_result_contains_images'] = 'data:image/' in json.dumps(response)
        return response
    rpc = CodexRPC(directory, on_event=event, on_request=request)
    runner.rpc = rpc
    try:
        await rpc.start()
        thread = await rpc.request('thread/start', {'model': model, 'ephemeral': True, 'experimentalRawEvents': True,
            'cwd': str(directory.resolve()), 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'baseInstructions': 'This is an isolated visual transport test. Only call the client celeste_observe tool.',
            'selectedCapabilityRoots': [], 'config': {'model_reasoning_effort': 'low'},
            'dynamicTools': [{'name': 'celeste_observe', 'description': 'Get a fixture observation. Application image attachments arrive in the same turn, not in the tool value.',
                              'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]})
        runner.session_id = thread['thread']['id']
        runner.turn_active = True
        turn = await rpc.request('turn/start', {'threadId': runner.session_id, 'effort': 'low', 'serviceTierForTurn': 'default',
            'input': [{'type': 'text', 'text': 'Call celeste_observe exactly once through the code tool and deliberately print its result with text(r). Read the four digits in each application image attachment. Reply ONLY screenshot digits, a space, then collision digits. Do not guess or call any other tools.'}]})
        runner.turn_id = turn['turn']['id']
        print('Running isolated production-adapter image test', flush=True)
        await asyncio.wait_for(done.wait(), 180)
        state.update(expected=' '.join(expected), thread=runner.session_id, turn=runner.turn_id,
                     context_tokens=metrics.state.context_tokens)
        state['passed'] = (state['reply'].strip() == state['expected'] and state['calls'] == 1
                           and state['image_inputs'] == 2 and not state['base64_text_outputs']
                           and not state['tool_result_contains_images'] and state['status'] == 'completed')
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return state['passed']
    finally:
        await rpc.close()
        (directory/'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True, help='New local evidence directory')
    args = parser.parse_args()
    return 0 if asyncio.run(check(args.model, args.output)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
