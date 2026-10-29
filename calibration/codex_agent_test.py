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

    def tearDown(self):
        self.temp.cleanup()

    async def read(self, offset, *, visible=None):
        ident = f'read-{offset}'
        command = f"sed -n '{offset}p' /kb/README.md"
        self.runner._event('rawResponseItem/completed', {'turnId': 'turn', 'item': {
            'type': 'custom_tool_call', 'call_id': ident, 'name': 'functions.exec',
            'input': f'text((await tools.exec_command({json.dumps({"cmd": command})})).output)'}})
        data = self.native_request(ident, command=command)
        decision = await native_tool_hook(self.runner, data)
        self.assertEqual(decision['decision'], 'allow')
        text = ('one\n' if offset == 1 else 'two\n') if visible is None else visible
        # Internal stdout alone is not evidence of what code mode returned.
        self.runner._event('item/completed', {'item': {'type': 'commandExecution',
            'id': ident, 'cwd': '/kb', 'aggregatedOutput': 'one\ntwo\n', 'exitCode': 0, 'status': 'completed'}})
        self.runner._event('rawResponseItem/completed', {'turnId': 'turn', 'item': {
            'type': 'custom_tool_call_output', 'call_id': ident,
            'output': [{'type': 'input_text', 'text': text}]}})
        return await native_tool_hook(self.runner, {**data, 'hook_event_name': 'PostToolUse'})

    def native_request(self, ident, **arguments):
        return {'token': self.runner.native_token, 'session_id': self.runner.session_id,
                'turn_id': self.runner.turn_id, 'tool_use_id': ident, 'tool_name': 'Bash',
                'hook_event_name': 'PreToolUse', 'tool_input': {'command': 'python offline.py', **arguments}}

    async def test_native_read_uses_command_directory_and_retries_delayed_rollout(self):
        self.runner._native_rollout_path = '/session/rollout.jsonl'
        self.runner._event('turn/started', {'turn': {'id': 'turn'}})
        self.runner._event('rawResponseItem/completed', {'turnId': 'turn', 'item': {
            'type': 'custom_tool_call', 'call_id': 'relative'}})
        data = self.native_request('relative-command', command='cat README.md')
        with patch('orchestrator.codex_container.read_rollout', side_effect=RuntimeError('not yet flushed')):
            self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'allow')
        self.assertEqual(self.runner._native_rollout_offset, 0)
        self.runner._event('item/completed', {'item': {'type': 'commandExecution',
            'id': 'relative-command', 'cwd': '/kb', 'exitCode': 0, 'status': 'completed'}})
        self.runner._event('rawResponseItem/completed', {'turnId': 'turn', 'item': {
            'type': 'custom_tool_call_output', 'call_id': 'relative',
            'output': [{'type': 'input_text', 'text': 'one\ntwo\n'}]}})
        self.assertTrue(self.study.complete())
        self.assertFalse(self.server.call_tool.called)
        with patch('orchestrator.codex_container.read_rollout') as tail:
            await self.runner._sync_native_reads()
            await self.runner._sync_native_reads()
        tail.assert_not_called()
        self.assertTrue(self.runner._native_read_finished)

    async def test_failed_native_read_does_not_credit_matching_error_output(self):
        self.runner._event('turn/started', {'turn': {'id': 'turn'}})
        self.runner._event('rawResponseItem/completed', {'turnId': 'turn', 'item': {
            'type': 'custom_tool_call', 'call_id': 'failed'}})
        data = self.native_request('failed-command', command='cat /kb/README.md')
        await native_tool_hook(self.runner, data)
        self.runner._event('item/completed', {'item': {'type': 'commandExecution',
            'id': 'failed-command', 'cwd': '/kb', 'exitCode': 1, 'status': 'completed'}})
        self.runner._event('rawResponseItem/completed', {'turnId': 'turn', 'item': {
            'type': 'custom_tool_call_output', 'call_id': 'failed',
            'output': [{'type': 'input_text', 'text': 'one\ntwo\n'}]}})
        self.assertFalse(self.study.read['README.md'])

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
        self.assertIsNotNone(self.runner._native_reads)
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

    async def test_internal_read_output_not_forwarded_to_model_does_not_credit_study(self):
        await self.read(1, visible='Read completed but content was not returned.\n')
        self.assertEqual(self.study.read['README.md'], set())
        denied = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
        self.assertFalse(denied['success'])
        self.server.call_tool.assert_not_called()

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
                self.assertNotIn('celeste_read', catalogs[-1])
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        tools = [{'name': 'celeste_' + name} for name in game_tools(self.cfg)]
        path.write_text(json.dumps({'payload': {'dynamic_tools': tools}}), encoding='utf-8')
        response = {'thread': {'id': 'original', 'path': str(path)}, 'sandbox': {'type': 'readOnly'}}
        self.runner.rpc.request = AsyncMock(return_value=response)
        result = await self.runner._resume_thread({'threadId': 'original', 'sandbox': 'read-only'})
        self.runner._validate_resumed_tools(result['thread'])
        self.assertNotIn('turns', result['thread'])
        path.write_text(json.dumps({'payload': {'dynamic_tools': []}}), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '缺少正式游戏工具'):
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
        self.runner.rpc.request.return_value = {'turnId': 'another-turn'}
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.assertIn('交付失败', result['contentItems'][-1]['text'])

    async def test_action_lock_covers_image_delivery_before_next_game_call(self):
        self.runner.study = None
        self.runner._native_reads = None
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
        self.runner._native_reads = None
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
        self.runner._native_reads = None
        self.runner.session = SimpleNamespace(reload_epoch=1, reload_observation_required=False)
        self.runner.rpc.request.side_effect = RuntimeError('image transport failed')
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.session.reload_observation_required)

    async def test_queued_old_turn_action_is_rejected_before_game_execution(self):
        self.runner.study = None
        self.runner._native_reads = None
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
        tools = ['celeste_observe', 'celeste_act', 'celeste_cutscene', 'celeste_exit_level',
                 'celeste_narrate', 'celeste_set_effort', 'celeste_stop_run']
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})
        self.cfg.sim_assist = True
        with self.assertRaisesRegex(RuntimeError, 'celeste_simulate'):
            self.runner._validate_resumed_tools({'path': str(path)})
        tools.append('celeste_simulate')
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})

    def test_resume_does_not_require_tools_disabled_by_play_mode(self):
        self.cfg.live = False
        self.cfg.mode = 'speedrun'
        self.cfg.cutscene_mode = 'auto'
        path = self.root / 'thread.jsonl'
        tools = ['celeste_observe', 'celeste_act', 'celeste_narrate', 'celeste_set_effort', 'celeste_stop_run']
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
        for name in ('celeste_bash', 'celeste_write', 'celeste_read'):
            with self.subTest(tool=name), patch('orchestrator.codex_workspace.workspace_call') as files:
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
