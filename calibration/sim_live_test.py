"""在线推演设置：账本、并发调用及两个执行器的控制通道；不接游戏和 OBS。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.sim_assist import AssistRules, SimEnergy


class LiveSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent='test', save='test', model='test', runtime='host', sim_assist=True, mode='explore')
        self.metrics = Metrics(self.root/'metrics.json', 'test', 'test', 'high', 600)
        self.session = GameSession(self.cfg, self.root, self.metrics)
        self.calls = []
        self.session.client = SimpleNamespace(call=lambda *args, **kw: self.calls.append(args) or {'state': {'frame': 1}})
        (self.root/'run_state.json').write_text(json.dumps({'session_id': 'preserved', 'tuning': {}}), encoding='utf-8')

    def tearDown(self):
        self.session._frames_log.close()
        self.session._decisions_log.close()
        self.temp.cleanup()

    def test_rule_changes_preserve_spent_energy_history_and_recording(self):
        ident = self.session.sim_energy.reserve(8)
        self.session.sim_energy.finish(ident)
        self.session.sim_energy.reward(room='first')
        self.session.sim_energy.reward(moving_frames=10)
        before = json.loads(json.dumps(self.session.sim_energy.data))
        self.cfg.playback_speed = 5
        result = self.session.update_simulation({'rules': {'assist_cost_base': 7, 'assist_initial': 80}})
        self.assertEqual(result['balance'], before['balance'])
        self.assertEqual(self.session.sim_energy.data, before)
        self.assertEqual(self.calls, [])
        state = json.loads((self.root/'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['session_id'], 'preserved')
        self.assertEqual(state['tuning']['assist_cost_base'], 7)
        self.assertEqual(state['tuning']['playback_speed'], 5)
        self.assertEqual(self.session.assist_rules.cost_base, 7)
        restored = SimEnergy(self.root/'sim_energy.json', rules=self.session.assist_rules)
        self.assertEqual(restored.snapshot()['balance'], before['balance'])

    def test_balance_is_explicit_and_capacity_clamps_without_resetting_wallet(self):
        self.assertEqual(self.session.update_simulation({'balance': 0})['balance'], 0)
        self.assertEqual(self.session.update_simulation({'rules': {'assist_initial': 90}})['balance'], 0)
        self.assertEqual(self.session.update_simulation({'balance': 80})['balance'], 80)
        result = self.session.update_simulation({'rules': {'assist_capacity': 30, 'assist_initial': 20}})
        self.assertEqual(result['balance'], 30)
        self.assertEqual(result['capacity'], 30)

    def test_invalid_updates_are_atomic_and_finished_sessions_reject_changes(self):
        before = self.session.sim_energy.snapshot()
        state = (self.root/'run_state.json').read_bytes()
        for data in [{'balance': True}, {'balance': None}, {'balance': 101}, {'rules': {'assist_cost_frames': 0}},
                     {'rules': {'assist_capacity': 20}}, {'rules': {'assist_max_frames': 601}},
                     {'rules': {'other': 1}}, {'rules': []}, {'rules': {'assist_cost_base': 1.5}}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.session.update_simulation(data)
            self.assertEqual(self.session.sim_energy.snapshot(), before)
            self.assertEqual((self.root/'run_state.json').read_bytes(), state)
        self.session.done = True
        with self.assertRaises(ValueError):
            self.session.update_simulation({'balance': 1})

    def test_persistence_failure_rolls_back_wallet_and_active_rules(self):
        before = self.session.sim_energy.snapshot()
        with patch.object(Path, 'replace', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):
                self.session.update_simulation({'balance': 1, 'rules': {'assist_cost_base': 10}})
        self.assertEqual(self.session.sim_energy.snapshot(), before)
        self.assertEqual(self.cfg.assist_cost_base, AssistRules().cost_base)
        self.assertEqual(self.session.assist_rules.cost_base, AssistRules().cost_base)

    def test_run_state_write_failure_restores_wallet_and_config(self):
        before = self.session.sim_energy.snapshot()
        replace = Path.replace
        def fail_state(path, target):
            if path.name == 'run_state.tmp':
                raise OSError('cannot persist run settings')
            return replace(path, target)
        with patch.object(Path, 'replace', fail_state), self.assertRaises(OSError):
            self.session.update_simulation({'balance': 1, 'rules': {'assist_cost_base': 10}})
        self.assertEqual(self.session.sim_energy.snapshot(), before)
        self.assertEqual(SimEnergy(self.root/'sim_energy.json').snapshot()['balance'], before['balance'])
        self.assertEqual(self.cfg.assist_cost_base, AssistRules().cost_base)

    def test_http_rejects_old_controller_and_queues_only_to_requested_run(self):
        from overlay.server import start_in_thread
        from orchestrator.control import RunControl
        control = RunControl(self.root)
        server = start_in_thread(0, pinned=self.root)
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/control'
            def send(run):
                body = json.dumps({'run': str(run), 'kind': 'sim_update', 'text': '{"balance":25}'}).encode()
                return urllib.request.urlopen(urllib.request.Request(url, data=body, headers={'Content-Type':'application/json'}))
            control.heartbeat(sim_control=False)
            with self.assertRaises(urllib.error.HTTPError) as error:
                send(self.root)
            self.assertIn('在线推演设置', error.exception.read().decode())
            error.exception.close()
            self.assertEqual(control.pending(), [])
            control.heartbeat(sim_control=True)
            with self.assertRaises(urllib.error.HTTPError) as error:
                send(self.root/'wrong')
            error.exception.read()
            error.exception.close()
            self.assertEqual(control.pending(), [])
            with send(self.root) as response:
                ident = json.load(response)['id']
            self.assertEqual(control.pending()[0]['id'], ident)
        finally:
            server.shutdown()
            server.server_close()

    def test_inflight_prediction_finishes_on_old_rules_then_next_call_uses_new_rules(self):
        started, release = threading.Event(), threading.Event()
        def predict(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test worker timed out')
            return {'info': {'frame': 1}, 'results': [{'frames': [], 'stop': 'done'}]}
        with patch('orchestrator.sim_assist.run_predictions', side_effect=predict), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.session.simulate, {'candidates': [['60,R']]})
            self.assertTrue(started.wait(1))
            update = pool.submit(self.session.update_simulation, {'rules': {'assist_cost_base': 10, 'assist_cost_frames': 30}})
            try:
                self.assertFalse(update.done())
            finally:
                release.set()
            self.assertEqual(first.result()['cost'], 3)
            update.result()
            self.assertEqual(self.session.simulate({'candidates': [['60,R']]})['cost'], 12)

    def test_common_control_channel_updates_codex_and_claude_without_pause_or_stop(self):
        from orchestrator.codex_agent import CodexRunner
        from orchestrator.agent import ClaudeRunner
        for adapter in [CodexRunner, ClaudeRunner]:
            with self.subTest(adapter=adapter.__name__):
                directory = self.root/adapter.__name__
                directory.mkdir()
                runner = adapter(self.cfg, directory, self.session, self.metrics, 'unused')
                runner._check_goal = AsyncMock()
                ident = runner.control.enqueue('sim_update', json.dumps({'balance': 25, 'rules': {'assist_max_frames': 500}}))
                async def run():
                    task = asyncio.create_task(runner._controls())
                    try:
                        for _ in range(100):
                            row = next(a for a in runner.control.snapshot()['actions'] if a['id'] == ident)
                            if row['status'] in {'error', 'submitted'}:
                                return row
                            await asyncio.sleep(.01)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                row = asyncio.run(run())
                self.assertEqual(row['status'], 'submitted', row)
                self.assertFalse(runner.paused)
                self.assertFalse(runner.stop_requested)
                self.assertTrue(runner._sim_status()['sim_control'])
                self.assertEqual(runner._sim_status()['sim_settings']['energy']['balance'], 25)
                self.assertEqual(self.session.assist_rules.max_frames, 500)
                self.assertIn('500帧', runner.pending_messages[-1]['text'])
                self.assertEqual(self.calls, [])
                runner.close_events()


    def test_claude_turn_is_not_interrupted_by_sim_update_notice(self):
        """Claude 不能向进行中的一轮追加输入：推演设置生效的通知留到下一轮，不打断当前这一轮思考。"""
        from orchestrator.agent import ClaudeRunner
        directory = self.root / 'claude_turn'
        directory.mkdir()
        runner = ClaudeRunner(self.cfg, directory, self.session, self.metrics, 'unused')
        runner._check_goal = AsyncMock()
        runner._interrupt_user = AsyncMock()
        runner.turn_active, runner.turn_id = True, 'turn'
        ident = runner.control.enqueue('sim_update', json.dumps({'balance': 30}))

        async def run():
            runner._turn_done.clear()
            task = asyncio.create_task(runner._controls())
            try:
                for _ in range(100):
                    row = next(a for a in runner.control.snapshot()['actions'] if a['id'] == ident)
                    if row['status'] in {'error', 'submitted'}:
                        break
                    await asyncio.sleep(.01)
                await asyncio.sleep(.3)                 # 再过几轮控制循环
                return row
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        row = asyncio.run(run())
        self.assertEqual(row['status'], 'submitted', row)
        runner._interrupt_user.assert_not_awaited()
        self.assertTrue(any('推演设置' in m['text'] for m in runner.pending_messages))
        runner.close_events()

if __name__ == '__main__':
    unittest.main()
