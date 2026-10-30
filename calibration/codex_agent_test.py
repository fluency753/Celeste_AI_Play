"""原生 GPT 执行器的契约测试，不连接游戏或调用模型。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.types import CallToolResult
from orchestrator.codex_agent import CodexRunner, public_event
from orchestrator.prepared import load_preparation
from orchestrator.config import RunConfig
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.codex_native import native_tool_hook
from orchestrator.modes import game_tools


def response_text(result):
    return '\n'.join(part.get('text', '') for part in result['contentItems'])


class NativeAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kb = self.root / "kb"
        self.kb.mkdir()
        (self.kb / "README.md").write_text("one\ntwo\n", encoding="utf-8")
        self.cfg = RunConfig(agent="native-test", save="test", model="test-model", engine="codex", live=True)
        self.study = KbStudy(self.kb, 10)
        self.metrics = Metrics(self.root / "metrics.json", "native-test", "test-model", "medium", 600, study=True)
        self.server = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult.model_validate({
            "content": [{"type": "text", "text": "observed"},
                        {"type": "image", "mimeType": "image/png", "data": "AAAA"}]})))
        self.runner = CodexRunner(self.cfg, self.root, None, self.metrics, "container", self.study,
                                  game_server=self.server)
        self.runner.session_id, self.runner.turn_id, self.runner.turn_active = 'session', 'turn', True
        self.runner._event('turn/started', {'turn': {'id': 'turn'}})
        self.runner.session = SimpleNamespace(done=False)
        self.runner.rpc.request = AsyncMock(return_value={'turnId': 'turn'})
        self.metrics.update(tool_calls=0)
        # Docker is the external filesystem boundary; keep metadata consumers real.
        def metadata_process(container, *command, **kwargs):
            self.assertEqual(command[:2], ('python3', '-c'))
            payload = json.loads(Path(command[-1]).read_text(encoding='utf-8'))['payload']
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        metadata = patch('orchestrator.codex_container._run', side_effect=metadata_process)
        metadata.start()
        self.addCleanup(metadata.stop)
        # Model discovery is an external account-container boundary too, including runner.run().
        catalog = patch('orchestrator.model_catalog.load_models', return_value={'models': []})
        self.models = catalog.start()
        self.addCleanup(catalog.stop)
        account = patch('orchestrator.model_catalog.ensure_account_container',
                        side_effect=AssertionError('Unit tests must not create an account container'))
        account.start()
        self.addCleanup(account.stop)

    def tearDown(self):
        self.temp.cleanup()

    def file_result(self, offset=1, *, path='/kb/README.md', text=None, images=None):
        return {'kind': 'text', 'path': path,
                'text': ('one' if offset == 1 else 'two') if text is None else text,
                'file': {'filePath': path, 'startLine': offset, 'numLines': 1, 'totalLines': 2},
                'images': images or []}

    async def read(self, offset, *, record=True):
        with patch('orchestrator.codex_agent.read_file', return_value=self.file_result(offset)) as files:
            result = await self.runner._request('item/tool/call', {'callId': f'read-{offset}',
                'tool': 'read', 'arguments': {'file_path': '/kb/README.md', 'offset': offset, 'limit': 1}})
        files.assert_called_once()
        self.assertEqual(files.call_args.args[0], 'container')
        self.assertEqual(files.call_args.args[1], {'file_path': '/kb/README.md', 'offset': offset, 'limit': 1,
            '_tool_names': {'read': 'read', 'glob': 'glob', 'grep': 'grep'}})
        self.assertTrue(result['success'])
        if record:
            self.record_steer()
        return result

    def steer_params(self):
        return next(call.args[1] for call in reversed(self.runner.rpc.request.call_args_list)
                    if call.args[0] == 'turn/steer')

    def record_steer(self, params=None):
        """Codex drains the steered input into history before the next model request (pinned 0.165 probe)."""
        params = params or self.steer_params()
        self.runner._event('item/completed', {'threadId': 'session', 'turnId': 'turn', 'item': {
            'type': 'userMessage', 'id': 'user-' + params['clientUserMessageId'],
            'clientId': params['clientUserMessageId'],
            'content': [{'type': 'text', 'text': part['text']} if part['type'] == 'text' else part
                        for part in params['input']]}})

    def native_request(self, ident, **arguments):
        return {'token': self.runner.native_token, 'session_id': self.runner.session_id,
                'turn_id': self.runner.turn_id, 'tool_use_id': ident, 'tool_name': 'Bash',
                'hook_event_name': 'PreToolUse', 'tool_input': {'command': 'python offline.py', **arguments}}

    async def test_interrupt_cancels_codex_turn_and_waits_for_active_action(self):
        runner = self.runner
        runner.session = SimpleNamespace(done=False)
        runner.session_id, runner.turn_id, runner.turn_active = 'session', 'turn', True
        runner.rpc.request = AsyncMock(return_value={})
        runner._check_goal = AsyncMock()
        await runner.action_lock.acquire()
        ident = runner.control.enqueue('interrupt')
        task = asyncio.create_task(runner._controls())
        try:
            for _ in range(30):
                if runner.interrupted_for_resume:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(runner.interrupted_for_resume)
            self.assertTrue(runner.stop_requested)
            runner.rpc.request.assert_awaited_once_with('turn/interrupt', {'threadId': 'session', 'turnId': 'turn'})
            self.assertIn('中断', runner._game_guard())
            row = lambda: next(a for a in runner.control.snapshot()['actions'] if a['id'] == ident)
            self.assertNotEqual(row()['status'], 'submitted')
            stopping = asyncio.create_task(runner._stopped_reason())
            await asyncio.sleep(.01)
            self.assertFalse(stopping.done())
            runner.action_lock.release()
            for _ in range(30):
                if row()['status'] == 'submitted':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(row()['status'], 'submitted')
            self.assertIn('用户中断', await asyncio.wait_for(stopping, 1))
            self.assertTrue(runner._effort_status()['interrupt_control'])
        finally:
            if runner.action_lock.locked():
                runner.action_lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_chat_detach_keeps_its_existing_stop_reason(self):
        self.assertEqual(await self.runner._stopped_reason(), '聊天会话切换')

    async def test_read_audit_gates_game_then_forwards_text_and_image(self):
        await self.read(1)
        self.assertEqual(self.study.read["README.md"], {1})
        denied = await self.runner._request("item/tool/call", {"tool": "celeste_observe", "arguments": {}})
        self.assertFalse(denied["success"])
        self.server.call_tool.assert_not_called()
        await self.read(2)
        self.assertTrue(self.study.complete())
        allowed = await self.runner._request("item/tool/call", {"tool": "celeste_observe", "arguments": {}})
        self.assertTrue(allowed["success"])
        self.assertEqual(allowed["contentItems"][0]["text"], "observed")
        self.assertNotIn('data:image/', json.dumps(allowed))
        attachment = self.runner.rpc.request.call_args.args[1]
        self.assertEqual(attachment['input'][1], {'type': 'image', 'url': 'data:image/png;base64,AAAA'})
        self.assertEqual(attachment['expectedTurnId'], 'turn')

    async def test_kb_read_counts_only_after_codex_records_steered_input(self):
        entered, release = asyncio.Event(), asyncio.Event()
        steered = []
        async def steer(method, params, **kwargs):
            self.assertEqual(method, 'turn/steer')
            self.assertIn('one', json.dumps(params, ensure_ascii=False))
            self.assertEqual(self.study.read['README.md'], set())
            steered.append(params)
            entered.set()
            await release.wait()
            return {'turnId': 'turn'}
        self.runner.rpc.request = steer
        with patch('orchestrator.codex_agent.read_file', return_value=self.file_result()):
            task = asyncio.create_task(self.runner._request('item/tool/call', {
                'callId': 'hidden-kb', 'tool': 'read', 'arguments': {'file_path': '/kb/README.md'}}))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(self.study.read['README.md'])
            release.set()
            result = await task
        self.assertTrue(result['success'])
        # The steer acknowledgement only queues the input; it can still be dropped.
        self.assertFalse(self.study.read['README.md'])
        self.assertIn('模型实际接收后计入', response_text(result))
        self.record_steer(steered[0])
        self.assertEqual(self.study.read['README.md'], {1})
        self.record_steer(steered[0])
        self.assertEqual(self.study.read['README.md'], {1})
        # Code mode may hide the tool return; required content was independently
        # delivered as current-turn input and counted once Codex recorded it.

    async def test_kb_steer_acknowledged_then_turn_interrupted_is_not_counted(self):
        # Pinned Codex 0.165 clears queued steer input when the turn is interrupted
        # before the next model request; the acknowledgement alone is not delivery.
        await self.read(1, record=False)
        self.assertTrue(self.runner._receipts)
        self.runner._event('turn/completed', {'turn': {'id': 'turn', 'status': 'interrupted'}})
        self.assertFalse(self.study.read['README.md'])
        self.assertEqual(self.runner._receipts, {})
        self.assertEqual(self.metrics.state.kb_progress, 0.0)

    async def test_kb_steer_on_normally_completed_turn_counts_without_item_event(self):
        await self.read(1, record=False)
        self.runner._event('turn/completed', {'turn': {'id': 'turn', 'status': 'completed'}})
        self.assertEqual(self.study.read['README.md'], {1})
        self.assertEqual(self.runner._receipts, {})

    async def test_pausing_turn_does_not_steer_reads(self):
        self.runner.pausing = True
        with patch('orchestrator.codex_agent.read_file', return_value=self.file_result()):
            result = await self.runner._request('item/tool/call', {'callId': 'pausing-kb',
                'tool': 'read', 'arguments': {'file_path': '/kb/README.md'}})
        self.assertFalse(result['success'])
        self.assertIn('交付失败', response_text(result))
        self.runner.rpc.request.assert_not_awaited()
        self.assertEqual(self.runner._receipts, {})

    async def test_kb_delivery_failure_does_not_credit_reading(self):
        self.runner.rpc.request = AsyncMock(side_effect=RuntimeError('attachment delivery failed'))
        with patch('orchestrator.codex_agent.read_file', return_value=self.file_result()):
            result = await self.runner._request('item/tool/call', {'callId': 'failed-kb',
                'tool': 'read', 'arguments': {'file_path': '/kb/README.md'}})
        self.assertFalse(result['success'])
        self.assertFalse(self.study.read['README.md'])

    async def test_kb_attachment_failure_does_not_credit_text_metadata(self):
        self.runner.rpc.request = AsyncMock(side_effect=RuntimeError('image delivery failed'))
        result_file = self.file_result(images=[{'mimeType': 'image/png', 'data': 'AAAA'}])
        with patch('orchestrator.codex_agent.read_file', return_value=result_file):
            result = await self.runner._request('item/tool/call', {'callId': 'failed-kb-image',
                'tool': 'read', 'arguments': {'file_path': '/kb/README.md'}})
        self.assertFalse(result['success'])
        self.assertFalse(self.study.read['README.md'])

    async def test_kb_acknowledgement_for_other_turn_does_not_credit_reading(self):
        self.runner.rpc.request = AsyncMock(return_value={'turnId': 'replacement'})
        with patch('orchestrator.codex_agent.read_file', return_value=self.file_result()):
            result = await self.runner._request('item/tool/call', {'callId': 'wrong-turn-kb',
                'tool': 'read', 'arguments': {'file_path': '/kb/README.md'}})
        self.assertFalse(result['success'])
        self.assertFalse(self.study.read['README.md'])

    async def test_native_shell_kb_output_does_not_count_as_explicit_read(self):
        data = self.native_request('native-cat', command='cat /kb/README.md')
        self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'allow')
        self.runner._event('item/completed', {'item': {'type': 'commandExecution',
            'id': 'native-cat', 'cwd': '/kb', 'exitCode': 0, 'status': 'completed',
            'aggregatedOutput': 'one\ntwo\n'}})
        await native_tool_hook(self.runner, {**data, 'hook_event_name': 'PostToolUse'})
        self.assertFalse(self.study.read['README.md'])

    async def test_oversized_ordinary_text_is_steered_intact_not_left_to_tool_output_truncation(self):
        # Pinned Codex 0.165 keeps only about 12–50k characters of a 96k dynamic-tool
        # text, but keeps steered user input intact; the page must not live in the tool result.
        from orchestrator import codex_read as reader
        rows = ''.join(f'row {number} ' + 'x' * 60 + '\n' for number in range(1, 3001))
        page = reader._text_result('/workspace/notes.md', rows.encode(), 1, 2000)
        self.assertGreater(len(page['text']), 60000)
        self.runner.chat_only = self.runner.paused = True
        with patch('orchestrator.codex_agent.read_file', return_value=page):
            result = await self.runner._request('item/tool/call', {'callId': 'notes',
                'tool': 'read', 'arguments': {'file_path': '/workspace/notes.md'}})
        self.assertTrue(result['success'])
        text = response_text(result)
        self.assertLess(len(text), 2000)
        self.assertNotIn('row 1 x', text)
        # The hint follows the rows actually delivered within the page budget.
        self.assertLess(page['next_offset'], 2001)
        self.assertIn(f"offset={page['next_offset']}", text)
        self.assertEqual(text.count('offset='), 1)
        params = self.steer_params()
        self.assertIn(page['text'], params['input'][0]['text'])
        self.assertTrue(params['input'][0]['text'].rstrip().endswith('x' * 60))
        self.assertFalse(self.study.read['README.md'])
        self.record_steer(params)
        self.assertFalse(self.study.read['README.md'])

    async def test_read_image_is_delivered_as_same_turn_image_in_paused_chat(self):
        self.runner.chat_only = self.runner.paused = True
        image = {'mimeType': 'image/png', 'data': 'AAAA'}
        with patch('orchestrator.codex_agent.read_file', return_value={
                'kind': 'image', 'path': '/workspace/picture.png', 'images': [image], 'text': 'picture'}):
            result = await self.runner._request('item/tool/call', {'callId': 'picture',
                'tool': 'read', 'arguments': {'file_path': '/workspace/picture.png'}})
        self.assertTrue(result['success'])
        self.assertNotIn('AAAA', json.dumps(result))
        params = self.runner.rpc.request.call_args.args[1]
        self.assertEqual(params['expectedTurnId'], 'turn')
        self.assertTrue(any(part.get('type') == 'image' and part.get('url') ==
            'data:image/png;base64,AAAA' for part in params['input']))

    async def test_read_worker_error_is_not_reported_as_success(self):
        with patch('orchestrator.codex_agent.read_file', return_value={
                'kind': 'error', 'text': 'file missing'}):
            result = await self.runner._request('item/tool/call', {'callId': 'missing',
                'tool': 'read', 'arguments': {'file_path': '/workspace/missing'}})
        self.assertFalse(result['success'])
        self.assertIn('file missing', json.dumps(result))
        self.runner.rpc.request.assert_not_awaited()

    async def test_note_reminders_survive_read_error_exception_and_delivery_failure(self):
        cases = [('worker-error', {'return_value': {'kind': 'error', 'error': 'file missing'}}, None),
                 ('worker-exception', {'side_effect': ValueError('worker timeout')}, None),
                 ('steer-failure', {'return_value': self.file_result(path='/workspace/x')},
                  RuntimeError('steer failed'))]
        for label, worker, steer_error in cases:
            with self.subTest(label=label):
                self.runner.note_reminders.pending['after'] = (1, 'NOTE-REMINDER-TEXT')
                self.runner.rpc.request = (AsyncMock(side_effect=steer_error) if steer_error
                                           else AsyncMock(return_value={'turnId': 'turn'}))
                with patch('orchestrator.codex_agent.read_file', **worker):
                    result = await self.runner._request('item/tool/call', {'callId': label,
                        'tool': 'read', 'arguments': {'file_path': '/workspace/x'}})
                self.assertFalse(result['success'])
                self.assertNotIn('NOTE-REMINDER-TEXT', response_text(result))
                self.assertTrue(self.runner.note_reminders.peek())
                if steer_error:
                    self.assertEqual(self.runner._inflight_notes, set())
                    self.runner.rpc.request = AsyncMock(return_value={'turnId': 'turn'})
                    # The failed reminder is retried by another tool, without repeating the read/game action.
                    await self.runner._request('item/tool/call', {'tool': 'unknown', 'arguments': {}})
                params = self.steer_params()
                self.assertIn('NOTE-REMINDER-TEXT', params['input'][0]['text'])
                self.record_steer(params)
                self.assertEqual(self.runner.note_reminders.peek(), ())

    async def test_native_tool_reminders_are_steered_and_confirmed_only_when_recorded(self):
        notes = self.runner.note_reminders
        self.cfg.live = True
        for outcome in ('recorded', 'interrupted'):
            with self.subTest(outcome=outcome):
                self.runner.turn_done.clear()
                self.runner.turn_id, self.runner.turn_active = 'turn', True
                notes.pending['after'] = (1, 'NOTE-REMINDER-TEXT')
                self.runner.rpc.request = AsyncMock(return_value={'turnId': 'turn'})
                with patch.object(self.metrics, 'take_live_reminder', return_value=45.0), \
                        patch.object(self.metrics, 'restore_live_reminder') as restore:
                    data = self.native_request('native-' + outcome)
                    decision = await native_tool_hook(self.runner, data)
                    self.assertEqual(decision['decision'], 'allow')
                    self.assertNotIn('NOTE-REMINDER-TEXT', decision['context'])
                    await asyncio.gather(*self.runner._receipt_tasks)
                    params = self.steer_params()
                    self.assertIn('NOTE-REMINDER-TEXT', params['input'][0]['text'])
                    self.assertIn('直播提醒', params['input'][0]['text'])
                    # Queued but not recorded: still pending, and not repeated by a tool result.
                    self.assertTrue(notes.peek())
                    other = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
                    self.assertNotIn('NOTE-REMINDER-TEXT', response_text(other))
                    if outcome == 'recorded':
                        self.record_steer(params)
                        self.assertEqual(notes.peek(), ())
                        restore.assert_not_called()
                    else:
                        self.runner._event('turn/completed', {'turn': {'id': 'turn', 'status': 'interrupted'}})
                        self.assertTrue(notes.peek())
                        restore.assert_called_once_with(45.0)
                        self.runner.turn_done.clear()
                        await self.runner._request('item/tool/call', {'tool': 'unknown', 'arguments': {}})
                        self.assertIn('NOTE-REMINDER-TEXT', self.steer_params()['input'][0]['text'])
                        self.record_steer()
                await native_tool_hook(self.runner, {**data, 'hook_event_name': 'PostToolUse'})

    async def test_read_tools_carry_live_narration_reminder(self):
        from orchestrator.codex_agent import LIVE_NOTE
        self.cfg.live = True
        for worker in ({'return_value': self.file_result(path='/workspace/notes.md')},
                       {'return_value': {'kind': 'error', 'error': 'missing'}}):
            with self.subTest(worker=worker), patch.object(self.metrics, 'take_live_reminder', return_value=40.0), \
                    patch('orchestrator.codex_agent.read_file', **worker):
                result = await self.runner._request('item/tool/call', {'callId': 'live-read',
                    'tool': 'read', 'arguments': {'file_path': '/workspace/notes.md'}})
            self.assertNotIn(LIVE_NOTE, response_text(result))
            self.assertIn(LIVE_NOTE, self.steer_params()['input'][0]['text'])
            self.record_steer()

    async def test_dynamic_reminders_cover_success_and_early_rejections_without_return_visibility(self):
        cases = [('celeste_narrate', {'thought': 'test'}), ('celeste_write', {}),
                 ('read', ['invalid']), ('unknown', {}), ('celeste_observe', {})]
        self.runner.study = None
        for tool, args in cases:
            with self.subTest(tool=tool):
                self.runner.note_reminders.pending['after'] = (1, 'HIDDEN-RETURN-NOTICE')
                result = await self.runner._request('item/tool/call', {'tool': tool, 'arguments': args})
                self.assertNotIn('HIDDEN-RETURN-NOTICE', response_text(result))
                params = self.steer_params()
                self.assertIn('HIDDEN-RETURN-NOTICE', params['input'][0]['text'])
                self.assertTrue(self.runner.note_reminders.peek())
                # Ignore the entire dynamic tool return, as a code-mode wrapper may do.
                self.record_steer(params)
                self.assertEqual(self.runner.note_reminders.peek(), ())

    async def test_dynamic_reminder_delivery_failure_timeout_and_turn_end_keep_retryable_state(self):
        from orchestrator.codex_agent import LIVE_NOTE
        for error in (RuntimeError('rejected'), TimeoutError('unknown queue state')):
            with self.subTest(error=type(error).__name__):
                self.runner.turn_done.clear()
                self.runner.rpc.request = AsyncMock(side_effect=error)
                self.runner.note_reminders.pending['after'] = (1, 'RETRY-NOTICE')
                with patch.object(self.metrics, 'take_live_reminder', return_value=40.0), \
                        patch.object(self.metrics, 'restore_live_reminder') as restore:
                    result = await self.runner._request('item/tool/call', {'tool': 'unknown', 'arguments': {}})
                    self.assertFalse(result['success'])
                    self.assertTrue(self.runner.note_reminders.peek())
                    self.assertEqual(bool(self.runner._inflight_notes), isinstance(error, TimeoutError))
                    if isinstance(error, TimeoutError):
                        restore.assert_not_called()
                    self.runner._event('turn/completed', {'turn': {'id': 'turn', 'status': 'interrupted'}})
                    self.assertEqual(self.runner._inflight_notes, set())
                    restore.assert_called_once_with(40.0)
                # An ended originating turn must never forward the reminder into another turn.
                self.runner.rpc.request = AsyncMock(return_value={'turnId': 'turn'})
                await self.runner._request('item/tool/call', {'tool': 'unknown', 'arguments': {}})
                self.runner.rpc.request.assert_not_awaited()
                self.runner.turn_done.clear()
                with patch.object(self.metrics, 'take_live_reminder', return_value=40.0):
                    await self.runner._request('item/tool/call', {'tool': 'unknown', 'arguments': {}})
                params = self.steer_params()
                self.assertIn('RETRY-NOTICE', params['input'][0]['text'])
                self.assertIn(LIVE_NOTE, params['input'][0]['text'])
                self.record_steer(params)
                self.assertEqual(self.runner.note_reminders.peek(), ())

    async def test_gate_wait_cannot_rebind_old_dynamic_result_or_reminder_to_new_turn(self):
        self.runner.study = None
        for tool in ('read', 'celeste_observe'):
            with self.subTest(tool=tool):
                self.runner.turn_id = 'turn'
                self.runner.rpc.request.reset_mock()
                self.server.call_tool.reset_mock()
                self.runner.note_reminders.pending['after'] = (1, 'OLD-TURN-REMINDER')

                async def decide(*args):
                    self.runner.turn_id = 'successor'
                    return True, ''

                self.runner.gate = SimpleNamespace(enabled=True, note_tool_use=Mock(), decide=decide)
                with patch('orchestrator.codex_agent.read_file', return_value=self.file_result(path='/workspace/x')):
                    result = await self.runner._request('item/tool/call', {'tool': tool,
                        'arguments': {'file_path': '/workspace/x'} if tool == 'read' else {}})
                self.assertFalse(result['success'])
                self.runner.rpc.request.assert_not_awaited()
                self.server.call_tool.assert_not_awaited()
                self.assertTrue(self.runner.note_reminders.peek())
                self.assertEqual(self.runner._inflight_notes, set())

    async def test_glob_grep_and_listing_need_no_program_budget_in_readonly_chat_or_pause(self):
        self.runner.chat_only = self.runner.paused = True
        charges = []
        self.runner.native_session = SimpleNamespace(cfg=self.cfg, program_guard=lambda: '当前为只读聊天',
                                                     program_run_denied=lambda: charges.append('program'))
        self.runner.gate = SimpleNamespace(enabled=True, note_tool_use=Mock(), decide=AsyncMock(return_value=(True, '')))
        for tool, operation in (('glob', 'glob'), ('grep', 'grep')):
            found = {'kind': operation, 'path': '/workspace', 'text': '/workspace/notes.md', 'images': [],
                     'matches': 1, 'truncated': False, 'next_offset': None}
            with patch('orchestrator.codex_agent.search_files', return_value=found) as search:
                result = await self.runner._request('item/tool/call', {'callId': tool, 'tool': tool,
                    'arguments': {'pattern': '*.md'}})
            self.assertTrue(result['success'], result)
            search.assert_called_once_with('container', operation, {'pattern': '*.md',
                '_tool_names': {'read': 'read', 'glob': 'glob', 'grep': 'grep'}})
            self.assertIn('/workspace/notes.md', self.steer_params()['input'][0]['text'])
        listing = {'kind': 'directory', 'path': '/workspace', 'text': '目录 /workspace', 'images': [],
                   'entries': 1, 'truncated': False, 'next_offset': None}
        with patch('orchestrator.codex_agent.read_file', return_value=listing):
            result = await self.runner._request('item/tool/call', {'callId': 'ls', 'tool': 'read',
                'arguments': {'file_path': '/workspace'}})
        self.assertTrue(result['success'])
        self.assertIn('目录列表', response_text(result))
        self.assertEqual(charges, [])
        self.assertEqual([call.args[0] for call in self.runner.gate.decide.await_args_list], ['Glob', 'Grep', 'Read'])
        # The native shell is still a program in the same chat.
        denied = await native_tool_hook(self.runner, self.native_request('ls-shell', command='ls /workspace'))
        self.assertEqual(denied['decision'], 'deny')

    async def test_readonly_chat_keeps_workspace_edits_and_denies_programs(self):
        runner = CodexRunner(self.cfg, self.root / 'chat', None, self.metrics, 'container', None, chat_only=True)
        captured = {}
        async def rpc_request(method, params, **kwargs):
            captured[method] = params
            raise RuntimeError('captured thread parameters')
        runner.rpc.start, runner.rpc.close, runner.rpc.request = AsyncMock(), AsyncMock(), rpc_request
        with self.assertRaisesRegex(RuntimeError, 'captured'):
            await runner.run()
        self.assertEqual(captured['thread/start']['permissions'], 'celeste-write')
        self.assertIn('apply_patch', captured['thread/start']['baseInstructions'])
        runner.session_id, runner.turn_id, runner.turn_active = 'session', 'turn', True
        runner.turn_done.clear()
        def hook(ident, tool, tool_input):
            return {'token': runner.native_token, 'session_id': 'session', 'turn_id': 'turn', 'tool_use_id': ident,
                    'tool_name': tool, 'hook_event_name': 'PreToolUse', 'tool_input': tool_input}
        edit = await native_tool_hook(runner, hook('notes-edit', 'apply_patch', {'command': '*** Begin Patch'}))
        self.assertEqual(edit['decision'], 'allow')
        program = await native_tool_hook(runner, hook('notes-shell', 'Bash', {'command': 'python3 tidy.py'}))
        self.assertEqual(program['decision'], 'deny')
        self.assertIn('只读聊天', program['reason'])
        runner.close_events()

    def test_resume_without_read_registration_continues_when_no_unfinished_study(self):
        path = self.root / 'window-thread.jsonl'
        tools = ['celeste_' + name for name in ('observe', 'act', 'narrate',
                 'set_effort', 'stop_run', 'exit_level', 'cutscene', 'simulate', 'finish_preparation')]
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name}
            for name in tools]}}), encoding='utf-8')
        for study in (None, 'complete'):
            with self.subTest(study=study):
                self.runner.tool_notice = ''
                if study == 'complete':
                    self.study.read['README.md'] = {1, 2}
                self.runner.study = self.study if study else None
                with patch('builtins.print') as log:
                    self.runner._validate_resumed_tools({'path': str(path)})
                self.assertIn('read', self.runner.tool_notice)
                self.assertIn('read', ' '.join(str(call.args[0]) for call in log.call_args_list))

    async def test_resumed_session_without_file_tools_reaches_model_with_notice(self):
        path = self.root / 'old-thread.jsonl'
        tools = [{'name': 'celeste_' + name} for name in game_tools(self.cfg)] + [{'name': 'read'}]
        path.write_text(json.dumps({'payload': {'dynamic_tools': tools}}), encoding='utf-8')
        runner = CodexRunner(self.cfg, self.root / 'resume', None, self.metrics, 'container', None,
                             session_id='original', resume=True, resume_note='继续试玩。')
        sent = []
        async def rpc_request(method, params, **kwargs):
            if method == 'thread/resume':
                return {'thread': {'id': 'original', 'path': str(path)}, 'model': 'test-model',
                        'activePermissionProfile': {'id': params['permissions']}}
            if method == 'turn/start':
                sent.append(params['input'][0]['text'])
                raise RuntimeError('captured first turn')
            return {}
        runner.rpc.start, runner.rpc.close, runner.rpc.request = AsyncMock(), AsyncMock(), rpc_request
        with patch('builtins.print'), patch('orchestrator.model_catalog.load_models', return_value={'models': []}), \
                self.assertRaisesRegex(RuntimeError, 'captured first turn'):
            await runner.run()
        self.assertIn('继续试玩。', sent[0])
        self.assertIn('glob', sent[0])
        self.assertIn('read 读取目录', sent[0])
        runner.close_events()
        # A resumed chat turn carries the user's message and the same notice.
        chat = CodexRunner(self.cfg, self.root / 'resume-chat', None, self.metrics, 'container', None,
                           session_id='original', resume=True, chat_only=True)
        chat.rpc.start, chat.rpc.close, chat.rpc.request = AsyncMock(), AsyncMock(), rpc_request
        chat.rpc.reader = asyncio.get_running_loop().create_future()
        chat.control.enqueue('message', '复盘一下上一章')
        with patch('builtins.print'), patch('orchestrator.model_catalog.load_models', return_value={'models': []}), \
                self.assertRaisesRegex(RuntimeError, 'captured first turn'):
            await asyncio.wait_for(chat.run(), 5)
        self.assertIn('复盘一下上一章', sent[1])
        self.assertIn('glob', sent[1])
        chat.close_events()

    async def test_codex_study_reminders_name_read_and_claude_keeps_read(self):
        from calibration.collision_contract_test import FakeBridge
        from orchestrator.game_session import GameSession
        from orchestrator.server import build_server
        expected = {'codex': '请用 read 读完这些行',
                    'claude': '请用 Read 读完这些行再开始（文件长时 Read 会截断，用 offset 与 limit 接着读，直到最后一行）。'}
        cases = [('codex', 'read', expected['codex']),
                 ('codex', 'celeste_read', '请用 celeste_read 读完这些行'),
                 ('claude', 'Read', expected['claude'])]
        for engine, reader, sentence in cases:
            with self.subTest(engine=engine, reader=reader):
                cfg = RunConfig(agent=f'remind-{engine}', save='test', model='test', engine=engine)
                run = self.root / (engine + '-' + reader)
                run.mkdir()
                metrics = Metrics(run / 'metrics.json', 'remind', 'test', 'high', 600)
                session = GameSession(cfg, run, metrics)
                session.client = FakeBridge()
                if engine == 'codex':
                    self.runner.registered_tools = {reader}
                    session.file_tool_name = self.runner._file_tool_name
                try:
                    server = build_server(session, KbStudy(self.kb, 10))
                    result = await server.call_tool('observe', {})
                finally:
                    session.close(False)
                text = json.dumps(result, ensure_ascii=False, default=str)
                self.assertIn(sentence, text)
                if engine == 'codex':
                    self.assertNotIn('请用 Read', text)
                if reader == 'celeste_read':
                    self.assertNotIn('请用 read ', text)
        # Claude's own reminder text is unchanged.
        self.assertIn(expected['claude'], self.study.reminder())

    def test_preparation_prompt_requires_explicit_read_without_disabling_native_programming(self):
        self.runner.prepare_only = True
        prompt = self.runner._first_prompt()
        self.assertIn('read', prompt)

    def register_file_tools(self, names):
        path = self.root / 'file-registration.jsonl'
        registered = {'celeste_' + name for name in game_tools(self.cfg)} | set(names)
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name}
            for name in sorted(registered)]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})
        self.assertEqual(self.runner.registered_tools, registered)
        return registered

    def test_complete_legacy_catalog_preserves_names_and_uses_registered_reader_in_prompt(self):
        self.register_file_tools({'celeste_read', 'celeste_glob', 'celeste_grep'})
        self.assertEqual({name: self.runner._file_tool_name(name) for name in ('read', 'glob', 'grep')},
                         {'read': 'celeste_read', 'glob': 'celeste_glob', 'grep': 'celeste_grep'})
        self.runner.prepare_only = True
        prompt = self.runner._first_prompt()
        self.assertIn('用 celeste_read', prompt)
        self.assertNotRegex(prompt, r'用 read(?:\s|，|的)')
        self.assertIn('celeste_glob', self.runner.tool_notice)
        self.assertIn('celeste_grep', self.runner.tool_notice)

    def test_mixed_catalog_resolves_each_file_tool_and_prefers_canonical(self):
        self.register_file_tools({'read', 'celeste_read', 'celeste_glob', 'grep', 'celeste_grep'})
        self.assertEqual({name: self.runner._file_tool_name(name) for name in ('read', 'glob', 'grep')},
                         {'read': 'read', 'glob': 'celeste_glob', 'grep': 'grep'})
        self.assertEqual(self.runner._reader_name(), 'read')

    def test_missing_single_search_tool_does_not_disable_registered_reader(self):
        for missing in ('glob', 'grep'):
            with self.subTest(missing=missing):
                self.runner.tool_notice = ''
                self.register_file_tools({'read', 'glob', 'grep'} - {missing})
                self.assertIsNone(self.runner._file_tool_name(missing))
                self.assertEqual(self.runner._file_tool_name('read'), 'read')
                self.assertIn(missing, self.runner.tool_notice)
                self.runner.prepare_only = True
                self.assertIn('用 read', self.runner._first_prompt())

    def test_missing_only_read_blocks_unfinished_study_but_allows_post_study_resume(self):
        with self.assertRaisesRegex(RuntimeError, '没有登记 read'):
            self.register_file_tools({'glob', 'grep'})
        self.assertIsNone(self.runner._file_tool_name('read'))
        self.assertEqual(self.runner._file_tool_name('glob'), 'glob')
        self.assertEqual(self.runner._file_tool_name('grep'), 'grep')
        self.runner.study = None
        self.register_file_tools({'glob', 'grep'})
        self.assertIn('本会话未登记', self.runner._reader_name())
        self.assertIn('只读聊天中无法读取文件', self.runner.tool_notice)

    async def test_canonical_and_legacy_file_requests_share_backend_gate_and_kb_receipt(self):
        self.runner.gate = SimpleNamespace(enabled=True, note_tool_use=Mock(),
                                          decide=AsyncMock(return_value=(True, '')))
        self.register_file_tools({'celeste_read', 'celeste_glob', 'celeste_grep'})
        before = self.metrics.state.tool_calls
        for name in ('read', 'celeste_read'):
            with self.subTest(name=name), patch('orchestrator.codex_agent.read_file',
                    return_value=self.file_result()) as worker:
                result = await self.runner._request('item/tool/call', {'callId': name,
                    'tool': name, 'arguments': {'file_path': '/kb/README.md'}})
            self.assertTrue(result['success'], result)
            self.assertEqual(worker.call_args.args[0], 'container')
            self.assertEqual(worker.call_args.args[1]['file_path'], '/kb/README.md')
            self.assertEqual(worker.call_args.args[1]['_tool_names']['read'], 'celeste_read')
            self.assertIn('"tool": "' + name + '"', self.steer_params()['input'][0]['text'])
            self.record_steer()
            self.assertEqual(self.study.read['README.md'], {1})
        self.assertEqual(self.metrics.state.tool_calls - before, 2)
        self.assertEqual([call.args[0] for call in self.runner.gate.decide.await_args_list], ['Read', 'Read'])
        for canonical in ('glob', 'grep'):
            for name in (canonical, 'celeste_' + canonical):
                with self.subTest(name=name), patch('orchestrator.codex_agent.search_files', return_value={
                        'kind': canonical, 'text': 'match', 'path': '/workspace', 'images': [], 'matches': 1}) as worker:
                    result = await self.runner._request('item/tool/call', {'callId': name,
                        'tool': name, 'arguments': {'pattern': 'test'}})
                self.assertTrue(result['success'], result)
                self.assertEqual(worker.call_args.args[:2], ('container', canonical))
                self.assertEqual(worker.call_args.args[2]['_tool_names']['read'], 'celeste_read')
        self.assertEqual([call.args[0] for call in self.runner.gate.decide.await_args_list],
                         ['Read', 'Read', 'Glob', 'Glob', 'Grep', 'Grep'])

    async def test_stop_run_can_report_blockage_while_paused_or_study_incomplete(self):
        self.server.call_tool.return_value = CallToolResult.model_validate({'content': [{'type': 'text', 'text': 'paused'}]})
        self.runner.paused=True
        self.runner.gate=SimpleNamespace(enabled=True,decide=AsyncMock(return_value=(False,'无额度')))
        result=await self.runner._request('item/tool/call',{'tool':'celeste_stop_run','arguments':{'reason':'工具故障'}})
        self.assertTrue(result['success'])
        self.server.call_tool.assert_awaited_with('stop_run',{'reason':'工具故障'})
        self.runner.gate.decide.assert_not_awaited()
        self.runner.chat_only=True
        denied=await self.runner._request('item/tool/call',{'tool':'celeste_stop_run','arguments':{'reason':'工具故障'}})
        self.assertFalse(denied['success'])

    async def test_initial_catalog_covers_later_play_settings_without_mutating_live_server(self):
        # The real MCP schema builder must describe all optional interfaces even
        # when preparation starts with each capability disabled.
        self.server.list_tools = AsyncMock(return_value=[])
        expected_game = {'celeste_observe', 'celeste_act', 'celeste_narrate', 'celeste_set_effort',
                         'celeste_stop_run', 'celeste_exit_level', 'celeste_cutscene', 'celeste_simulate'}
        catalogs = []
        for preparing, chat, live, simulate, cutscene in [
                (True, False, False, False, 'auto'),
                (False, True, True, False, 'auto'),
                (False, False, False, True, 'agent'),
                (False, False, True, True, 'agent')]:
            with self.subTest(preparing=preparing, chat=chat, live=live, simulate=simulate):
                self.runner.prepare_only, self.runner.chat_only = preparing, chat
                self.cfg.live, self.cfg.sim_assist, self.cfg.cutscene_mode = live, simulate, cutscene
                original = deepcopy(vars(self.cfg))
                tools = await self.runner.tool_specs()
                catalogs.append({tool['name']: tool['inputSchema'] for tool in tools})
                self.assertTrue(expected_game <= catalogs[-1].keys())
                self.assertNotIn('celeste_bash', catalogs[-1])
                self.assertIn('read', catalogs[-1])
                schema = catalogs[-1]['read']['properties']
                self.assertTrue({'file_path', 'path', 'offset', 'limit', 'pages'} <= schema.keys())
                self.assertNotIn('celeste_write', catalogs[-1])
                self.assertEqual(vars(self.cfg), original)
                self.assertFalse(hasattr(self.server, 'cutscene_tool'))
                self.assertFalse(hasattr(self.server, 'reset_observation_delivery'))
        self.server.list_tools.assert_not_awaited()
        self.assertTrue(all(catalog == catalogs[0] for catalog in catalogs[1:]))

    async def test_preparation_registration_resumes_after_optional_capabilities_change(self):
        self.cfg.live, self.cfg.sim_assist, self.cfg.cutscene_mode = False, False, 'auto'
        self.runner.prepare_only = True
        registered = await self.runner.tool_specs()
        path = self.root/'prepared-thread.jsonl'
        path.write_text(json.dumps({'payload': {'dynamic_tools': registered}}), encoding='utf-8')
        original = path.read_bytes()
        for live, simulate, cutscene in [(True, True, 'agent'), (False, False, 'auto')]:
            self.runner.prepare_only = False
            self.cfg.live, self.cfg.sim_assist, self.cfg.cutscene_mode = live, simulate, cutscene
            self.runner._validate_resumed_tools({'path': str(path)})
            self.assertEqual(path.read_bytes(), original)

    async def test_optional_catalog_entries_do_not_enable_execution_or_spend_planning_budget(self):
        self.runner.study = None
        self.cfg.live, self.cfg.sim_assist, self.cfg.cutscene_mode = False, False, 'auto'
        self.runner.gate = SimpleNamespace(enabled=True, note_tool_use=Mock(),
                                           decide=AsyncMock(return_value=(True, '')))
        arguments = {'celeste_simulate': {}, 'celeste_exit_level': {'reason': 'user requested'},
                     'celeste_cutscene': {'action': 'advance'}}
        for name, args in arguments.items():
            with self.subTest(tool=name):
                result = await self.runner._request('item/tool/call', {'tool': name, 'arguments': args})
                self.assertFalse(result['success'])
                text = result['contentItems'][0]['text']
                self.assertIn('当前试玩配置未启用', text)
                self.assertIn(name, text)
                self.assertIn('未执行', text)
        self.server.call_tool.assert_not_awaited()
        self.runner.gate.note_tool_use.assert_not_called()
        self.runner.gate.decide.assert_not_awaited()
        self.runner.gate = None
        self.cfg.live, self.cfg.sim_assist, self.cfg.cutscene_mode = True, True, 'agent'
        self.server.call_tool.return_value = CallToolResult.model_validate({
            'content': [{'type': 'text', 'text': 'executed once'}]})
        for name, args in arguments.items():
            with self.subTest(enabled_tool=name):
                self.server.call_tool.reset_mock()
                result = await self.runner._request('item/tool/call', {'tool': name, 'arguments': args})
                self.assertTrue(result['success'])
                self.server.call_tool.assert_awaited_once_with(name.removeprefix('celeste_'), args)

    async def test_phase_restrictions_still_explain_rejected_registered_optional_tools(self):
        self.runner.study = None
        self.cfg.live, self.cfg.sim_assist, self.cfg.cutscene_mode = False, False, 'auto'
        for preparing, chat, paused, explanation in [
                (True, False, False, '知识库准备'),
                (False, True, False, '只提供聊天'),
                (False, False, True, '用户已暂停')]:
            self.runner.prepare_only, self.runner.chat_only, self.runner.paused = preparing, chat, paused
            for name in ('celeste_simulate', 'celeste_exit_level', 'celeste_cutscene'):
                with self.subTest(phase=explanation, tool=name):
                    result = await self.runner._request('item/tool/call', {'tool': name, 'arguments': {}})
                    self.assertFalse(result['success'])
                    self.assertIn(explanation, result['contentItems'][0]['text'])
        self.server.call_tool.assert_not_awaited()

    async def test_exit_tool_is_forwarded_and_denied_when_paused_or_chat_only(self):
        self.runner.study=None
        result=await self.runner._request('item/tool/call',{'tool':'celeste_exit_level','arguments':{'reason':'用户要求退出'}})
        self.assertTrue(result['success'])
        self.server.call_tool.assert_awaited_with('exit_level',{'reason':'用户要求退出'})
        for paused,chat_only in [(True,False),(False,True)]:
            self.runner.paused,self.runner.chat_only=paused,chat_only
            self.server.call_tool.reset_mock()
            denied=await self.runner._request('item/tool/call',{'tool':'celeste_exit_level','arguments':{'reason':'退出'}})
            self.assertFalse(denied['success']);self.server.call_tool.assert_not_awaited()

    async def test_exit_tool_never_uses_container_shell(self):
        self.runner.study=None
        self.runner.session=SimpleNamespace()
        with patch('orchestrator.sandbox.subprocess.run') as shell:
            result=await self.runner._request('item/tool/call',{'tool':'celeste_exit_level',
                'arguments':{'reason':'按用户要求退出'}})
        self.assertTrue(result['success'])
        shell.assert_not_called()
        self.server.call_tool.assert_awaited_with('exit_level',{'reason':'按用户要求退出'})

    async def test_workspace_chat_can_run_sandbox_programs_but_game_tools_stay_closed(self):
        self.runner.chat_only = self.runner.paused = True
        self.runner.allow_programs = True
        data = self.native_request('offline-chat')
        result = await native_tool_hook(self.runner, data)
        self.assertEqual(result['decision'], 'allow')
        self.assertTrue(self.metrics.tools_running)
        await native_tool_hook(self.runner, {**data, 'hook_event_name':'PostToolUse'})
        for name in ('celeste_act', 'celeste_observe', 'celeste_simulate', 'celeste_exit_level', 'celeste_cutscene'):
            denied = await self.runner._request('item/tool/call', {'tool': name, 'arguments': {}})
            self.assertFalse(denied['success'])
        self.server.call_tool.assert_not_awaited()
        self.runner.allow_programs = False
        denied = await native_tool_hook(self.runner, self.native_request('closed-chat'))
        self.assertEqual(denied['decision'], 'deny')
        self.assertIn('只读聊天', denied['reason'])
        self.assertIn('未开放程序运行', denied['reason'])
        self.assertFalse(self.metrics.tools_running)

    async def test_resume_requests_metadata_only_without_mutating_parameters(self):
        expected = {"thread": {"id": "original", "path": str(self.root / "thread.jsonl")},
                    "sandbox": {"type": "readOnly"}, "model": "test-model"}
        self.runner.rpc.request = AsyncMock(return_value=expected)
        params = {"threadId": "original", "sandbox": "read-only", "selectedCapabilityRoots": []}
        result = await self.runner._resume_thread(params)
        self.assertEqual(result, expected)
        self.runner.rpc.request.assert_awaited_once_with("thread/resume", {
            **params, "excludeTurns": True})
        self.assertNotIn("excludeTurns", params)
        self.assertNotIn("turns", result["thread"])
        self.assertEqual(result["thread"]["id"], "original")

    async def test_metadata_only_resume_keeps_dynamic_tool_validation(self):
        from orchestrator.modes import game_tools
        path = self.root / 'resumed-thread.jsonl'
        tools = [{'name': 'celeste_' + name} for name in game_tools(self.cfg)] + [{'name': 'read'}]
        path.write_text(json.dumps({'payload': {'dynamic_tools': tools}}), encoding='utf-8')
        response = {'thread': {'id': 'original', 'path': str(path)}, 'sandbox': {'type': 'readOnly'}}
        self.runner.rpc.request = AsyncMock(return_value=response)
        result = await self.runner._resume_thread({'threadId': 'original', 'sandbox': 'read-only'})
        self.runner._validate_resumed_tools(result['thread'])
        self.assertNotIn('turns', result['thread'])
        path.write_text(json.dumps({'payload': {'dynamic_tools': []}}), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '缺少当前必需工具'):
            self.runner._validate_resumed_tools(result['thread'])

    async def test_writer_conflict_forks_history_and_records_recovery(self):
        expected = {"thread": {"id": "recovered"}}
        self.runner.rpc.request = AsyncMock(side_effect=[
            RuntimeError("thread original already has an active writer"), expected])
        params = {"threadId": "original", "sandbox": "read-only", "selectedCapabilityRoots": []}
        self.assertEqual(await self.runner._resume_thread(params), expected)
        self.runner.rpc.request.assert_awaited_with("thread/fork", {
            "threadId": "original", "sandbox": "read-only", "excludeTurns": True})
        self.assertEqual(json.loads((self.root / "session_recovery.json").read_text())["thread"], "recovered")

    async def test_cutscene_tool_returns_screenshot_alongside_text(self):
        self.runner.study=None
        self.runner.session=SimpleNamespace()
        with patch('orchestrator.sandbox.subprocess.run') as shell:
            result=await self.runner._request('item/tool/call',{'tool':'celeste_cutscene',
                'arguments':{'action':'advance','frames':30}})
        shell.assert_not_called()
        self.server.call_tool.assert_awaited_with('cutscene',{'action':'advance'})
        self.assertTrue(result['success'])
        self.assertNotIn('data:image/', json.dumps(result))
        self.assertEqual(self.runner.rpc.request.call_args.args[1]['input'][1],
                         {'type':'image','url':'data:image/png;base64,AAAA'})

    async def test_image_delivery_failure_keeps_executed_text_and_pauses_without_retry(self):
        self.runner.study = None
        self.runner.rpc.request = AsyncMock(side_effect=TimeoutError('attachment timeout'))
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.assertEqual(result['contentItems'][0]['text'], 'observed')
        self.assertIn('勿重复已执行动作', result['contentItems'][-1]['text'])
        self.assertNotIn('data:image/', json.dumps(result))
        self.assertEqual(self.server.call_tool.await_count, 1)
        self.assertEqual(self.runner.rpc.request.await_count, 1)

    async def test_image_delivery_never_targets_replacement_turn(self):
        self.runner.study = None
        async def changed_turn(*args):
            self.runner.turn_id = 'replacement'
            return self.server.call_tool.return_value
        self.server.call_tool.side_effect = changed_turn
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}})
        self.assertFalse(result['success'])
        self.runner.rpc.request.assert_not_awaited()
        self.assertNotIn('data:image/', json.dumps(result))

    async def test_pause_during_game_call_does_not_deliver_images_or_resume(self):
        self.runner.study = None
        async def paused_during_action(*args):
            self.runner.paused = True
            return self.server.call_tool.return_value
        self.server.call_tool.side_effect = paused_during_action
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.runner.rpc.request.assert_not_awaited()

    async def test_invalid_image_acknowledgment_is_not_reported_delivered(self):
        self.runner.study = None
        self.runner.rpc.request.return_value = {'turnId': 'another-turn'}
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.assertIn('交付失败', result['contentItems'][-1]['text'])

    async def test_action_lock_covers_image_delivery_before_next_game_call(self):
        self.runner.study = None
        sending, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            sending.set()
            await release.wait()
            return {'turnId': 'turn'}
        self.runner.rpc.request.side_effect = send
        first = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}}))
        await sending.wait()
        second = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}}))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(self.server.call_tool.await_count, 1)
        finally:
            release.set()
            results = await asyncio.gather(first, second)
        self.assertTrue(all(r['success'] for r in results))
        self.assertEqual(self.server.call_tool.await_count, 2)

    async def test_queued_pre_reload_action_is_rejected_after_baseline_ack(self):
        self.runner.study = None
        self.runner.session = SimpleNamespace(reload_epoch=0, reload_observation_required=False)
        await self.runner.action_lock.acquire()
        pending = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}}))
        await asyncio.sleep(0)
        self.runner.session.reload_epoch = 1
        self.runner.action_lock.release()
        result = await pending
        self.assertFalse(result['success'])
        self.assertIn('旧计划未执行', result['contentItems'][0]['text'])
        self.server.call_tool.assert_not_awaited()

    async def test_failed_reload_observation_image_keeps_fresh_observe_required(self):
        self.runner.study = None
        self.runner.session = SimpleNamespace(reload_epoch=1, reload_observation_required=False)
        self.runner.rpc.request.side_effect = RuntimeError('image transport failed')
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.session.reload_observation_required)

    async def test_queued_old_turn_action_is_rejected_before_game_execution(self):
        self.runner.study = None
        sending, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            sending.set()
            await release.wait()
            return {'turnId': 'turn'}
        self.runner.rpc.request.side_effect = send
        first = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}}))
        await sending.wait()
        second = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}}))
        await asyncio.sleep(.01)
        self.runner.turn_id = 'replacement'
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(self.server.call_tool.await_count, 1)
        self.assertFalse(results[1]['success'])
        self.assertIn('动作未执行', results[1]['contentItems'][0]['text'])

    def test_public_image_inputs_are_redacted_for_all_protocol_aliases(self):
        value = public_event({'input': [{'type': 'image', 'url': 'data:image/png;base64,PRIVATE'}],
                              'image_url': 'data:image/png;base64,PRIVATE'})
        self.assertNotIn('PRIVATE', json.dumps(value))

    async def test_other_resume_errors_do_not_fork(self):
        self.runner.rpc.request = AsyncMock(side_effect=RuntimeError("connection failed"))
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            await self.runner._resume_thread({"threadId": "original"})
        self.assertEqual(self.runner.rpc.request.await_count, 1)

    def test_resumed_session_reports_missing_dynamic_tools_without_bash_fallback(self):
        path = self.root / 'thread.jsonl'
        tools = ['read', 'celeste_observe', 'celeste_act', 'celeste_cutscene', 'celeste_exit_level',
                 'celeste_narrate', 'celeste_set_effort', 'celeste_stop_run']
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})

        self.cfg.sim_assist = True
        with self.assertRaisesRegex(RuntimeError, 'celeste_simulate'):
            self.runner._validate_resumed_tools({'path': str(path)})
        tools.append('celeste_simulate')
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})


    def test_resume_without_explicit_read_registration_is_rejected(self):
        path = self.root / 'native-only-thread.jsonl'
        tools = ['celeste_' + name for name in ('observe', 'act', 'narrate',
                 'set_effort', 'stop_run', 'exit_level', 'cutscene', 'simulate')]
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name}
            for name in tools]}}), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'read'):
            self.runner._validate_resumed_tools({'path': str(path)})

    def test_resume_does_not_require_tools_disabled_by_play_mode(self):
        self.cfg.live = False
        self.cfg.mode = 'speedrun'
        self.cfg.cutscene_mode = 'auto'
        path = self.root / 'thread.jsonl'
        tools = ['read', 'celeste_observe', 'celeste_act', 'celeste_narrate', 'celeste_set_effort', 'celeste_stop_run']
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})
        self.cfg.live = True
        with self.assertRaisesRegex(RuntimeError, 'celeste_exit_level'):
            self.runner._validate_resumed_tools({'path': str(path)})

    async def test_bash_has_hard_time_limit(self):
        # 探索模式：单次规划程序的上限（explore_program_s，默认 15 秒，与提示词一致）；速通模式：Bash 最长时长 bash_max_s
        async def call():
            data = self.native_request('cap-' + str(self.metrics.state.tool_calls), timeout_ms=1000000)
            decision = await native_tool_hook(self.runner, data)
            self.assertEqual(decision['decision'], 'allow')
            await native_tool_hook(self.runner, {**data, 'hook_event_name':'PostToolUse'})
            return decision['timeout_s']
        self.cfg.mode, self.cfg.knowledge_profile = "explore", "explore"
        self.assertEqual(await call(), 15)
        self.cfg.explore_program_s = 40
        self.assertEqual(await call(), 40)
        self.cfg.mode, self.cfg.knowledge_profile = "speedrun", "full"
        self.assertEqual(await call(), 600)

    async def test_native_program_output_keeps_its_app_server_result_without_legacy_repackaging(self):
        import io
        self.cfg.live=False
        raw='frame=22 bounds=room exits=right\n'+'spinner\n'*1000+'LAST SPINNER'
        data = self.native_request('geometry-output')
        self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'allow')
        event = {'item':{'type':'commandExecution', 'id':'geometry-output', 'aggregatedOutput':raw,
                         'exitCode':0, 'status':'completed'}}
        before = deepcopy(event)
        self.runner._events = io.StringIO()
        self.runner._event('item/completed', event)
        await native_tool_hook(self.runner, {**data, 'hook_event_name':'PostToolUse'})
        self.assertEqual(event, before)
        self.assertIsNone(self.runner.fatal)
        saved = json.loads(self.runner._events.getvalue())['params']['item']
        self.assertEqual(saved['aggregatedOutput'], raw)
        self.assertNotIn('stdout_truncated', saved)
        self.assertFalse(self.metrics.tools_running)

    async def test_legacy_programming_entries_explain_disabled_without_execution(self):
        for name in ('celeste_bash', 'celeste_write'):
            with self.subTest(tool=name), patch('orchestrator.codex_agent.read_file') as files:
                result = await self.runner._request('item/tool/call', {'tool':name,
                    'arguments':{'command':'python3 old.py', 'path':'/workspace/old.py', 'content':'old'}})
                self.assertFalse(result['success'])
                text = result['contentItems'][0]['text']
                self.assertIn('入口已停用', text)
                self.assertIn('未执行', text)
                self.assertIn('原生 Shell', text)
                files.assert_not_called()
                self.server.call_tool.assert_not_called()

    async def test_host_permission_request_is_never_approved(self):
        with self.assertRaises(RuntimeError):
            await self.runner._request("item/permissions/requestApproval", {})

    async def test_gate_denial_prevents_sandbox_program(self):
        from orchestrator.pausegate import PauseGate
        self.runner.gate = PauseGate(self.metrics, enabled=True, started=True, sim_only=True)
        result = await native_tool_hook(self.runner, self.native_request('b1'))
        self.assertEqual(result['decision'], 'deny')
        self.assertIn('轮次', result['reason'])
        self.assertFalse(self.metrics.tools_running)
        self.assertEqual(self.runner.gate.bank, 0)

    def test_usage_and_compaction_are_actual_events(self):
        self.runner._event("thread/tokenUsage/updated", {"tokenUsage": {
            "last": {"inputTokens": 100, "outputTokens": 20}, "modelContextWindow": 1000,
            "total": {"inputTokens": 999999, "outputTokens": 70, "reasoningOutputTokens": 30}}})
        for _ in range(2):
            self.runner._event("item/completed", {"item": {"type": "contextCompaction", "id": "one"}})
        self.assertEqual(self.metrics.state.context_tokens, 120)
        self.assertEqual(self.metrics.state.context_window, 1000)
        self.assertEqual(self.metrics.state.compactions, 1)
        self.assertEqual(self.metrics.state.output_tokens, 70)

    def test_nested_reasoning_is_not_written_to_event_log(self):
        data = public_event({"turn": {"items": [{"type": "reasoning", "id": "r", "summary": "PRIVATE", "content": "PRIVATE"},
                                               {"type": "agentMessage", "text": "公开解说"}]}})
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertIn("公开解说", json.dumps(data, ensure_ascii=False))

    def preparation(self):
        return {"quick_check": False, "native_game_tools": True, "session_id": "native-thread", "metrics": {"agent": self.cfg.agent},
                "kb_study": {"complete": True, "files": {"README.md": [2, 2]}},
                "kb_hashes": {"README.md": hashlib.sha256((self.kb / "README.md").read_bytes()).hexdigest()}}

    def test_prepared_session_requires_same_files_and_complete_reading(self):
        data = self.preparation()
        (self.root / "prepare_summary.json").write_text(json.dumps(data), encoding="utf-8")
        restored = load_preparation(str(self.root), self.cfg, self.study)
        self.assertEqual(restored["session_id"], "native-thread")
        self.assertTrue(self.study.complete())
        (self.kb / "README.md").write_text("modified\ntwo\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_preparation(str(self.root), self.cfg, KbStudy(self.kb, 10))

    def test_codex_preparation_uses_actual_workspace_capability_not_client_label(self):
        data = {**self.preparation(), 'runtime': 'container', 'engine': 'codex'}
        (self.root / 'prepare_summary.json').write_text(json.dumps(data), encoding='utf-8')
        self.cfg.runtime = 'host'
        restored = load_preparation(str(self.root), self.cfg, self.study)
        self.assertEqual(restored['session_id'], 'native-thread')
        self.assertTrue(self.study.complete())

    def test_quick_check_cannot_be_used_as_full_preparation(self):
        data = {**self.preparation(), "quick_check": True}
        (self.root / "prepare_summary.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_preparation(str(self.root), self.cfg, self.study)

    def test_explore_profile_isolates_kb_workspace_and_rejects_old_session(self):
        old_workspace = self.cfg.workspace_dir
        self.cfg.knowledge_profile = "explore"
        self.assertNotEqual(old_workspace, self.cfg.workspace_dir)
        self.assertEqual(self.cfg.kb_dir.name, "explore")
        self.assertNotIn("reference", str(self.cfg.kb_dir))
        study = KbStudy(self.cfg.kb_dir, 10)
        self.assertEqual(set(study.files), {"README.md", "basics.md", "data.md", "tools.md"})   # 系列赛自己加的资料走 --kb-extra
        self.assertNotIn("help/controls.md", study.files)
        data = self.preparation()
        (self.root / "prepare_summary.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "知识范围不同"):
            load_preparation(str(self.root), self.cfg, study)

    async def test_prepared_manifest_reaches_game_session(self):
        from orchestrator.run import run_async
        manifest = self.preparation()
        args = SimpleNamespace(no_agent=False, prepared=str(self.root), no_hud=True, no_hud_window=True)
        with patch("orchestrator.prepared.load_preparation", return_value=manifest), \
             patch("orchestrator.run._run_session", new_callable=AsyncMock, return_value={}) as run:
            await run_async(self.cfg, args, self.root, None)
        self.assertIs(run.call_args.args[-1], manifest)


if __name__ == "__main__":
    unittest.main()
