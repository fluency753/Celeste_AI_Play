"""Fixed last-tool journal through real MCP and Codex consumers.

Only the game bridge, model capability lookup and filesystem fault boundary
are fixtures. No live controller, account, OBS or user configuration is read.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mcp.types import CallToolRequestParams

from calibration.collision_contract_test import FakeBridge, observation
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


class JournalBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.steps = []
        self.entered = None
        self.release = None
        self.fail_after_step = False
        self.inspect_record = None

    def call(self, command, **args):
        if command == 'step':
            self.steps.append(deepcopy(args['inputs']))
            if self.inspect_record:
                self.inspect_record()
            if self.entered:
                self.entered.set()
            if self.release and not self.release.wait(3):
                raise TimeoutError('test did not release bridge step')
            result = super().call(command, **args)
            if self.fail_after_step:
                if isinstance(self.fail_after_step, BaseException):
                    raise self.fail_after_step
                raise RuntimeError('bridge disconnected after input was sent')
            return result
        return super().call(command, **args)


def text_of(result):
    return '\n'.join(b.text for b in result.content if getattr(b, 'type', '') == 'text')


class LastToolRecordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config_patch = patch('orchestrator.config.REPO_ROOT', self.root)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.cfg = RunConfig(agent='journal-test', save='test', model='test', engine='codex',
                             runtime='host', mode='explore', sim=False, sim_assist=False,
                             collision_image='off', cutscene_mode='agent', record=False, save_slot=3)
        self.cfg.min_frames_per_act = 1
        self.run = self.root/'first'
        self.run.mkdir()
        self.metrics = Metrics(self.run/'metrics.json', self.cfg.agent, 'test', 'high', 60)
        self.session = GameSession(self.cfg, self.run, self.metrics)
        self.addCleanup(self.session.close, release=False)
        self.bridge = JournalBridge()
        self.session.client = self.bridge
        self.session.last_obs = deepcopy(self.bridge.obs)
        self.server = build_server(self.session)
        self.path = self.session.workspace/'game/last_tool.json'

    def document(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    async def call(self, tool, arguments):
        return await self.server.call_tool(tool, arguments)

    async def handle(self, tool, arguments):
        return await self.server._handle_call_tool(Mock(), CallToolRequestParams(name=tool, arguments=arguments))

    async def act(self, **extra):
        return await self.call('act', {'inputs': ['1,R'], 'thought': '核对调用记录', **extra})

    async def test_act_then_observe_use_one_fixed_file_and_preserve_last_action(self):
        first = await self.act()
        self.assertFalse(first.is_error)
        acted = self.document()
        self.assertEqual(acted['version'], 1)
        self.assertEqual(acted['save'], 'test')
        self.assertEqual(acted['save_slot'], 3)
        action = acted['last_action']
        self.assertEqual(action['save_slot'], 3)
        self.assertEqual(action['tool'], 'act')
        self.assertEqual(action['status'], 'returned')
        self.assertTrue(action['handler_entered'])
        self.assertEqual(action['arguments']['inputs'], ['1,R'])
        self.assertEqual(action['before']['frame'], 12)
        self.assertEqual(action['after']['frame'], 13)
        self.assertEqual(action['detail']['result']['consumed'], 1)
        self.assertEqual(action['detail']['attempts'][0]['inputs'], ['1,R'])
        self.assertEqual(action['detail']['attempts'][0]['input_format'], 'celeste_ai')
        self.assertEqual(action['result']['is_error'], False)
        self.assertIn('执行了 1/1 帧', json.dumps(action['result'], ensure_ascii=False))
        await self.call('observe', {})
        observed = self.document()
        self.assertEqual(observed['run_id'], acted['run_id'])
        self.assertEqual(observed['last_call']['tool'], 'observe')
        self.assertGreater(observed['last_call']['sequence'], action['sequence'])
        self.assertEqual(observed['last_action'], action)
        self.assertEqual(len(self.bridge.steps), 1)
        self.assertEqual(list(self.path.parent.glob('last_tool*.json')), [self.path])

    async def test_plain_return_is_not_confused_with_successful_action(self):
        await self.act()
        previous_action = self.document()['last_action']
        result = await self.act(inputs=['200,R'])
        self.assertFalse(result.is_error)  # Existing ActError consumer returns a normal refusal text.
        self.assertIn('动作未执行', text_of(result))
        record = self.document()['last_call']
        self.assertEqual(record['status'], 'returned')
        self.assertTrue(record['handler_entered'])
        self.assertEqual(record['execution_status'], 'not_started')
        self.assertEqual(record['result']['is_error'], False)
        self.assertEqual(record['before']['frame'], record['after']['frame'])
        self.assertEqual(len(self.bridge.steps), 1)
        self.assertIn('动作未执行', json.dumps(record['result'], ensure_ascii=False))
        self.assertEqual(self.document()['last_action'], previous_action)

    async def test_server_gate_refusal_preserves_the_previous_action(self):
        await self.act()
        previous_action = self.document()['last_action']
        runner = CodexRunner(self.cfg, self.run, self.session, self.metrics, 'unused', game_server=self.server)
        runner.paused = True
        result = await self.act()
        self.assertFalse(result.is_error)
        self.assertIn('暂停', text_of(result))
        record = self.document()['last_call']
        self.assertEqual(record['status'], 'returned')
        self.assertEqual(record['execution_status'], 'not_started')
        self.assertEqual(self.document()['last_action'], previous_action)
        self.assertEqual(len(self.bridge.steps), 1)

    async def test_observe_that_finds_a_changed_frame_records_change_without_claiming_an_action(self):
        await self.act()
        action = self.document()['last_action']
        self.bridge.obs = observation(14)
        await self.call('observe', {})
        changed = self.document()['last_call']
        self.assertEqual(changed['tool'], 'observe')
        self.assertGreater(changed['sequence'], action['sequence'])
        self.assertEqual(changed['before']['frame'], 13)
        self.assertEqual(changed['after']['frame'], 14)
        self.assertEqual(self.document()['last_action'], action)
        self.assertEqual(len(self.bridge.steps), 1)

    async def test_mcp_handle_entry_records_validation_failure_without_dispatch(self):
        result = await self.handle('act', {'inputs': ['1,R']})
        self.assertTrue(result.is_error)
        record = self.document()['last_call']
        self.assertEqual(record['tool'], 'act')
        self.assertFalse(record['handler_entered'])
        self.assertEqual(record['status'], 'raised')
        self.assertEqual(record['execution_status'], 'not_started')
        self.assertTrue(record['result']['is_error'])
        self.assertTrue(record['error'])
        self.assertEqual(self.bridge.steps, [])

    async def test_unlocked_narrate_validation_failure_does_not_claim_handler_entry(self):
        await self.act()
        previous_action = self.document()['last_action']
        result = await self.handle('narrate', {})
        self.assertTrue(result.is_error)
        record = self.document()['last_call']
        self.assertEqual(record['tool'], 'narrate')
        self.assertEqual(record['status'], 'raised')
        self.assertFalse(record['handler_entered'])
        self.assertEqual(record['execution_status'], 'not_started')
        self.assertTrue(record['result']['is_error'])
        self.assertEqual(self.document()['last_action'], previous_action)
        self.assertEqual(len(self.bridge.steps), 1)

    async def test_running_record_is_on_disk_before_bridge_can_execute(self):
        captured = []
        self.bridge.inspect_record = lambda: captured.append(self.document())
        result = await self.act()
        self.assertFalse(result.is_error)
        self.assertEqual(len(captured), 1)
        running = captured[0]['last_call']
        self.assertEqual(running['status'], 'running')
        self.assertTrue(running['handler_entered'])
        self.assertEqual(running['tool'], 'act')
        self.assertEqual(running['before']['frame'], 12)
        self.assertEqual(self.document()['last_call']['id'], running['id'])
        self.assertEqual(self.document()['last_call']['status'], 'returned')

    async def test_partial_execution_exception_is_recorded_without_claiming_no_execution(self):
        self.bridge.fail_after_step = True
        result = await self.handle('act', {'inputs': ['1,R'], 'thought': '核对部分执行异常'})
        self.assertTrue(result.is_error)
        record = self.document()['last_call']
        self.assertEqual(record['status'], 'raised')
        self.assertTrue(record['handler_entered'])
        self.assertEqual(record['execution_status'], 'unconfirmed')
        self.assertTrue(record['result']['is_error'])
        self.assertIn(record['error'], text_of(result))
        self.assertEqual(len(self.bridge.steps), 1)
        self.assertEqual(self.bridge.obs['frame'], 13)
        self.assertEqual(self.document()['last_action']['id'], record['id'])

    async def test_recovery_observe_cannot_erase_an_unconfirmed_action_that_executed(self):
        self.bridge.fail_after_step = True
        result = await self.handle('act', {'inputs': ['1,R'], 'thought': '核对未送达动作结果'})
        self.assertTrue(result.is_error)
        failed_action = self.document()['last_action']
        self.assertEqual(failed_action['status'], 'raised')
        self.assertEqual(failed_action['execution_status'], 'unconfirmed')
        self.assertEqual(self.session.last_obs['frame'], 12)
        self.assertEqual(self.bridge.obs['frame'], 13)
        await self.call('observe', {})
        recovered = self.document()
        self.assertEqual(recovered['last_call']['tool'], 'observe')
        self.assertEqual(recovered['last_call']['after']['frame'], 13)
        self.assertEqual(recovered['last_action'], failed_action)
        self.assertEqual(len(self.bridge.steps), 1)

    async def test_act_error_after_step_cannot_be_reported_as_zero_execution(self):
        self.bridge.fail_after_step = ActError('执行后的输入回执丢失')
        result = await self.act()
        self.assertFalse(result.is_error)
        self.assertIn('执行范围未确认', text_of(result))
        self.assertNotIn('没有消耗任何游戏帧', text_of(result))
        self.assertNotIn('动作未执行', text_of(result))
        record = self.document()['last_call']
        self.assertEqual(record['status'], 'returned')
        self.assertTrue(record['handler_entered'])
        self.assertNotEqual(record['execution_status'], 'not_started')
        self.assertEqual(self.document()['last_action']['id'], record['id'])
        self.assertEqual(len(self.bridge.steps), 1)
        self.assertEqual(self.bridge.obs['frame'], 13)

    async def test_cancelled_call_keeps_execution_unconfirmed_while_bridge_thread_can_finish(self):
        self.bridge.entered, self.bridge.release = threading.Event(), threading.Event()
        task = asyncio.create_task(self.act())
        try:
            self.assertTrue(await asyncio.to_thread(self.bridge.entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            record = self.document()['last_call']
            self.assertEqual(record['status'], 'raised')
            self.assertTrue(record['handler_entered'])
            self.assertEqual(record['execution_status'], 'unconfirmed')
            self.assertTrue(record['result']['is_error'])
            self.bridge.release.set()
            async with asyncio.timeout(3):
                while (self.session.last_obs or {}).get('frame') != 13:
                    await asyncio.sleep(.005)
            self.assertEqual(len(self.bridge.steps), 1)
            final = self.document()['last_action']
            self.assertEqual(final['id'], record['id'])
            self.assertEqual(final['status'], 'raised')
            self.assertEqual(final['execution_status'], 'unconfirmed')
        finally:
            self.bridge.release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_old_unlocked_effort_finish_does_not_overwrite_new_action(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def capability_policy(level):
            entered.set()
            await release.wait()
        self.session.effort_policy = capability_policy
        slow = asyncio.create_task(self.call('set_effort', {'level': 'low', 'reason': '核对并发顺序'}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            effort = self.document()['last_call']
            self.assertEqual(effort['tool'], 'set_effort')
            await self.act()
            action = self.document()['last_call']
            self.assertGreater(action['sequence'], effort['sequence'])
            release.set()
            await slow
            final = self.document()
            self.assertEqual(final['last_call']['id'], action['id'])
            self.assertEqual(final['last_action']['id'], action['id'])
        finally:
            release.set()
            await asyncio.gather(slow, return_exceptions=True)

    async def test_waiting_observe_does_not_claim_started_before_action_lock_is_acquired(self):
        self.bridge.entered, self.bridge.release = threading.Event(), threading.Event()
        action = asyncio.create_task(self.act())
        observe = None
        try:
            self.assertTrue(await asyncio.to_thread(self.bridge.entered.wait, 3))
            running = self.document()['last_call']
            observe = asyncio.create_task(self.call('observe', {}))
            await asyncio.sleep(.03)
            self.assertEqual(self.document()['last_call']['id'], running['id'])
            self.assertEqual(self.document()['last_call']['status'], 'running')
            self.bridge.release.set()
            await action
            await observe
            final = self.document()
            self.assertEqual(final['last_call']['tool'], 'observe')
            self.assertEqual(final['last_action']['tool'], 'act')
            self.assertEqual(final['last_action']['status'], 'returned')
        finally:
            self.bridge.release.set()
            await asyncio.gather(*[task for task in (action, observe) if task is not None], return_exceptions=True)

    async def test_start_persistence_failure_prevents_dispatch_and_keeps_previous_record(self):
        await self.call('observe', {})
        original = self.path.read_bytes()
        replace = Path.replace
        def fail(source, target):
            if Path(target) == self.path:
                raise OSError('journal start unavailable')
            return replace(source, target)
        with patch.object(Path, 'replace', fail):
            result = await self.handle('act', {'inputs': ['1,R'], 'thought': '核对起始落盘失败'})
        self.assertTrue(result.is_error)
        self.assertEqual(self.bridge.steps, [])
        self.assertEqual(self.path.read_bytes(), original)
        await self.call('observe', {})
        self.assertIsNone(self.document()['last_action'])

    async def test_completion_persistence_failure_keeps_running_record_and_actual_result(self):
        replace = Path.replace
        def fail(source, target):
            if Path(target) == self.path:
                payload = json.loads(source.read_text(encoding='utf-8'))
                if payload['last_call']['status'] == 'returned':
                    raise OSError('journal completion unavailable')
            return replace(source, target)
        with patch.object(Path, 'replace', fail):
            result = await self.act()
        self.assertFalse(result.is_error)
        self.assertIn('执行了 1/1 帧', text_of(result))
        self.assertIn('记录', text_of(result))
        self.assertEqual(len(self.bridge.steps), 1)
        record = self.document()['last_call']
        self.assertEqual(record['tool'], 'act')
        self.assertEqual(record['status'], 'running')
        self.assertTrue(record['handler_entered'])

    async def test_new_run_preserves_same_save_action_with_its_original_run_identity(self):
        await self.act()
        old = self.document()
        run = self.root/'second'
        run.mkdir()
        metrics = Metrics(run/'metrics.json', self.cfg.agent, 'test', 'high', 60)
        session = GameSession(self.cfg, run, metrics)
        session.client = JournalBridge()
        session.last_obs = deepcopy(session.client.obs)
        try:
            server = build_server(session)
            await server.call_tool('observe', {})
            current = self.document()
            self.assertNotEqual(current['run_id'], old['run_id'])
            self.assertEqual(current['save'], 'test')
            self.assertEqual(current['save_slot'], 3)
            self.assertEqual(current['last_action'], old['last_action'])
            self.assertEqual(current['last_action']['run_id'], old['run_id'])
            self.assertEqual(current['last_call']['run_id'], current['run_id'])
            self.assertEqual(current['last_call']['save_slot'], 3)
            self.assertEqual(current['last_call']['tool'], 'observe')
        finally:
            session.close(False)

    async def test_same_save_name_in_a_different_slot_does_not_inherit_action(self):
        await self.act()
        run = self.root/'different-slot'
        run.mkdir()
        cfg = replace(self.cfg, save_slot=4)
        metrics = Metrics(run/'metrics.json', cfg.agent, 'test', 'high', 60)
        session = GameSession(cfg, run, metrics)
        session.client = JournalBridge()
        session.last_obs = deepcopy(session.client.obs)
        try:
            await build_server(session).call_tool('observe', {})
            current = self.document()
            self.assertEqual(current['save'], 'test')
            self.assertEqual(current['save_slot'], 4)
            self.assertEqual(current['last_call']['save_slot'], 4)
            self.assertIsNone(current['last_action'])
        finally:
            session.close(False)

    async def test_unknown_slot_in_either_run_does_not_claim_previous_action(self):
        for source_slot, destination_slot in ((3, None), (None, 3), (None, None)):
            with self.subTest(source_slot=source_slot, destination_slot=destination_slot):
                self.cfg.save_slot = source_slot
                await self.act()
                run = self.root/f'unknown-slot-{source_slot}-{destination_slot}'
                run.mkdir()
                cfg = replace(self.cfg, save_slot=destination_slot)
                metrics = Metrics(run/'metrics.json', cfg.agent, 'test', 'high', 60)
                session = GameSession(cfg, run, metrics)
                session.client = JournalBridge()
                session.last_obs = deepcopy(session.client.obs)
                try:
                    await build_server(session).call_tool('observe', {})
                    current = self.document()
                    self.assertEqual(current['save_slot'], destination_slot)
                    self.assertIsNone(current['last_action'])
                finally:
                    session.close(False)

    async def test_new_save_does_not_claim_another_saves_action(self):
        await self.act()
        old = self.document()
        run = self.root/'another-save'
        run.mkdir()
        cfg = replace(self.cfg, save='different-save')
        metrics = Metrics(run/'metrics.json', cfg.agent, 'test', 'high', 60)
        session = GameSession(cfg, run, metrics)
        session.client = JournalBridge()
        session.last_obs = deepcopy(session.client.obs)
        try:
            await build_server(session).call_tool('observe', {})
            current = self.document()
            self.assertNotEqual(current['run_id'], old['run_id'])
            self.assertEqual(current['save'], 'different-save')
            self.assertIsNone(current['last_action'])
        finally:
            session.close(False)

    async def test_journal_keeps_image_metadata_without_image_payload(self):
        self.cfg.collision_image = 'room'
        result = await self.call('observe', {})
        self.assertTrue(any(b.type == 'image' for b in result.content))
        record = self.document()['last_call']
        images = [b for b in record['result']['content'] if b['type'] == 'image']
        self.assertEqual(len(images), 1)
        serialized = json.dumps(images)
        self.assertIn('collision', serialized)
        self.assertNotIn('data:image', serialized)
        self.assertNotIn('iVBORw0KGgo', serialized)
        self.assertNotIn('data', images[0])

    async def test_codex_preflight_refusal_is_last_call_but_does_not_overwrite_last_action(self):
        await self.act()
        action = self.document()['last_action']
        runner = CodexRunner(self.cfg, self.run, self.session, self.metrics, 'unused', game_server=self.server)
        runner.session_id, runner.turn_id, runner.turn_active = 'fixture-thread', 'fixture-turn', True
        runner.paused = True
        runner.rpc.request = AsyncMock()
        response = await runner._request('item/tool/call', {'callId': 'paused-action', 'tool': 'celeste_act',
                                                          'arguments': {'inputs': ['1,R'], 'thought': '已暂停的动作'}})
        self.assertFalse(response['success'])
        record = self.document()['last_call']
        self.assertEqual(record['tool'], 'act')
        self.assertFalse(record['handler_entered'])
        self.assertEqual(record['execution_status'], 'not_started')
        self.assertTrue(record['result']['is_error'])
        self.assertIn('暂停', record['error'])
        self.assertEqual(self.document()['last_action'], action)
        self.assertEqual(len(self.bridge.steps), 1)


if __name__ == '__main__':
    unittest.main()
