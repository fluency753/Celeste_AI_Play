"""Check relay mounts, authoritative counting and replay snapshot identity."""
import io
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from orchestrator import sim_relay
from orchestrator.container import _sim_label, _sim_mount_matches


class SimRelayTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cfg = SimpleNamespace(sim=True, sim_relay=True, sim_dir=Path(directory.name) / 'dist',
                                   workspace_dir=Path(directory.name) / 'workspace', engine='codex')
        self.playback = Mock(enabled=True)
        self.metrics = Mock()
        self.session = SimpleNamespace(cfg=self.cfg, metrics=self.metrics, playback=self.playback,
                                       sim_snapshot_digest='current',
                                       last_obs={'frame': 20, 'level': {'sid': 'city', 'room': '1'}})

    def test_raw_distribution_is_rejected_if_controller_owns_simulation(self):
        relay_mount = {'Destination': sim_relay.RELAY_PATH, 'Type': 'volume', 'Name': sim_relay.volume(self.cfg), 'RW': True}
        raw = {'Destination': '/sim', 'Type': 'bind', 'Source': str(self.cfg.sim_dir.resolve()), 'RW': False}
        managed = {**raw, 'Source': str(sim_relay.public_dir(self.cfg).resolve())}
        self.assertEqual(_sim_label(self.cfg), 'relay-v1')
        self.assertTrue(_sim_mount_matches(self.cfg, [managed, relay_mount]))
        self.assertFalse(_sim_mount_matches(self.cfg, [raw, relay_mount]))
        self.assertFalse(_sim_mount_matches(self.cfg, [managed]))

    def test_worker_gets_map_data_only_when_present(self):
        # 官方地图数据（sim/maps）只挂给主控的私有模拟进程，用于跨房间推演；没有时照旧只挂发布目录
        self.assertEqual(sim_relay.worker_mounts(self.cfg), ['-v', f'{self.cfg.sim_dir}:/sim:ro'])
        maps = self.cfg.sim_dir.parent / 'maps'
        maps.mkdir(parents=True)
        self.assertEqual(sim_relay.worker_mounts(self.cfg),
                         ['-v', f'{self.cfg.sim_dir}:/sim:ro', '-v', f'{maps}:/maps:ro'])

    def test_failed_start_cleanup_cannot_be_overtaken_by_start(self):
        with patch('orchestrator.sim_relay.find_docker_exe', return_value='docker'):
            relay = sim_relay.SimRelay(self.session)
        entered, release, closing = (threading.Event() for _ in range(3))
        order = []

        def start():
            entered.set()
            release.wait(2)
            order.append('start')

        def close():
            closing.set()
            relay.close()

        with patch.object(relay, '_start', side_effect=start) as launcher, patch.object(
                relay, '_close', side_effect=lambda: order.append('close')):
            starter = threading.Thread(target=relay.start)
            stopper = threading.Thread(target=close)
            starter.start()
            try:
                self.assertTrue(entered.wait(2))
                stopper.start()
                self.assertTrue(closing.wait(2))
                self.assertEqual(order, [])
            finally:
                release.set()
                starter.join(2)
                if stopper.ident is not None:
                    stopper.join(2)
            self.assertEqual(order, ['start', 'close'])
            relay.start()
            launcher.assert_called_once()

    def test_counts_historical_and_current_batches_but_only_replays_current_snapshot(self):
        with patch('orchestrator.sim_relay.find_docker_exe', return_value='docker'), patch('orchestrator.sim_relay.threading.Timer'):
            relay = sim_relay.SimRelay(self.session)
            trace = {'stop': 'room', 'frames': [[21, 2, 3]]}
            second = {'stop': 'death', 'frames': [[21, 5, 6]]}
            current = {'frame': 20, 'room': '1', 'sid': 'city', 'digest': 'current'}
            events = [
                {'ready': True},
                {'simulated': 250},
                {'simulated': 10, 'start': current, 'traces': [trace]},
                {'simulated': 5, 'start': {**current, 'sid': 'other'}, 'traces': [trace]},
                {'simulated': 9, 'start': current, 'traces': [second]},
                {'simulated': 6, 'start': current},
            ]
            relay.process = SimpleNamespace(stdout=io.BytesIO(''.join(json.dumps(e) + '\n' for e in events).encode()))
            relay.read()
            relay.flush()
            self.assertTrue(relay.ready.is_set())
            self.playback.collect_prediction.assert_called_once_with(
                [trace, second], 20, relay_batch=(('city', '1', 20, 'current'), 25))
            self.metrics.add.assert_called_once_with(sim_calls=5, sim_rollouts=280)
            relay.accept(events[2])
            self.session.last_obs['frame'] += 1
            relay.flush()
            self.playback.collect_prediction.assert_called_once()
            self.metrics.add.assert_called_with(sim_calls=1, sim_rollouts=10)
            self.playback.enabled = False
            relay.accept(events[2])
            relay.flush()
            self.playback.collect_prediction.assert_called_once()
            self.metrics.add.assert_called_with(sim_calls=1, sim_rollouts=10)


class RelayWorkerPreviewTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'agent-container/sim-relay.py'
        spec = importlib.util.spec_from_file_location('_relay_worker', source)
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)

    @staticmethod
    def result(end, length, stop='done', events=()):
        return {'ok': True, 'stop': stop, 'events': events,
                'frames': [[i, i * end / length, 0, 'ignored'] for i in range(length + 1)]}

    def test_preview_preserves_first_room_exit_and_ignores_backtracking_as_best(self):
        back = self.result(10, 3, 'room')
        cross = self.result(100, 10, 'death', [[6, 'room_exit', '2'], [7, 'room_enter', '2']])
        slow = self.result(55, 8, 'room')
        tracks = self.worker.preview_tracks([back, cross, slow], 1, 100, 40)
        self.assertEqual(tracks, [{'stop': 'room', 'frames': [row[:3] for row in cross['frames'][:7]]}])

    def test_budget_is_shared_across_replies_without_losing_counts(self):
        start = {'digest': 'current'}
        budget = [0, 0, 0]
        results = [self.result(i, 12) for i in range(110)]
        data = json.dumps({'results': results}).encode()
        self.worker.PREVIEW_POINTS = 400
        with patch.object(self.worker, 'time', SimpleNamespace(monotonic=lambda: 10)):
            events = [self.worker.simulation_event(data, {'batch': []}, start, 'current', threading.Lock(),
                                                   budget, None, 40) for _ in range(3)]
        tracks = [track for event in events for track in event.get('traces', [])]
        self.assertEqual(len(events[0]['traces']), 24)
        self.assertGreater(len(events[1]['traces']), 1)
        self.assertEqual(len(tracks), 30)
        self.assertNotIn('traces', events[2])
        self.assertEqual(sum(event['simulated'] for event in events), 330)


class RelayClientCliTests(unittest.TestCase):
    """代跑时 /sim/celeste-sim 换成了转发客户端：run 的输出要与原命令行一致，智能体已有的程序照常解析。"""

    def client(self):
        import importlib.util
        source = Path(__file__).resolve().parent.parent / 'orchestrator' / 'workspace_files' / 'sim_relay_client.py'
        spec = importlib.util.spec_from_file_location('_relay_client', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_run_output_matches_native_cli(self):
        client = self.client()
        out, err = io.StringIO(), io.StringIO()
        client.print_run({'ok': True, 'stop': 'room', 'detail': '6c', 'warnings': ['unknown bit tag lucid'],
                          'frames': [[2312, 2016.9166, -1135, 295, 0, 'Dash', 1, 0], [2313]]}, out, err)
        # 原命令行（celeste-sim run）：表头、逐帧 CSV（没有 Madeline 的帧其余 7 列留空）、stop 带详情
        self.assertEqual(out.getvalue().splitlines(),
                         ['frame,x,y,vx,vy,state,dashes,onGround', '2312,2016.9166,-1135,295,0,Dash,1,0', '2313,,,,,,,'])
        self.assertEqual(err.getvalue().splitlines(), ['warning: unknown bit tag lucid', 'stop: room 6c'])
        err = io.StringIO()
        client.print_run({'ok': True, 'stop': 'done', 'detail': None, 'frames': []}, io.StringIO(), err)
        self.assertEqual(err.getvalue(), 'stop: done\n')

    def test_through_rooms_turns_off_stop_on_room(self):
        client = self.client()
        args = SimpleNamespace(inputs=['12,R'], inputs_file=None, through_rooms=False)
        self.assertEqual(client.run_request(args), {'inputs': ['12,R']})
        args.through_rooms = True
        self.assertEqual(client.run_request(args), {'inputs': ['12,R'], 'stop_on_room': False})

    def test_inputs_file_is_accepted_like_native_cli(self):
        client = self.client()
        with TemporaryDirectory() as folder:
            plan = Path(folder) / 'plan.txt'
            plan.write_text('# 注释\n12,R\n\n1,R,J\n', encoding='utf-8')
            args = SimpleNamespace(inputs=['3'], inputs_file=str(plan))
            self.assertEqual(client.plan_of(args), ['3', '12,R', '1,R,J'])
            self.assertEqual(client.plan_of(SimpleNamespace(inputs=None, inputs_file=None)), [])
