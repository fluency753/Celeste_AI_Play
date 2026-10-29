"""Chat activity is measured independently of the closed game gate."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics


class ChatActivityTests(unittest.TestCase):
    def test_both_adapters_count_chat_work_but_not_idle_or_quota_waits(self):
        for adapter in (CodexRunner, ClaudeRunner):
            with self.subTest(adapter=adapter.__name__), tempfile.TemporaryDirectory() as tmp, \
                    patch('time.monotonic') as clock:
                clock.return_value = 100
                root = Path(tmp)
                metrics = Metrics(root / 'metrics.json', 'test', 'model', 'medium', 600)
                cfg = RunConfig(agent='test', save='test', model='model', runtime='host')
                runner = adapter(cfg, root, SimpleNamespace(done=False), metrics, 'unused', chat_only=True)
                try:
                    metrics.pause()
                    metrics.update(status='chat_idle')
                    clock.return_value = 200
                    runner._begin_model_turn(runtime='test')
                    self.assertEqual(metrics.state.status, 'thinking')
                    self.assertTrue(runner.paused)
                    self.assertIsNotNone(runner._game_guard())
                    clock.return_value = 210
                    metrics.tool_begin('read', label='Read')
                    self.assertEqual(metrics.state.status, 'tool')
                    clock.return_value = 215
                    metrics.tool_end('read')
                    self.assertEqual(metrics.state.status, 'thinking')
                    clock.return_value = 220
                    runner._end_model_turn()
                    self.assertEqual(metrics.state.status, 'chat_idle')
                    self.assertEqual(metrics.state.thinking_s, 15)
                    self.assertEqual(metrics.state.tool_s, 5)
                    clock.return_value = 500
                    runner._begin_model_turn(runtime='test')
                    clock.return_value = 503
                    runner._end_model_turn()
                    self.assertEqual(metrics.state.thinking_s, 18)
                    self.assertEqual(metrics.state.quota_waits, 0)
                    self.assertEqual(metrics.state.waited_s, 0)
                    runner._end_model_turn()  # cleanup is idempotent
                    self.assertEqual(metrics.state.thinking_s, 18)
                finally:
                    runner.close_events()


class CodexChatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_completion_restores_chat_activity_and_tool_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics = Metrics(root / 'metrics.json', 'test', 'model', 'medium', 600)
            cfg = RunConfig(agent='test', save='test', model='model', runtime='host')
            runner = CodexRunner(cfg, root, None, metrics, 'unused', chat_only=True)
            for pending_tool in (False, True):
                runner._begin_model_turn(runtime='test')
                if pending_tool:
                    metrics.tool_begin('read', label='Read')
                item = {'type': 'contextCompaction', 'id': str(pending_tool)}
                runner._event('item/started', {'item': item})
                self.assertEqual(metrics.state.status, 'compacting')
                runner._event('item/completed', {'item': item})
                self.assertEqual(metrics.state.status, 'tool' if pending_tool else 'thinking')
                if pending_tool:
                    metrics.tool_end('read')
                    self.assertEqual(metrics.state.status, 'thinking')
                runner._end_model_turn()

    async def test_claude_chat_send_and_result_share_the_activity_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics = Metrics(root / 'metrics.json', 'test', 'model', 'medium', 600)
            cfg = RunConfig(agent='test', save='test', model='model', runtime='host')
            runner = ClaudeRunner(cfg, root, SimpleNamespace(done=False), metrics, 'unused', chat_only=True)
            runner._write = AsyncMock()
            metrics.pause()
            runner.pending_messages = [{'id': runner.control.enqueue('message', '整理笔记'), 'text': '整理笔记'}]
            try:
                await runner._next_user_turn('unused')
                self.assertEqual(metrics.state.status, 'thinking')
                self.assertNotIn('只回复用户', runner._write.call_args.args[0])
                self.assertTrue(runner.paused)
                await asyncio.sleep(.01)
                runner._handle({'type': 'result', 'subtype': 'success', 'usage': {}})
                self.assertEqual(metrics.state.status, 'chat_idle')
                self.assertGreater(metrics.state.thinking_s, 0)
                self.assertIsNotNone(runner._game_guard())
            finally:
                runner.close_events()

    async def test_real_loop_enters_thinking_and_returns_to_idle_without_game_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics = Metrics(root / 'metrics.json', 'test', 'model', 'medium', 600)
            cfg = RunConfig(agent='test', save='test', model='model', runtime='host')
            runner = CodexRunner(cfg, root, None, metrics, 'unused', chat_only=True)
            started = asyncio.Event()
            messages = []

            async def request(method, params):
                if method == 'thread/start':
                    return {'thread': {'id': 'test'}, 'model': 'model', 'sandbox': {'type': 'readOnly'}, 'activePermissionProfile': {'id': params['permissions']}}
                if method == 'turn/start':
                    messages.append(params['input'][0]['text'])
                    started.set()
                    return {'turn': {'id': 'turn'}}
                return {}

            runner.rpc.start = AsyncMock()
            runner.rpc.request = AsyncMock(side_effect=request)
            runner.rpc.close = AsyncMock()
            runner.rpc.reader = asyncio.get_running_loop().create_future()
            runner.control.enqueue('message', '整理一下笔记')
            with patch('orchestrator.model_catalog.load_models', return_value={'models': []}):
                task = asyncio.create_task(runner.run())
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    self.assertEqual(metrics.state.status, 'thinking')
                    self.assertNotIn('只能回复用户', messages[0])
                    self.assertTrue(runner.paused)
                    self.assertIsNotNone(runner._game_guard())
                    await asyncio.sleep(.02)
                    metrics.narrated()
                    self.assertGreater(metrics.state.thinking_s, 0)
                    runner._event('turn/completed', {'turn': {'status': 'completed'}})
                    async with asyncio.timeout(3):
                        while metrics.state.status != 'chat_idle':
                            await asyncio.sleep(.01)
                    before = metrics.state.thinking_s
                    await asyncio.sleep(.02)
                    metrics.narrated()
                    self.assertEqual(metrics.state.thinking_s, before)
                finally:
                    runner.stop_requested = True
                    await asyncio.wait_for(task, 3)
