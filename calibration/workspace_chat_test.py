"""Offline workspace hosting keeps the same conversation and no game session."""
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestrator.chat import chat
from orchestrator.metrics import Metrics


class WorkspaceChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_claude_idle_chat_does_not_create_an_unused_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'run_state.json').write_text(json.dumps({'agent':'test','save':'test','model':'opus',
                'effort':'high','engine':'claude','runtime':'host','session_id':'original'}))
            (root/'summary.json').write_text('{"completed":true}')
            Metrics(root/'metrics.json','test','opus','high',600)
            runner = SimpleNamespace(run=AsyncMock())
            @asynccontextmanager
            async def host(cfg, directory, metrics, container, **options):
                self.assertIsNone(container)
                yield runner
            with patch('orchestrator.chat.ensure_container') as container, \
                 patch('orchestrator.chat.host_runner', host), \
                 patch('orchestrator.runtimes.Runtime.token', return_value='stub'):
                await chat(root)
            container.assert_not_called()
            runner.run.assert_awaited_once()

    async def test_host_codex_chat_routes_programs_through_its_container(self):
        from orchestrator.codex_agent import CodexRunner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'run_state.json').write_text(json.dumps({'agent':'test','save':'test','model':'model',
                'effort':'medium','engine':'codex','runtime':'host','session_id':'original'}))
            (root/'summary.json').write_text('{"completed":true}')
            Metrics(root/'metrics.json','test','model','medium',600)
            received = {}
            @asynccontextmanager
            async def host(cfg,directory,metrics,container,**options):
                runner = CodexRunner(cfg,directory,None,metrics,container,**options)
                async def run():
                    received['reply'] = await runner._request('item/tool/call',{'tool':'celeste_bash',
                        'callId':'offline','arguments':{'command':'echo OK','description':'离线验证'}})
                runner.run = run
                try:
                    yield runner
                finally:
                    runner.close_events()
            fake = SimpleNamespace(returncode=0,stdout='OK',stderr='',seconds=.01,timed_out=False)
            with patch('orchestrator.chat.ensure_container',return_value='offline-container'), \
                 patch('orchestrator.chat.host_runner',host), \
                 patch('orchestrator.codex_agent.run_program',return_value=fake) as program:
                await chat(root,allow_programs=True,sim=True)
            self.assertTrue(received['reply']['success'])
            self.assertEqual(program.call_args.args[0],'offline-container')

    async def test_explicit_offline_sim_is_available_for_both_runtimes(self):
        for engine, execution in (('codex','container'), ('codex','host'), ('claude','container')):
            with self.subTest(engine=engine, runtime=execution), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = {'agent': 'test', 'save': 'test', 'model': 'model', 'effort': 'medium',
                         'engine': engine, 'provider': 'codex' if engine == 'codex' else 'anthropic',
                         'session_id': 'original', 'mode': 'speedrun', 'knowledge_profile': 'full',
                         'runtime':execution}
                (root / 'run_state.json').write_text(json.dumps(state))
                (root / 'summary.json').write_text('{"completed":true}')
                Metrics(root / 'metrics.json', 'test', 'model', 'medium', 600)
                runner = SimpleNamespace(run=AsyncMock())
                received = {}

                @asynccontextmanager
                async def host(cfg, directory, metrics, container, **options):
                    received.update(sim=cfg.sim, chapters=cfg.chapters, **options)
                    yield runner

                with patch('orchestrator.chat.ensure_container', return_value='offline') as container, \
                     patch('orchestrator.chat.host_runner', host), \
                     patch('orchestrator.runtimes.Runtime.token', return_value='test'):
                    await chat(root, allow_programs=True, sim=True)
                self.assertTrue(received['sim'])
                self.assertTrue(received['chat_only'])
                self.assertTrue(received['resume'])
                self.assertEqual(received['session_id'], 'original')
                self.assertEqual(received['chapters'], [])
                self.assertTrue(runner.allow_programs)
                container.assert_called_once()
                runner.run.assert_awaited_once()

    async def test_sim_requires_explicit_program_access_and_cannot_leak_into_naming(self):
        with patch('orchestrator.chat.ensure_container') as container:
            with self.assertRaisesRegex(ValueError, 'allow-programs'):
                await chat(Path('unused'), sim=True)
            with self.assertRaisesRegex(ValueError, '命名'):
                await chat(Path('unused'), name_request='name', allow_programs=True, sim=True)
            container.assert_not_called()

    async def test_host_claude_program_access_is_rejected_before_any_external_side_effect(self):
        for engine in ('claude',):
            for sim in (False, True):
                with self.subTest(engine=engine, sim=sim), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    state = {'agent':'test', 'save':'test', 'model':'model', 'effort':'medium',
                             'engine':engine, 'runtime':'host', 'session_id':'original'}
                    (root/'run_state.json').write_text(json.dumps(state))
                    (root/'summary.json').write_text('{"completed":true}')
                    Metrics(root/'metrics.json', 'test', 'model', 'medium', 600)
                    with patch('orchestrator.chat.ensure_container') as container, \
                         patch('orchestrator.chat.host_runner') as host, \
                         patch('orchestrator.runtimes.Runtime.token') as token:
                        with self.assertRaisesRegex(ValueError, 'container.*host'):
                            await chat(root, allow_programs=True, sim=sim)
                    container.assert_not_called()
                    host.assert_not_called()
                    token.assert_not_called()
