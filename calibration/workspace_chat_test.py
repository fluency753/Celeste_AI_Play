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
from orchestrator.codex_native import native_tool_hook


class WorkspaceChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolated = tempfile.TemporaryDirectory()
        self.addCleanup(self.isolated.cleanup)
        root = Path(self.isolated.name)
        (root / 'kb').mkdir()
        for target, value in (('orchestrator.config.REPO_ROOT', root),
                              ('orchestrator.kb_access.KB_ROOT', root / 'kb')):
            context = patch(target, value)
            context.start()
            self.addCleanup(context.stop)

    async def test_host_claude_idle_chat_does_not_create_an_unused_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'run_state.json').write_text(json.dumps({'agent':'test','save':'test','model':'opus',
                'effort':'high','engine':'claude','runtime':'host','session_id':'original'}), encoding='utf-8')
            (root/'summary.json').write_text('{"completed":true}', encoding='utf-8')
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
                'effort':'medium','engine':'codex','runtime':'host','session_id':'original'}), encoding='utf-8')
            (root/'summary.json').write_text('{"completed":true}', encoding='utf-8')
            Metrics(root/'metrics.json','test','model','medium',600)
            received = {}
            @asynccontextmanager
            async def host(cfg,directory,metrics,container,**options):
                runner = CodexRunner(cfg,directory,None,metrics,container,**options)
                async def run():
                    import subprocess
                    import sys
                    runner.session_id, runner.turn_id, runner.turn_active = 'original', 'offline-turn', True
                    metrics.update(tool_calls=0)
                    received['container'] = runner.rpc.container
                    data = {'token':runner.native_token, 'session_id':runner.session_id, 'turn_id':runner.turn_id,
                            'tool_use_id':'offline', 'tool_name':'Bash', 'hook_event_name':'PreToolUse',
                            'tool_input':{'command':'echo OK', 'description':'离线验证'}}
                    received['reply'] = await native_tool_hook(runner, data)
                    if received['reply']['decision'] == 'allow':
                        # Replace only the external native process with a real safe program.
                        output = subprocess.run([sys.executable, '-c', 'print("OK")'], cwd=directory,
                                                capture_output=True, text=True, check=True)
                        received['stdout'] = output.stdout
                        await native_tool_hook(runner, {**data, 'hook_event_name':'PostToolUse'})
                    self.assertFalse(metrics.tools_running)
                runner.run = run
                try:
                    yield runner
                finally:
                    runner.close_events()
            with patch('orchestrator.chat.ensure_container',return_value='offline-container'), \
                 patch('orchestrator.chat.host_runner',host):
                await chat(root)
            self.assertEqual(received['reply']['decision'], 'allow')
            self.assertEqual(received['container'], 'offline-container')
            self.assertEqual(received['stdout'], 'OK\n')

    async def test_chat_cannot_enable_simulator_for_any_executor(self):
        for engine, runtime in (('codex', 'container'), ('codex', 'host'), ('claude', 'container'), ('claude', 'host')):
            with self.subTest(engine=engine, runtime=runtime), \
                 patch('orchestrator.chat.ensure_container') as container, \
                 patch('orchestrator.chat.host_runner') as host:
                with self.assertRaisesRegex(ValueError, '试玩中'):
                    await chat(Path('unused'), sim=True)
                container.assert_not_called()
                host.assert_not_called()

    async def test_sim_resource_mount_cannot_leak_into_naming(self):
        with patch('orchestrator.chat.ensure_container') as container:
            with self.assertRaisesRegex(ValueError, '命名'):
                await chat(Path('unused'), name_request='name', allow_programs=True, sim=True)
            container.assert_not_called()

    async def test_host_claude_sim_mount_is_rejected_before_any_external_side_effect(self):
        for engine in ('claude',):
            for sim in (True,):
                with self.subTest(engine=engine, sim=sim), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    state = {'agent':'test', 'save':'test', 'model':'model', 'effort':'medium',
                             'engine':engine, 'runtime':'host', 'session_id':'original'}
                    (root/'run_state.json').write_text(json.dumps(state), encoding='utf-8')
                    (root/'summary.json').write_text('{"completed":true}', encoding='utf-8')
                    Metrics(root/'metrics.json', 'test', 'model', 'medium', 600)
                    with patch('orchestrator.chat.ensure_container') as container, \
                         patch('orchestrator.chat.host_runner') as host, \
                         patch('orchestrator.runtimes.Runtime.token') as token:
                        with self.assertRaisesRegex(ValueError, '试玩中'):
                            await chat(root, allow_programs=True, sim=sim)
                    container.assert_not_called()
                    host.assert_not_called()
                    token.assert_not_called()
