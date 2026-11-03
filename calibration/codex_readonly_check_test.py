"""Readonly diagnostics keep preparation proof and real Codex tool delivery."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from calibration import codex_readonly_check
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.kb_access import prepare_view
from orchestrator.kb_layout import study_identity
from orchestrator.kb_study import KbStudy
from orchestrator.modes import game_tools


class ReadonlyCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('shared/data.md', 'shared/tools.md', 'speedrun/README.md', 'explore/README.md'):
            path = self.root/'kb'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture knowledge\nsecond required line\n', encoding='utf-8')
        for target, value in (
            ('orchestrator.config.REPO_ROOT', self.root),
            ('orchestrator.kb_access.KB_ROOT', self.root/'kb'),
            ('calibration.codex_readonly_check.REPO_ROOT', self.root),
            ('orchestrator.personal_prompts.STORE', self.root/'personal.json'),
            ('orchestrator.prompting.OVERRIDES', self.root/'overrides'),
        ):
            self.enterContext(patch(target, value))

    def preparation(self, knowledge):
        cfg = RunConfig(agent='gpt-native', save='test', model='', engine='codex',
                        provider='codex', knowledge_profile=knowledge,
                        mode='explore' if knowledge == 'explore' else 'speedrun', sim=False)
        prepare_view(cfg)
        study = KbStudy(cfg.kb_dir, 60)
        manifest = {
            'engine': 'codex', 'provider': 'codex', 'runtime': 'container',
            'knowledge_profile': knowledge, 'success': True, 'game_tools_registered': True,
            'session_id': 'prepared-readonly-fixture', 'metrics': {'agent': 'gpt-native', 'tool_calls': 7},
            'kb_identity': study_identity(study.kb_dir),
            'kb_hashes': {name: hashlib.sha256((study.kb_dir/name).read_bytes()).hexdigest()
                          for name in study.files},
            'kb_study': {'complete': True, 'files': {name: [count, count] for name, count in study.total.items()}},
        }
        directory = self.root/'prepared'/knowledge
        directory.mkdir(parents=True)
        (directory/'prepare_summary.json').write_text(json.dumps(manifest), encoding='utf-8')
        return directory, manifest

    async def test_prepared_readonly_diagnostic_dispatches_only_observation_and_narration(self):
        # A valid PNG reaches the actual Codex image input adapter; no real model sees it here.
        picture = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        for knowledge in ('full', 'explore'):
            with self.subTest(knowledge=knowledge):
                prepared, _ = self.preparation(knowledge)
                bridges, runners, turns, inputs, responses = [], [], [], [], []
                external_tasks = []

                def bridge(**options):
                    client = SimpleNamespace(close=Mock())
                    client.call = Mock(return_value={'obs': {'scene': 'AreaComplete', 'frame': 312},
                                                     'image': base64.b64encode(picture).decode()})
                    bridges.append(client)
                    return client

                def factory(*args, **kwargs):
                    runner = CodexRunner(*args, **kwargs)
                    runners.append(runner)
                    self.assertTrue(runner.resume)
                    self.assertEqual(runner.session_id, 'prepared-readonly-fixture')
                    self.assertTrue(runner.study.ready())
                    self.assertEqual(runner.study.extra, {})
                    self.assertFalse(runner.cfg.sim or runner.cfg.sim_assist or runner.cfg.pause_limit)
                    self.assertEqual(runner.cfg.mode, 'explore' if knowledge == 'explore' else 'speedrun')

                    async def act_as_model():
                        registered = {tool.name for tool in await runner.game_server.list_tools()}
                        self.assertEqual(registered, {'observe', 'narrate'})
                        for tool, arguments in [('celeste_narrate', {'thought': '只读核验结算画面'}),
                                                ('celeste_observe', {})]:
                            response = await runner._request('item/tool/call', {
                                'tool': tool, 'arguments': arguments, 'callId': tool})
                            responses.append(response)
                            self.assertTrue(response['success'], response)
                        runner._event('turn/completed', {'turn': {'id': runner.turn_id, 'status': 'completed'}})

                    async def rpc(method, params, **options):
                        if method == 'thread/resume':
                            return {'thread': {'id': params['threadId'], 'path': '/fixture/rollout.jsonl'},
                                    'model': 'fixture', 'activePermissionProfile': {'id': params['permissions']}}
                        if method == 'turn/start':
                            turns.append(params)
                            external_tasks.append(asyncio.create_task(act_as_model()))
                            return {'turn': {'id': 'readonly-fixture-turn'}}
                        if method == 'turn/steer':
                            inputs.extend(params['input'])
                            return {'turnId': params['expectedTurnId']}
                        return {}

                    runner.rpc.start, runner.rpc.close = AsyncMock(), AsyncMock()
                    runner.rpc.request = AsyncMock(side_effect=rpc)
                    runner.rpc.reader = asyncio.get_running_loop().create_future()
                    return runner

                def metadata(*args):
                    return {'dynamic_tools': [{'name': 'celeste_'+name} for name in game_tools(runners[-1].cfg)]
                            + [{'name': name} for name in ('read', 'glob', 'grep')]}
                with patch.object(codex_readonly_check, 'datetime', SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda _: 'fixture-'+knowledge))), \
                     patch.object(codex_readonly_check, 'ensure_container', return_value='fixture-isolated'), \
                     patch.object(codex_readonly_check, 'BridgeClient', side_effect=bridge), \
                     patch.object(codex_readonly_check, 'CodexRunner', side_effect=factory), \
                     patch('orchestrator.codex_container.read_metadata', side_effect=metadata), \
                     patch('orchestrator.model_catalog.load_models', return_value={'models': []}):
                    await asyncio.wait_for(codex_readonly_check.check(SimpleNamespace(prepared=str(prepared))), 5)
                    await asyncio.gather(*external_tasks)
                self.assertEqual(len(turns), 1)
                self.assertEqual(runners[0].called_tools, ['celeste_narrate', 'celeste_observe'])
                self.assertTrue(runners[0]._events.closed)
                bridges[0].call.assert_called_once_with('observe', image=True, entities=False, tiles=False)
                bridges[0].close.assert_called_once()
                self.assertTrue(any('AreaComplete' in block.get('text', '') for block in responses[1]['contentItems']))
                self.assertEqual([item['url'] for item in inputs if item['type'] == 'image'],
                                 ['data:image/png;base64,'+base64.b64encode(picture).decode()])

    async def test_invalid_preparation_fails_before_container_or_bridge(self):
        prepared, original = self.preparation('full')
        for corruption in ('identity', 'hash', 'coverage', 'session', 'workspace'):
            with self.subTest(corruption=corruption):
                manifest = json.loads(json.dumps(original))
                if corruption == 'identity':
                    manifest['kb_identity'] = {'outdated': True}
                elif corruption == 'hash':
                    manifest['kb_hashes'][next(iter(manifest['kb_hashes']))] = 'incorrect'
                elif corruption == 'coverage':
                    manifest['kb_study']['files'][next(iter(manifest['kb_study']['files']))][0] = 0
                elif corruption == 'session':
                    manifest['session_id'] = None
                else:
                    manifest['metrics']['agent'] = 'another-workspace'
                (prepared/'prepare_summary.json').write_text(json.dumps(manifest), encoding='utf-8')
                with patch.object(codex_readonly_check, 'ensure_container') as container, \
                     patch.object(codex_readonly_check, 'BridgeClient') as bridge:
                    with self.assertRaises(ValueError):
                        await codex_readonly_check.check(SimpleNamespace(prepared=str(prepared)))
                    container.assert_not_called()
                    bridge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
