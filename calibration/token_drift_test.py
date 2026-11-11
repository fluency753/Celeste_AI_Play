"""Usage accounting and real game/tool consumers with only the bridge replaced."""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from calibration.stiffness_test import StepClient
from calibration.story_wait_test import BridgeWire, observation
from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.launch_options import build, config_for
from orchestrator.metrics import Metrics
from orchestrator.output_usage import ClaudeUsage, partial_usage_event
from orchestrator.server import build_server
from orchestrator.token_drift import TokenDrift
from orchestrator.tool_gate import decide_tool
from tools.bridge_client import BridgeClient


def partial(kind, **fields):
    return {'type': 'stream_event', 'event': {'type': kind, **fields}}


def level_obs(room='a', dead=False):
    obs = observation(controllable=not dead, cutscene=False, sid='Celeste/4-GoldenRidge', room=room)
    obs['player']['dead'] = dead
    return obs


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.m = Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        self.seen = []

    def tracker(self):
        return ClaudeUsage(self.m, self.root/'output_usage.json', 'session', lambda *v: self.seen.append(v))

    def stream(self, tracker, output, ident='r'):
        tracker.partial(partial('message_start', message={'id': ident, 'usage': {'output_tokens': 1}}))
        tracker.partial(partial('message_delta', usage={'output_tokens': output}))

    def test_response_replay_result_reconciliation_and_next_turn(self):
        t = self.tracker()
        self.stream(t, 700)
        self.stream(t, 700)
        self.stream(t, 300, 'second')
        t.result({'uuid': 'result', 'usage': {'output_tokens': 1200,
                 'output_tokens_details': {'thinking_tokens': 900}}})
        t.result({'uuid': 'result', 'usage': {'output_tokens': 1200}})
        self.assertEqual((self.m.state.output_tokens, self.m.state.thinking_tokens), (1200, 900))
        self.assertEqual(sum(v[1] for v in self.seen), 1200)  # reasoning is a subset
        t.begin_turn()
        self.stream(t, 100, 'third')
        t.result({'uuid': 'next', 'usage': {'output_tokens': 100}})
        self.assertEqual(self.m.state.output_tokens, 1300)

    def test_real_reconstruction_unfinished_turn_and_resumed_directory(self):
        t = self.tracker()
        self.stream(t, 1000)
        t = self.tracker()
        t.begin_turn()
        self.stream(t, 1000)
        t.result({'uuid': 'result', 'usage': {'output_tokens': 1200}})
        self.assertEqual(self.m.state.output_tokens, 1200)
        next_dir = self.root/'resumed'
        next_dir.mkdir()
        self.m.state.resumed_from = str(self.root)
        t = ClaudeUsage(self.m, next_dir/'output_usage.json', 'session', lambda *v: self.seen.append(v))
        t.result({'uuid': 'result', 'usage': {'output_tokens': 1200}})
        self.assertEqual(self.m.state.output_tokens, 1200)

    def test_new_cli_send_does_not_subtract_an_unfinished_previous_result(self):
        t = self.tracker()
        self.stream(t, 1000, 'old-unfinished')
        t.begin_turn()  # new user message after CLI reconnect
        self.stream(t, 1000, 'new-response')
        t.result({'uuid': 'new-result', 'usage': {'output_tokens': 1200}})
        self.assertEqual(self.m.state.output_tokens, 2200)
        # A reconstructed receiver can reconcile a result without replayed deltas.
        t = self.tracker()
        t.result({'uuid': 'new-result', 'usage': {'output_tokens': 1200}})
        self.assertEqual(self.m.state.output_tokens, 2200)

    def test_usage_persistence_failure_does_not_submit_movement(self):
        t = self.tracker()
        with patch('orchestrator.output_usage.write_json', side_effect=OSError('fixture disk')):
            with self.assertRaises(OSError):
                self.stream(t, 1000)
        self.assertEqual(self.seen, [])

    def test_replayed_terminal_delta_after_result_reconciliation_is_closed(self):
        t = self.tracker()
        self.stream(t, 1000)
        t.result({'uuid': 'result', 'usage': {'output_tokens': 1200}})
        t = self.tracker()
        t.begin_turn()
        self.stream(t, 1200)
        t.result({'uuid': 'result', 'usage': {'output_tokens': 1200}})
        self.assertEqual(self.m.state.output_tokens, 1200)

    def test_partial_allowlist_missing_counts_and_placeholder(self):
        t = self.tracker()
        t.partial(partial('message_start', message={'id': 'r', 'usage': {'output_tokens': 1}}))
        t.partial(partial('message_delta', usage={}))
        t.result({'usage': {}})
        self.assertEqual(self.m.state.output_tokens, 0)
        self.assertIsNone(partial_usage_event(partial('content_block_delta', delta={'thinking': 'PRIVATE'})))
        safe = partial_usage_event(partial('message_delta', usage={'output_tokens': 9, 'private': 'PRIVATE'},
                                          delta={'text': 'PRIVATE'}))
        self.assertNotIn('PRIVATE', json.dumps(safe))

    def test_ledger_remainder_no_replay_partial_and_unknown(self):
        p = self.root/'token_drift.json'
        d = TokenDrift(p, self.m, 500)
        d.receive(700, 700, 'source', True, (1,))
        d.receive(700, 700, 'source', True, (1,))
        self.assertEqual((d.pending, d.state['remainder_tokens']), (1, 200))
        self.assertEqual(d.begin((1,), {}), 1)
        d.commit({'consumed': 1, 'stop': 'done'})
        d.receive(1300, 600, 'source', True, (1,))
        d.discard('pause')
        resumed = TokenDrift(p, self.m, 500)
        self.assertEqual((resumed.pending, resumed.state['remainder_tokens']), (0, 300))
        resumed.receive(1500, 200, 'source', True, (1,))
        self.assertEqual(resumed.pending, 0)  # first post-resume span is unknown
        resumed.receive(1700, 200, 'source', True, (1,))
        self.assertEqual(resumed.begin((1,), {}), 1)
        unknown = TokenDrift(p, self.m, 500)
        self.assertTrue(unknown.unknown)
        unknown.receive(2000, 500, 'source', True, (1,))
        self.assertEqual(unknown.pending, 0)
        self.assertIsNone(unknown.begin((1,), {}))

    def test_history_source_reset_missing_latest_and_excluded_usage(self):
        d = TokenDrift(self.root/'token_drift.json', self.m, 500)
        d.receive(100000, None, 'old', True, (1,))
        self.assertEqual(d.pending, 0)
        d.receive(100500, 500, 'old', False, (1,))
        self.assertEqual(d.state['excluded_tokens'], 500)
        d.receive(100, 100, 'old', True, (1,))  # monotonic cursor survives reset
        self.assertEqual(d.pending, 0)
        d.receive(900000, 1000, 'new', True, (1,))
        self.assertEqual(d.pending, 0)  # first positive sample after the excluded window
        d.receive(901000, 1000, 'new', True, (1,))
        self.assertEqual(d.pending, 2)
        d.begin((1,), {})
        d.commit({'consumed': 1, 'stop': 'room'})
        self.assertEqual((d.pending, d.state['consumed_frames'], d.state['dropped_frames']), (0, 1, 1))

    def test_configuration_roundtrip_and_invalid_combinations(self):
        base = {'agent': 'fixture', 'save': {'name': 'fixture'}, 'mode': 'explore',
                'runtime': 'host', 'token_drift': True, 'tune': {'tokens_per_frame': 250}}
        args = build(base)
        self.assertIn('--token-drift', args)
        cfg = config_for(base)
        self.assertEqual((cfg.token_drift, cfg.tokens_per_frame), (True, 250))
        for change in ({'stiffness': True}, {'mode': 'speedrun'}, {'tune': {'tokens_per_frame': 0}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                build({**base, **change})

    def test_samples_inside_excluded_window_keep_resume_boundary_unknown(self):
        d = TokenDrift(self.root/'token_drift.json', self.m, 500)
        d.receive(100, 100, 'source', True, (1,))
        d.exclude_window('pause')
        d.receive(600, 500, 'source', False, (1,))
        d.receive(1100, 500, 'source', False, (1,))
        d.receive(1600, 500, 'source', True, (1,))
        self.assertEqual(d.pending, 0)
        d.receive(2100, 500, 'source', True, (1,))
        self.assertEqual((d.pending, d.state['remainder_tokens']), (1, 100))


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine='claude',
                             mode='explore', knowledge_profile='explore', runtime='host',
                             token_drift=True, tokens_per_frame=500, thought_lead_s=0,
                             min_frames_per_act=1, sim=False, cutscene_mode='agent')
        self.m = Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        self.s = GameSession(self.cfg, self.root, self.m)
        self.s.client = StepClient([], make=level_obs)
        self.s.last_obs = level_obs()
        self.s._remember_held_input({'inputs': ['10,R,G']}, {'result': {'consumed': 10}})
        self.server = build_server(self.s)
        self.r = ClaudeRunner(self.cfg, self.root, self.s, self.m, 'fixture', session_id='fixture')
        self.r.turn_active = True
        self.r._begin_model_turn(runtime='claude')
        self.r._start_token_worker()

    async def asyncTearDown(self):
        await self.r._finish_token_worker()
        self.r.close_events()
        self.s.close()
        self.temp.cleanup()

    async def settle(self):
        self.r._token_event.set()
        for _ in range(150):
            if not self.r.token_drift.pending and not self.s.lock._is_owned():
                async with self.r.action_lock:
                    return
            await asyncio.sleep(.01)
        self.fail('token worker did not settle')

    async def tool(self, name, **args):
        result = await self.server.call_tool(name, args)
        return '\n'.join(getattr(item, 'text', '') for item in result.content)

    def wire_bridge(self):
        fixture = self.s.client
        with patch('tools.bridge_client.socket.create_connection', return_value=BridgeWire(fixture)):
            self.s.client = BridgeClient(port=1)
        self.s.client.after_step = self.s._after_step
        return fixture

    async def use_codex(self):
        await self.r._finish_token_worker()
        self.r.close_events()
        self.cfg.engine = 'codex'
        self.s._token_cancel.clear()
        self.r = CodexRunner(self.cfg, self.root, self.s, self.m, None,
                             game_server=self.server, session_id='fixture')
        self.r.turn_active, self.r.turn_id = True, 'turn-1'
        self.r._begin_model_turn(runtime='codex')
        self.r._start_token_worker()
        self.r._record_output_usage(1, 1)  # Close the saved ledger's resume gap.

    async def test_codex_background_death_notifies_active_turn_without_holding_game_locks(self):
        bridge = self.wire_bridge()
        await self.use_codex()
        bridge.results = [('death', 1, {**level_obs(dead=True), 'frame': 101}, [])]
        entered, release = asyncio.Event(), asyncio.Event()

        async def steer(method, params, *, timeout):
            entered.set()
            await release.wait()
            return {'turnId': 'turn-1'}

        with patch.object(self.r.rpc, 'request', side_effect=steer) as request:
            self.r._record_output_usage(1501, 1500)
            try:
                await asyncio.wait_for(entered.wait(), 3)
                method, params = request.call_args.args
                self.assertEqual((method, params['expectedTurnId']), ('turn/steer', 'turn-1'))
                self.assertIn('角色死亡（帧 101）', params['input'][0]['text'])
                self.assertEqual(self.m.state.status, 'thinking')
                response = await asyncio.wait_for(self.r._request('item/tool/call', {
                    'threadId': 'fixture', 'turnId': 'turn-1', 'tool': 'celeste_act',
                    'arguments': {'inputs': ['1,L'], 'thought': '旧计划'}}), 3)
                self.assertFalse(response['success'])
                self.assertIn('角色死亡', response['contentItems'][0]['text'])
                self.assertEqual(len(bridge.steps), 1)
                self.assertEqual(self.r.token_drift.state['dropped_frames'], 2)
                self.assertFalse(self.r.paused)
                self.assertTrue(self.r.turn_active)
            finally:
                release.set()
                await asyncio.gather(*self.r._receipt_tasks)
        self.assertIn('角色死亡', (self.root/'public_events.jsonl').read_text(encoding='utf-8'))

    async def test_codex_steer_failure_preserves_boundary_for_tools_and_next_turn(self):
        await self.use_codex()
        obs = observation(node='dialogue', controllable=False)
        self.s.client.results = [('story_input', 1, {**obs, 'frame': 101}, [])]
        with patch.object(self.r.rpc, 'request', new_callable=AsyncMock,
                          side_effect=RuntimeError('turn closed')) as request:
            self.r._record_output_usage(1001, 1000)
            await self.settle()
            await asyncio.gather(*self.r._receipt_tasks)
        request.assert_awaited_once()
        self.assertFalse(self.r.paused)
        self.assertFalse(self.r.turn_done.is_set())
        self.assertIn('出现剧情输入', self.r._turn_message('继续', []))
        self.assertIn('出现剧情输入', await self.tool('observe'))
        self.assertNotIn('出现剧情输入', self.r._turn_message('继续', []))

    async def test_codex_does_not_steer_a_notice_into_a_different_turn(self):
        await self.use_codex()
        self.s.client.results = [('room', 1, {**level_obs(room='b'), 'frame': 101}, [])]
        with patch.object(self.r.rpc, 'request', new_callable=AsyncMock) as request:
            async with self.r.action_lock:
                self.r._record_output_usage(1001, 1000)
                await self.r._flush_token_drift_locked()
                self.r.turn_id = 'turn-2'
            await asyncio.gather(*self.r._receipt_tasks)
        request.assert_not_awaited()
        self.assertIn('新房间 b', self.r._turn_message('继续', []))

    async def test_claude_next_turn_receives_chapter_completion_notice(self):
        obs = level_obs()
        obs['level']['completed'] = True
        self.s.client.results = [('complete', 1, {**obs, 'frame': 101}, [])]
        self.r._record_output_usage(1000, 1000)
        await self.settle()
        self.assertIn('游戏登记章节通关', self.r._turn_message('继续', []))
        self.assertIn('游戏登记章节通关', (await decide_tool('Bash', session=self.s))['context'])
        self.assertIn('游戏登记章节通关', (await decide_tool('mcp__celeste__narrate', session=self.s))['context'])
        self.assertIn('游戏登记章节通关', await self.tool('act', inputs=['1,L'], thought='旧动作'))

    async def test_real_step_keeps_held_keys_and_accounts_frames(self):
        self.r._record_output_usage(1200, 1200)
        await self.settle()
        self.assertEqual(self.s.client.steps[0]['inputs'], ['2,R,G'])
        self.assertEqual((self.m.state.token_frames, self.m.state.token_s), (2, 2/60))
        self.assertEqual(self.m.state.frames_played, 0)
        self.assertEqual(self.r.token_drift.state['consumed_frames'], 2)

    async def test_token_frames_do_not_restart_three_step_stop_confirmation(self):
        bridge = self.wire_bridge()
        with patch('orchestrator.stop_categories.LOCAL_CATEGORIES', self.root / 'local.stop_categories.json'):
            reason = '两条路线均未抵达高台，请提示起跳点'
            first = json.loads((await self.tool('stop_run', reason='hint：' + reason)).splitlines()[0])
            bridge.results = [('done', 3, {**level_obs(), 'frame': 103}, []),
                              ('done', 2, {**level_obs(), 'frame': 105}, [])]
            self.r._record_output_usage(1500, 1500)
            await self.settle()
            self.assertEqual((self.s.last_obs['frame'], self.m.state.token_frames), (103, 3))
            second = json.loads((await self.tool('stop_run', reason=first['confirmation_prefix'] + reason)).splitlines()[0])
            self.assertEqual((second['step'], second['required_steps'], self.r.paused), (2, 3, False))
            self.r._record_output_usage(2500, 1000)
            await self.settle()
            self.assertEqual((self.s.last_obs['frame'], self.m.state.token_frames), (105, 5))
            final = json.loads((await self.tool('stop_run', reason=second['confirmation_prefix'] + reason)).splitlines()[0])
            self.assertEqual((final['status'], final['paused'], self.r.paused), ('reported', True, True))

    async def test_ordinary_token_motion_does_not_make_a_report_new(self):
        bridge = self.wire_bridge()
        # Exercise a new attempt in the same session without holding a user pause.
        self.s.request_pause = lambda reason: False
        with patch('orchestrator.stop_categories.LOCAL_CATEGORIES', self.root / 'local.stop_categories.json'):
            reason = '需要用户核验工具状态'
            first = json.loads((await self.tool('stop_run', reason='state_verification：' + reason)).splitlines()[0])
            self.assertEqual(json.loads((await self.tool('stop_run', reason=first['confirmation_prefix'] + reason))
                                        .splitlines()[0])['status'], 'reported')
            moving = {**level_obs(), 'frame': 103}
            moving['player']['state'] = 'Dash'
            bridge.results = [('done', 3, moving, [])]
            self.r._record_output_usage(1500, 1500)
            await self.settle()
            self.assertEqual((self.s.last_obs['frame'], self.m.state.token_frames), (103, 3))
            repeated = json.loads((await self.tool('stop_run', reason='state_verification：' + reason)).splitlines()[0])
            self.assertEqual(repeated['status'], 'duplicate')

    async def test_token_room_transition_invalidates_stop_confirmation(self):
        bridge = self.wire_bridge()
        with patch('orchestrator.stop_categories.LOCAL_CATEGORIES', self.root / 'local.stop_categories.json'):
            reason = '需要用户核验工具状态'
            first = json.loads((await self.tool('stop_run', reason='state_verification：' + reason)).splitlines()[0])
            bridge.results = [('room', 1, {**level_obs(room='b'), 'frame': 101}, [])]
            self.r._record_output_usage(500, 500)
            await self.settle()
            self.assertEqual((self.s.last_obs['frame'], self.m.state.token_frames), (101, 1))
            changed = json.loads((await self.tool('stop_run', reason=first['confirmation_prefix'] + reason)).splitlines()[0])
            self.assertEqual((changed['status'], changed['step'], self.r.paused), ('confirmation_required', 1, False))
            self.assertNotEqual(first['confirmation_prefix'], changed['confirmation_prefix'])

    async def test_actual_reader_never_persists_partial_content(self):
        from types import SimpleNamespace
        reader = asyncio.StreamReader()
        events = [partial('message_start', message={'id': 'r', 'model': 'fixture', 'usage': {'output_tokens': 1}}),
                  partial('content_block_delta', delta={'thinking': 'PRIVATE_PARTIAL_CANARY'}),
                  partial('message_delta', usage={'output_tokens': 1200}, delta={'text': 'PRIVATE_DELTA_CANARY'})]
        for event in events:
            reader.feed_data((json.dumps(event)+'\n').encode())
        reader.feed_eof()
        self.r.proc = SimpleNamespace(stdout=reader)
        await self.r._read_stdout()
        await self.settle()
        body = (self.root/'agent_events.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('CANARY', body)
        self.assertEqual(self.m.state.output_tokens, 1200)
        self.assertEqual(self.r.token_drift.state['consumed_frames'], 2)

    async def test_stream_model_mismatch_blocks_motion_before_complete_assistant(self):
        self.r._expected_model = 'expected-model'
        self.r._handle(partial('message_start', message={'id': 'r', 'model': 'different-model'}))
        self.r._handle(partial('message_delta', usage={'output_tokens': 1500}))
        await self.settle()
        self.assertIsNotNone(self.r.model_mismatch)
        self.assertEqual(self.s.client.steps, [])

    async def test_room_interrupt_blocks_further_usage_and_old_action_until_observe(self):
        self.s.client.results = [('room', 1, level_obs(room='b'), [])]
        self.r._record_output_usage(1500, 1500)
        await self.settle()
        self.r._record_output_usage(2500, 1000)
        await self.settle()
        self.assertEqual(len(self.s.client.steps), 1)
        message = await self.tool('act', inputs=['1,L'], thought='重新尝试')
        self.assertIn('先默认 observe', message)
        self.assertIn('房间切换（帧 100，新房间 b）', message)
        self.assertEqual(len(self.s.client.steps), 1)
        await self.tool('observe')
        self.assertFalse(self.s.token_observation_required)
        await self.tool('act', inputs=['1,L'], thought='根据新房间移动')
        self.assertEqual(self.s.client.steps[-1]['inputs'], ['1,L'])

    async def test_queued_credit_discarded_when_paused_compacting_or_reloading(self):
        for block in (lambda active: setattr(self.r, 'paused', active), self.m.set_compacting):
            async with self.r.action_lock:
                self.r._record_output_usage(self.m.state.output_tokens + 1000, 1000)
                block(True)
            await self.settle()
            block(False)
        self.s.reload_observation_required = True
        self.r._record_output_usage(4000, 1000)
        await self.settle()
        self.assertEqual(self.s.client.steps, [])

    async def test_unknown_ack_pauses_without_replay_and_worker_survives(self):
        original = self.s.client.call
        def call(cmd, **args):
            if cmd == 'step':
                raise ConnectionError('fixture lost ACK')
            return original(cmd, **args)
        self.s.client.call = call
        self.r._record_output_usage(500, 500)
        await self.settle()
        self.assertTrue(self.r.paused)
        self.assertTrue(self.r.token_drift.unknown)
        self.assertFalse(self.r._token_task.done())
        self.r.paused = False
        self.r._record_output_usage(1000, 500)
        await self.settle()
        self.assertEqual(self.s.client.steps, [])
        refusal = await self.tool('act', inputs=['1,R'], thought='不能越过未知批次')
        self.assertIn('执行范围未确认', refusal)
        self.assertEqual(self.s.client.steps, [])
        self.r.paused = True
        ident = self.r.control.enqueue('resume')
        controls = asyncio.create_task(self.r._controls())
        try:
            for _ in range(100):
                row = next(a for a in self.r.control.snapshot()['actions'] if a['id'] == ident)
                if row['status'] == 'error':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(row['status'], 'error')
            self.assertIn('执行范围未确认', row['detail'])
            self.assertTrue(self.r.paused)
        finally:
            controls.cancel()
            await asyncio.gather(controls, return_exceptions=True)

    async def test_pause_compaction_and_network_without_usage_do_not_repay_gap(self):
        total = 100
        self.r._record_output_usage(total, 100)
        for boundary in ('pause', 'compaction', 'network'):
            if boundary == 'pause':
                self.m.pause()
                self.m.resume(quota=False)
            elif boundary == 'compaction':
                self.r._public_compaction('fixture')
                self.r._public_compaction('fixture', completed=True)
            else:
                self.r._net_trouble('fixture', 'fixture network retry', give_up=False)
                self.r._net_ok()
            total += 1000
            self.r._record_output_usage(total, 1000)
            await self.settle()
            self.assertEqual(self.s.client.steps, [], boundary)
        self.r._record_output_usage(total+500, 500)
        await self.settle()
        self.assertEqual(len(self.s.client.steps), 1)
        self.assertEqual(self.r.token_drift.state['remainder_tokens'], 100)

    async def test_shutdown_waits_for_inflight_thread(self):
        entered, released = threading.Event(), threading.Event()
        original = self.s.client.call
        def call(cmd, **args):
            if cmd == 'step':
                entered.set()
                if not released.wait(3):
                    raise TimeoutError('fixture release missing')
            return original(cmd, **args)
        self.s.client.call = call
        self.r._record_output_usage(500, 500)
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        self.assertTrue(entered.is_set())
        finishing = asyncio.create_task(self.r._finish_token_worker())
        await asyncio.sleep(.03)
        self.assertFalse(finishing.done())
        released.set()
        await asyncio.wait_for(finishing, 3)
        self.assertEqual(self.r.token_drift.state['consumed_frames'], 1)

    async def test_distinct_runner_and_tool_locks_preserve_queued_action_epoch(self):
        # Codex owns a separate outer action lock; Claude shares the MCP lock.
        self.r.action_lock = asyncio.Lock()
        self.s.client.results = [('room', 1, level_obs(room='b'), [])]
        async with self.r.action_lock:
            async with self.s.tool_lock:
                old_action = asyncio.create_task(self.tool('act', inputs=['1,L'], thought='旧房间计划'))
                await asyncio.sleep(0)
                self.r._record_output_usage(1000, 1000)
                # Match the production lock order while draining this boundary.
                await self.r._flush_token_drift_locked()
                self.s.token_observation_required = False  # another successful overview wins the race
        message = await old_action
        self.assertIn('旧动作未执行', message)
        self.assertEqual(len(self.s.client.steps), 1)


if __name__ == '__main__':
    unittest.main()
