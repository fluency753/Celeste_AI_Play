"""Check relay mounts, authoritative counting and replay snapshot identity."""
import io
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
            events = [
                {'ready': True},
                {'simulated': 250, 'start': {'frame': 8, 'room': '1', 'sid': 'city'}, 'trace': trace},
                {'simulated': 10, 'start': {'frame': 20, 'room': '1', 'sid': 'city', 'digest': 'current'}, 'trace': trace},
                {'simulated': 5, 'start': {'frame': 20, 'room': '1', 'sid': 'other'}, 'trace': trace},
            ]
            relay.process = SimpleNamespace(stdout=io.BytesIO(''.join(json.dumps(e) + '\n' for e in events).encode()))
            relay.read()
            relay.flush()
            self.assertTrue(relay.ready.is_set())
            self.playback.collect_prediction.assert_called_once_with([trace], 20)
            self.metrics.add.assert_called_once_with(sim_calls=3, sim_rollouts=265)
            self.playback.enabled = False
            relay.accept(events[2])
            relay.flush()
            self.playback.collect_prediction.assert_called_once()
            self.metrics.add.assert_called_with(sim_calls=1, sim_rollouts=10)
