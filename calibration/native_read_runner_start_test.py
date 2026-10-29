"""Exercise production startup through external AppServer/filesystem boundaries."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.modes import game_tools
# Load repository-relative game dependencies before redirecting test workspace IO.
from orchestrator import server  # noqa: F401


class NativeReadRunnerStartTest(unittest.IsolatedAsyncioTestCase):
    async def check_start(self, resumed):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kb = root / 'kb'
            kb.mkdir()
            (kb / 'README.md').write_text('required\n', encoding='utf-8')
            cfg = RunConfig(agent='native-start-test', save='test', engine='codex', model='test')
            study = KbStudy(kb, 1)
            runner = CodexRunner(cfg, root / 'run', None,
                                Metrics(root / 'metrics.json', cfg.agent, 'test', 'low', 60),
                                'isolated-container', study, prepare_only=True,
                                resume=resumed, session_id='thread' if resumed else None)
            path = '/codex-home/sessions/not-yet-created.jsonl'
            runner.rpc.start = AsyncMock()
            runner.rpc.close = AsyncMock()
            runner.rpc.request = AsyncMock(return_value={
                'thread': {'id': 'thread', 'path': path}, 'model': 'test',
                'activePermissionProfile': {'id': 'celeste-write'}})
            runner.stop_requested = True
            metadata = {'dynamic_tools': [{'name': 'celeste_' + name} for name in game_tools(cfg)]}
            with patch('orchestrator.config.REPO_ROOT', root), \
                    patch('orchestrator.personal_prompts.STORE', root / 'personal.json'), \
                    patch('orchestrator.prompting.OVERRIDES', root / 'overrides'), \
                    patch('orchestrator.codex_container.read_metadata', return_value=metadata), \
                    patch('orchestrator.codex_container.read_rollout', return_value=(1234, b'')) as read:
                await runner.run()
            self.assertEqual(runner._native_rollout_path, path)
            if resumed:
                read.assert_called_once_with('isolated-container', path)
                self.assertEqual(runner._native_rollout_offset, 1234)
            else:
                read.assert_not_called()
                self.assertEqual(runner._native_rollout_offset, 0)
                self.assertTrue(runner.rpc.request.call_args.args[1]['experimentalRawEvents'])

    async def test_fresh_thread_does_not_read_uncreated_rollout_file(self):
        await self.check_start(False)

    async def test_resumed_thread_seeds_rollout_eof_before_new_turn(self):
        await self.check_start(True)


if __name__ == '__main__':
    unittest.main()
