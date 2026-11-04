"""Host-owned simulation, with only the external Bridge/worker replaced."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.planning_budget import PlanningBudget
from orchestrator.server import build_server
from orchestrator.session_controls import SessionControls


class PresetSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_root = patch('orchestrator.config.REPO_ROOT', self.root)
        self.config_root.start()
        self.addCleanup(self.config_root.stop)
        self.cfg = RunConfig(agent='test', save='test', model='stub', sim=True, pause_limit=True)
        self.cfg.sim_dir.mkdir(parents=True)
        (self.cfg.sim_dir / 'celeste-sim').write_bytes(b'fixture; never executed')
        self.run = self.root / 'run'
        self.run.mkdir()
        self.metrics = Metrics(self.run / 'metrics.json', 'test', 'stub', 'high', 100)
        self.session = GameSession(self.cfg, self.run, self.metrics)
        self.addCleanup(self.session._frames_log.close)
        self.addCleanup(self.session._decisions_log.close)
        self.budget = self.session.planning_budget = PlanningBudget(
            self.metrics, self.run / 'planning_budget.json', self.session.workspace / 'game/rounds.json',
            rounds_per_room=1)
        self.budget.on_room('map:0', 'one', [0, 0, 320, 180])
        self.controller = SessionControls()
        self.controller.cfg, self.controller.session = self.cfg, self.session
        self.controller.paused = self.controller.chat_only = self.controller.prepare_only = False
        self.controller.goal_finishing = self.controller.stop_requested = False
        self.session.control_guard = self.controller._game_guard
        self.state_calls = []
        self.session.client = SimpleNamespace(call=lambda *a, **kw: self.state_calls.append(a) or {'state': {'frame': 10}})
        self.result = {'info': {'frame': 10}, 'results': [{'stop': 'done', 'frames': [[11, 1, 2]]}]}

    def test_mcp_batch_spends_only_simulation_and_replays_same_request(self):
        server = build_server(self.session)
        with patch('orchestrator.sim_assist.run_predictions', return_value=self.result) as worker:
            first = asyncio.run(server.call_tool('simulate', {'candidates': [['1,R']], 'request_id': 'one'}))
            self.assertFalse(first.is_error)
            result = json.loads(first.content[0].text)
            self.assertEqual(result['planning']['left'], 0)
            self.assertEqual(result['cost'], 1)
            self.assertTrue(result['trajectory_file'].startswith('/workspace/game/simulation/'))
            again = asyncio.run(server.call_tool('simulate', {'candidates': [['1,R']], 'request_id': 'one'}))
            self.assertFalse(again.is_error)
            self.assertEqual(json.loads(again.content[0].text)['receipt'], 'one')
            worker.assert_called_once()
            self.assertEqual(len(self.state_calls), 1)
            self.assertEqual(self.metrics.state.sim_calls, 1)
            self.assertEqual(self.metrics.state.sim_rollouts, 1)
            with self.assertRaisesRegex(ValueError, '额度'):
                self.session.simulate({'candidates': [['1,L']], 'request_id': 'exhausted'})
        from orchestrator.tool_gate import decide_tool
        decision = asyncio.run(decide_tool('Bash'))
        self.assertEqual(decision['decision'], 'allow')

    def test_next_run_without_preset_capability_removes_old_balance_projection(self):
        path = self.session.workspace / 'game/rounds.json'
        self.assertTrue(path.exists())
        self.cfg.sim = self.cfg.pause_limit = False
        following = self.root / 'following'
        following.mkdir()
        session = GameSession(self.cfg, following, self.metrics)
        self.addCleanup(session._frames_log.close)
        self.addCleanup(session._decisions_log.close)
        self.assertFalse(path.exists())

    def test_each_running_phase_condition_blocks_before_capture_and_reservation(self):
        for name in ('paused', 'chat_only', 'prepare_only', 'goal_finishing', 'stop_requested', 'model_waiting'):
            with self.subTest(name=name), patch('orchestrator.sim_assist.run_predictions') as worker:
                setattr(self.controller, name, True)
                with self.assertRaises(ValueError):
                    self.session.simulate({'candidates': [['1,R']], 'request_id': 'test'})
                setattr(self.controller, name, False)
                worker.assert_not_called()
                self.assertEqual(self.budget.bank, 1)
                self.assertEqual(self.state_calls, [])
        for name in ('pause_limit', 'sim'):
            setattr(self.cfg, name, False)
            with self.assertRaisesRegex(ValueError, '未开启'):
                self.session.simulate({'candidates': [['1,R']], 'request_id': 'test'})
            setattr(self.cfg, name, True)
        (self.cfg.sim_dir / 'celeste-sim').unlink()
        with self.assertRaisesRegex(ValueError, '未配置'):
            self.session.simulate({'candidates': [['1,R']], 'request_id': 'test'})
        self.assertEqual(self.budget.bank, 1)
        self.assertEqual(self.state_calls, [])

    def test_request_file_is_data_and_rejects_escape_links_and_excess(self):
        workspace = self.session.workspace
        (workspace / 'request.json').write_text(json.dumps({'candidates': [['1,R']]}), encoding='utf-8')
        with patch('orchestrator.sim_assist.run_predictions', return_value=self.result):
            result = self.session.simulate({'request_file': '/workspace/request.json', 'request_id': 'file'})
        self.assertEqual(result['receipt'], 'file')
        for name in ('../outside.json', '/elsewhere/request.json', 'C:/data.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.session.simulate({'request_file': name, 'request_id': 'path'})
        with self.assertRaises(ValueError):
            self.session.simulate({'candidates': [['6001,R']], 'request_id': 'oversize'})
        try:
            (workspace / 'link.json').symlink_to(workspace / 'request.json')
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, '链接'):
            self.session.simulate({'request_file': 'link.json', 'request_id': 'link'})

    def test_missing_id_is_rejected_before_execution_even_when_retried(self):
        with patch('orchestrator.sim_assist.run_predictions') as worker:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, '显式提供 request_id'):
                    self.session.simulate({'candidates': [['1,R']]})
        self.assertEqual(self.budget.bank, 1)
        self.assertEqual(self.state_calls, [])
        worker.assert_not_called()

    def test_reservation_failure_never_submits_worker(self):
        with patch('orchestrator.planning_budget._atomic_json', side_effect=OSError('disk')), \
             patch('orchestrator.sim_assist.run_predictions') as worker:
            with self.assertRaisesRegex(ValueError, '未提交'):
                self.session.simulate({'candidates': [['1,R']], 'request_id': 'test'})
        worker.assert_not_called()
        self.assertEqual(self.budget.bank, 1)

    def test_capture_failure_refunds_but_worker_timeout_does_not(self):
        with patch.object(self.session.client, 'call', side_effect=OSError('Bridge')), \
             patch('orchestrator.sim_assist.run_predictions') as worker:
            with self.assertRaises(ValueError):
                self.session.simulate({'candidates': [['1,R']], 'request_id': 'capture'})
        worker.assert_not_called()
        self.assertEqual(self.budget.bank, 1)
        with patch('orchestrator.sim_assist.run_predictions', side_effect=TimeoutError('worker')) as worker:
            with self.assertRaisesRegex(RuntimeError, '不要重放'):
                self.session.simulate({'candidates': [['1,R']], 'request_id': 'timeout'})
            with self.assertRaisesRegex(ValueError, '不会自动重跑'):
                self.session.simulate({'candidates': [['1,R']], 'request_id': 'timeout'})
        worker.assert_called_once()
        self.assertEqual(self.budget.bank, 0)

    def test_private_directory_failure_before_submission_refunds(self):
        (self.run / 'sim_private').write_text('directory blocked', encoding='utf-8')
        with patch('orchestrator.sim_assist.run_predictions') as worker:
            with self.assertRaisesRegex(ValueError, "尚未提交"):
                self.session.simulate({'candidates': [['1,R']], 'request_id': 'mkdir'})
        self.assertEqual(self.budget.bank, 1)
        self.assertEqual(self.budget.receipt('mkdir', self.budget._data['receipts']['mkdir']['digest'])['status'], 'refunded')
        worker.assert_not_called()

    def test_submitted_failure_and_receipt_disk_failure_never_claim_zero_execution(self):
        server = build_server(self.session)
        with patch('orchestrator.sim_assist.run_predictions', side_effect=TimeoutError('worker')) as worker, \
             patch.object(self.budget, 'finish', side_effect=OSError('receipt disk')):
            response = asyncio.run(server.call_tool('simulate', {
                'candidates': [['1,R']], 'request_id': 'submitted'}))
        worker.assert_called_once()
        text = response.content[0].text
        self.assertIn('已提交', text)
        self.assertIn('不要重放', text)
        self.assertNotIn('未提交预测', text)
        self.assertEqual(self.budget.bank, 0)

    def test_workspace_publish_failure_is_recoverable_without_reexecution(self):
        from orchestrator import simulation
        write = simulation._write
        def fail_projection(path, value):
            if path.is_relative_to(self.session.workspace):
                raise OSError('projection')
            write(path, value)
        with patch('orchestrator.sim_assist.run_predictions', return_value=self.result) as worker:
            with patch.object(simulation, '_write', side_effect=fail_projection):
                result = self.session.simulate({'candidates': [['1,R']], 'request_id': 'publish'})
            self.assertIn('publication_warning', result)
            repeated = self.session.simulate({'candidates': [['1,R']], 'request_id': 'publish'})
            self.assertIn('trajectory_file', repeated)
            worker.assert_called_once()
        with self.assertRaisesRegex(ValueError, '不同内容'):
            self.session.simulate({'candidates': [['1,L']], 'request_id': 'publish'})

    def test_resume_uses_private_budget_and_original_result_without_bridge_or_worker(self):
        with patch('orchestrator.sim_assist.run_predictions', return_value=self.result):
            self.session.simulate({'candidates': [['1,R']], 'request_id': 'resume'})
        resumed = self.root / 'resumed'
        resumed.mkdir()
        (resumed / 'run_state.json').write_text(json.dumps({'resumed_from': str(self.run)}), encoding='utf-8')
        metrics = Metrics(resumed / 'metrics.json', 'test', 'stub', 'high', 100)
        session = GameSession(self.cfg, resumed, metrics)
        self.addCleanup(session._frames_log.close)
        self.addCleanup(session._decisions_log.close)
        session.planning_budget = PlanningBudget(metrics, resumed / 'planning_budget.json',
                                                previous_path=self.run / 'planning_budget.json')
        session.planning_budget.on_room('map:0', 'one')
        session.client = SimpleNamespace(call=lambda *a, **kw: self.fail('duplicate captured state'))
        with patch('orchestrator.sim_assist.run_predictions') as worker:
            result = session.simulate({'candidates': [['1,R']], 'request_id': 'resume'})
        worker.assert_not_called()
        self.assertEqual(result['planning']['left'], 0)
        self.assertEqual(result['planning']['used'], 1)
        self.assertEqual(result['receipt'], 'resume')


if __name__ == '__main__':
    unittest.main()
