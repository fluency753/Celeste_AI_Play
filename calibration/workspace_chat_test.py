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
    async def test_explicit_offline_sim_is_available_for_both_runtimes(self):
        for engine in ('codex', 'claude'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = {'agent': 'test', 'save': 'test', 'model': 'model', 'effort': 'medium',
                         'engine': engine, 'provider': 'codex' if engine == 'codex' else 'anthropic',
                         'session_id': 'original', 'mode': 'speedrun', 'knowledge_profile': 'full'}
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

    async def test_host_program_access_is_rejected_before_any_external_side_effect(self):
        for engine in ('codex', 'claude'):
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
