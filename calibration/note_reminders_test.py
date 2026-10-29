"""Note reminders through file state, real runner events, MCP responses and the gate route."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from calibration.collision_contract_test import FakeBridge
from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.note_reminders import NoteReminders
from orchestrator.pausegate import PauseGate
from orchestrator.run import add_gate_route
from orchestrator.codex_native import native_tool_hook
from orchestrator.server import build_server


@contextmanager
def runner_fixture(engine='codex', **options):
    with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
        root = Path(folder)
        run = root / 'run'
        run.mkdir()
        cfg = RunConfig(agent='notes', save='test', model='stub', engine=engine, runtime='container',
                        provider_info={'images': False}, sim=False, chapter=3, cutscene_mode='agent')
        cfg.min_frames_per_act = 1
        cfg.workspace_dir.mkdir(parents=True)
        notes = cfg.workspace_dir / 'notes.md'
        notes.write_bytes(b'original notes')
        metrics = Metrics(run / 'metrics.json', 'notes', 'stub', 'high', 600)
        session = GameSession(cfg, run, metrics)
        session.client = FakeBridge()
        if engine == 'codex':
            runner = CodexRunner(cfg, run, session, metrics, 'unused', game_server=build_server(session), **options)
            runner.rpc.request = AsyncMock(return_value={})
        else:
            runner = ClaudeRunner(cfg, run, session, metrics, '', **options)
            runner._write = AsyncMock()
        try:
            yield runner, session, notes
        finally:
            runner.close_events()
            session.close(False)


def codex_usage(runner, used, window=1000):
    runner._event('thread/tokenUsage/updated', {'tokenUsage': {
        'last': {'inputTokens': used, 'outputTokens': 0}, 'modelContextWindow': window,
        'total': {'outputTokens': 0}}})


def codex_compaction(runner, ident='one'):
    runner._event('item/completed', {'item': {'type': 'contextCompaction', 'id': ident}})


def claude_usage(runner, used):
    runner._handle({'type': 'assistant', 'message': {'id': 'reply', 'content': [],
                    'usage': {'input_tokens': used}}})


def claude_compaction(runner, ident='one'):
    runner._handle({'type': 'system', 'subtype': 'compact_boundary', 'uuid': ident,
                    'compact_metadata': {'trigger': 'auto', 'pre_tokens': 100, 'post_tokens': 10}})


def response_text(response):
    return '\n'.join(item.get('text', '') for item in response['contentItems'])


class NoteReminderStateTests(unittest.TestCase):
    def test_default_threshold_is_sixty_and_boundary_is_strict(self):
        self.assertEqual(RunConfig(agent='test', save='test', model='stub').notes_warn_percent, 60)
        with tempfile.TemporaryDirectory() as folder:
            notes = NoteReminders(Path(folder), lambda: 60)
            notes.usage(600, 1000)
            self.assertEqual(notes.peek(), ())
            notes.usage(601, 1000)
            self.assertEqual([key[0] for key, _ in notes.peek()], ['before'])
            self.assertIn('60%', notes.take())
            notes.usage(950, 1000)
            self.assertEqual(notes.peek(), ())

    def test_threshold_changes_cancel_pending_pre_and_allow_later_fresh_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            threshold = [60]
            notes = NoteReminders(Path(folder), lambda: threshold[0])
            notes.usage(700, 1000)
            self.assertTrue(notes.peek())
            threshold[0] = 80
            self.assertEqual(notes.peek(), ())
            notes.usage(800, 1000)
            self.assertEqual(notes.peek(), ())
            notes.usage(801, 1000)
            self.assertTrue(notes.peek())
            threshold[0] = 0
            self.assertEqual(notes.peek(), ())
            notes.usage(999, 1000)
            self.assertEqual(notes.peek(), ())
            notes.compacted('compact')
            self.assertEqual([key[0] for key, _ in notes.peek()], ['after'])

    def test_observed_content_change_is_latched_even_if_original_text_returns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'notes.md'
            path.write_bytes(b'original')
            notes = NoteReminders(Path(folder), lambda: 60)
            notes.usage(700, 1000)
            path.write_bytes(b'updated')
            self.assertEqual(notes.peek(), ())
            self.assertTrue(notes.updated)
            path.write_bytes(b'original')
            notes.observe_file()
            notes.usage(950, 1000)
            self.assertEqual(notes.peek(), ())

    def test_compaction_replaces_pre_without_reusing_old_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            notes = NoteReminders(Path(folder), lambda: 60)
            notes.usage(900, 1000)
            notes.compacted('one')
            self.assertEqual([key[0] for key, _ in notes.peek()], ['after'])
            notes.take()
            notes.compacted('one')
            self.assertEqual(notes.peek(), ())
            self.assertIsNone(notes.percent)
            self.assertEqual(notes.peek(), ())
            notes.usage(601, 1000)
            self.assertEqual([key[0] for key, _ in notes.peek()], ['before'])

    def test_unknown_and_oversized_file_do_not_infer_no_update(self):
        with tempfile.TemporaryDirectory() as folder:
            notes = NoteReminders(Path(folder), lambda: 60)
            path = Path(folder) / 'notes.md'
            path.write_bytes(b'x' * (256 * 1024 + 1))
            notes.usage(900, 1000)
            self.assertEqual(notes.peek(), ())
            self.assertFalse(notes.observable)
            with patch.object(notes, '_fingerprint', return_value=None):
                notes.usage(900, 1000)
                self.assertEqual(notes.peek(), ())

    def test_stale_ack_does_not_discard_new_compaction_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            notes = NoteReminders(Path(folder), lambda: 60)
            notes.compacted('one')
            offered = notes.peek()
            notes.compacted('two')
            notes.acknowledge(offered)
            self.assertEqual([key[0] for key, _ in notes.peek()], ['after'])
            self.assertNotEqual(notes.peek()[0][0], offered[0][0])

    def test_unknown_numeric_usage_cancels_stale_pre_until_fresh_valid_usage(self):
        for used, window in ((700, None), (None, 1000), (700, 0), (True, 1000), (float('nan'), 1000)):
            with self.subTest(used=used, window=window), tempfile.TemporaryDirectory() as folder:
                notes = NoteReminders(Path(folder), lambda: 60)
                notes.usage(700, 1000)
                self.assertTrue(notes.peek())
                notes.usage(used, window)
                self.assertEqual(notes.peek(), ())
                notes.usage(700, 1000)
                self.assertTrue(notes.peek())

    def test_no_workspace_has_no_reminders(self):
        notes = NoteReminders(None, lambda: 60)
        notes.usage(900, 1000)
        notes.compacted('one')
        self.assertEqual(notes.take(), '')


class NoteReminderRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_fresh_usage_and_compaction_reach_real_game_response_once(self):
        with runner_fixture() as (runner, session, notes):
            runner.session_id, runner.turn_id, runner.turn_active = 'session', 'turn', True
            codex_usage(runner, 700)
            self.assertEqual([key[0] for key, _ in runner.note_reminders.peek()], ['before'])
            codex_compaction(runner)
            codex_compaction(runner)
            self.assertEqual([key[0] for key, _ in runner.note_reminders.peek()], ['after'])
            self.assertEqual(runner.turns, 0)
            self.assertEqual(runner.pending_messages, [])
            runner.rpc.request.assert_not_awaited()
            response = await runner._request('item/tool/call', {'callId': 'observe-one',
                'tool': 'celeste_observe', 'arguments': {}})
            self.assertTrue(response['success'])
            self.assertIn('上下文刚完成压缩', response_text(response))
            self.assertNotIn('本监测周期尚未', response_text(response))
            response = await runner._request('item/tool/call', {'callId': 'observe-two',
                'tool': 'celeste_observe', 'arguments': {}})
            self.assertNotIn('系统·笔记提醒', response_text(response))
            self.assertEqual(runner.turns, 0)
            runner.rpc.request.assert_not_awaited()

    async def test_codex_natural_user_turn_includes_pending_note_without_creating_another_turn(self):
        with runner_fixture(chat_only=True) as (runner, session, notes):
            codex_compaction(runner, 'before-chat')
            started = asyncio.Event()
            messages = []
            async def rpc_request(method, params):
                if method == 'thread/start':
                    return {'thread': {'id': 'test'}, 'model': 'stub', 'sandbox': {'type': 'readOnly'}, 'activePermissionProfile': {'id': params['permissions']}}
                if method == 'turn/start':
                    messages.append(params['input'][0]['text'])
                    started.set()
                    return {'turn': {'id': 'natural-turn'}}
                return {}
            runner.rpc.start = AsyncMock()
            runner.rpc.close = AsyncMock()
            runner.rpc.request = AsyncMock(side_effect=rpc_request)
            runner.rpc.reader = asyncio.get_running_loop().create_future()
            runner.control.enqueue('message', '用户主动继续聊天')
            with patch('orchestrator.model_catalog.load_models', return_value={'models': []}):
                task = asyncio.create_task(runner.run())
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    self.assertIn('用户主动继续聊天', messages[0])
                    self.assertIn('上下文刚完成压缩', messages[0])
                    self.assertTrue(runner.paused)
                    self.assertEqual(runner.turns, 1)
                    runner._event('turn/completed', {'turn': {'status': 'completed'}})
                    async with asyncio.timeout(3):
                        while runner.metrics.state.status != 'chat_idle':
                            await asyncio.sleep(.01)
                    self.assertEqual(runner.turns, 1)
                    self.assertEqual(runner.note_reminders.peek(), ())
                    self.assertEqual([call.args[0] for call in runner.rpc.request.call_args_list].count('turn/start'), 1)
                    self.assertNotIn('turn/interrupt', [call.args[0] for call in runner.rpc.request.call_args_list])
                finally:
                    runner.stop_requested = True
                    await asyncio.wait_for(task, 3)

    async def test_codex_bash_mentions_do_not_count_as_update_but_actual_write_cancels_pre(self):
        with runner_fixture() as (runner, session, notes):
            import subprocess
            import sys
            runner.session_id, runner.turn_id, runner.turn_active = 'notes-thread', 'notes-turn', True
            runner.metrics.update(tool_calls=0)
            async def program(ident, command, source):
                data = {'token':runner.native_token, 'session_id':runner.session_id, 'turn_id':runner.turn_id,
                        'tool_use_id':ident, 'tool_name':'Bash', 'hook_event_name':'PreToolUse',
                        'tool_input':{'command':command}}
                before = await native_tool_hook(runner, data)
                self.assertEqual(before['decision'], 'allow')
                # Only the external native process is replaced; execute a real isolated program.
                subprocess.run([sys.executable, '-c', source], cwd=session.workspace,
                               capture_output=True, check=True)
                return await native_tool_hook(runner, {**data, 'hook_event_name':'PostToolUse'})
            await program('mention', 'echo /workspace/notes.md', "print('/workspace/notes.md')")
            self.assertFalse(runner.note_reminders.updated)
            codex_usage(runner, 700)
            response = await program('changed', 'python3 update_notes.py',
                         f'from pathlib import Path;Path({str(notes)!r}).write_bytes(b"new content")')
            self.assertEqual(response['decision'], 'allow')
            self.assertTrue(runner.note_reminders.updated)
            self.assertNotIn('系统·笔记提醒', response['context'])
            notes.write_bytes(b'original notes')
            codex_usage(runner, 900)
            self.assertEqual(runner.note_reminders.peek(), ())
            runner.rpc.request.assert_not_awaited()

    async def test_codex_paused_or_chat_only_events_do_not_wake_or_interrupt(self):
        for options in ({'start_paused': True}, {'chat_only': True}):
            with self.subTest(options=options), runner_fixture(**options) as (runner, session, notes):
                codex_usage(runner, 700)
                codex_compaction(runner)
                self.assertTrue(runner.paused)
                self.assertEqual(runner.turns, 0)
                self.assertEqual(runner.pending_messages, [])
                runner.rpc.request.assert_not_awaited()

    async def test_naming_and_actual_host_claude_without_workspace_access_do_not_remind(self):
        with runner_fixture(naming_only=True) as (runner, session, notes):
            codex_usage(runner, 700)
            codex_compaction(runner)
            self.assertEqual(runner.note_reminders.take(), '')
        with runner_fixture('claude', naming_only=True) as (runner, session, notes):
            runner.metrics.update(context_window=1000)
            claude_usage(runner, 700)
            claude_compaction(runner)
            self.assertEqual(runner.note_reminders.take(), '')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent='host', save='test', model='stub', engine='claude', runtime='host')
            metrics = Metrics(root / 'metrics.json', 'host', 'stub', 'high', 600)
            metrics.update(context_window=1000)
            runner = ClaudeRunner(cfg, root, None, metrics, '')
            try:
                claude_usage(runner, 700)
                claude_compaction(runner)
                self.assertEqual(runner.note_reminders.take(), '')
            finally:
                runner.close_events()

    async def test_claude_usage_compaction_and_natural_send_append_without_extra_turn(self):
        with runner_fixture('claude') as (runner, session, notes):
            runner.cfg.autocompact = '1000'
            claude_usage(runner, 600)
            self.assertEqual(runner.note_reminders.peek(), ())
            claude_usage(runner, 601)
            self.assertEqual([key[0] for key, _ in runner.note_reminders.peek()], ['before'])
            claude_compaction(runner)
            self.assertEqual([key[0] for key, _ in runner.note_reminders.peek()], ['after'])
            self.assertEqual(runner.turns, 0)
            runner._write.assert_not_awaited()
            await runner._send('用户的自然消息')
            text = runner._write.call_args.args[0]
            self.assertIn('用户的自然消息', text)
            self.assertIn('上下文刚完成压缩', text)
            self.assertNotIn('本监测周期尚未', text)
            self.assertEqual(runner.turns, 1)
            self.assertEqual(runner.note_reminders.peek(), ())
            self.assertEqual(runner.pending_messages, [])

    async def test_failed_claude_send_keeps_pending_note_for_later_delivery(self):
        with runner_fixture('claude') as (runner, session, notes):
            claude_compaction(runner)
            offered = runner.note_reminders.peek()
            runner._write.side_effect = OSError('pipe closed')
            with self.assertRaisesRegex(OSError, 'pipe closed'):
                await runner._send('用户消息')
            self.assertEqual(runner.note_reminders.peek(), offered)

    async def test_real_gate_route_delivers_only_after_allowed_request_and_file_change_is_detected(self):
        with runner_fixture('claude') as (runner, session, notes):
            runner.metrics.update(context_window=1000)
            claude_usage(runner, 700)
            app = SimpleNamespace(router=SimpleNamespace(routes=[]))
            gate = PauseGate(runner.metrics, enabled=False)
            add_gate_route(app, gate, session=session)
            endpoint = app.router.routes[0].endpoint
            runner.paused = True
            denied = await endpoint(SimpleNamespace(json=AsyncMock(return_value={'tool_name': 'Bash'})))
            self.assertEqual(json.loads(denied.body)['decision'], 'deny')
            self.assertTrue(runner.note_reminders.peek())
            allowed = await endpoint(SimpleNamespace(json=AsyncMock(return_value={'tool_name': 'Read'})))
            body = json.loads(allowed.body)
            self.assertEqual(body['decision'], 'allow')
            self.assertIn('系统·笔记提醒', body['context'])
            self.assertEqual(runner.note_reminders.peek(), ())
            self.assertTrue(runner.paused)
            self.assertEqual(runner.turns, 0)
            runner._write.assert_not_awaited()
            claude_compaction(runner, 'new')
            # A normal Bash result event observes a real content change, not its command text.
            notes.write_bytes(b'updated through external bash')
            runner._handle({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'bash-one', 'content': 'done'}]}})
            self.assertTrue(runner.note_reminders.updated)
            runner.note_reminders.take()
            claude_usage(runner, 950)
            self.assertEqual(runner.note_reminders.peek(), ())


if __name__ == '__main__':
    unittest.main()
