"""A maintenance reload cannot release queued plans before a new model observation."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calibration.collision_contract_test import observation, FakeBridge
from orchestrator.config import RunConfig
from orchestrator.game_session import ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


class TrackedBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.calls = []

    def call(self, command, **options):
        self.calls.append((command, options))
        return super().call(command, **options)


class ReloadObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        cfg = RunConfig(agent='reload', save='test', model='test', runtime='host')
        self.session = GameSession(cfg, root, Metrics(root/'metrics.json', 'reload', 'test', 'high', 60))
        self.addCleanup(lambda: self.session.close(release=False))
        self.session.client = TrackedBridge()
        self.session.workspace = root/'workspace'
        (self.session.workspace/'game').mkdir(parents=True)
        self.server = build_server(self.session)

    async def test_neutral_key_persistence_failure_is_not_reported_as_success(self):
        held = self.session.run_dir/'held_input.json'
        held.write_text(json.dumps({'keys': 'R', 'input_format': 'celeste_ai', 'frame': 12}))
        with patch.object(Path, 'replace', side_effect=PermissionError('held file locked')):
            with self.assertRaisesRegex(PermissionError, 'held file locked'):
                self.session.after_bridge_reload(observation())
        self.assertTrue(self.session.reload_observation_required)
        self.assertEqual(self.session._held_input, ('', 'celeste_ai'))
        self.assertFalse(held.with_suffix('.reload-tmp').exists())

    async def test_successor_ignores_same_frame_key_from_incomplete_reload(self):
        previous = self.session.run_dir/'previous'
        previous.mkdir()
        (previous/'summary.json').write_text(json.dumps({'game_released': False}))
        (previous/'held_input.json').write_text(json.dumps({'keys': 'R', 'frame': 12}))
        self.session.metrics.state.resumed_from = str(previous)
        for content in ('{"phase":"refreshing"}', 'invalid'):
            with self.subTest(ledger=content):
                (previous/'bridge_reload.json').write_text(content)
                self.session._restore_held_input(12)
                self.assertEqual(self.session._held_input, ('', 'celeste_ai'))
                self.assertTrue(self.session.reload_observation_required)

    async def test_refresh_replaces_workspace_and_requires_default_observe(self):
        game = self.session.workspace/'game'
        for name in ('last_act.json', 'retry.json', 'sim_state.json', 'latest_obs.json', 'collision.json'):
            (game/name).write_text('stale')
        self.session.after_bridge_reload(observation())
        self.assertTrue(self.session.reload_observation_required)
        self.assertEqual(json.loads((game/'latest_obs.json').read_text())['obs']['frame'], 12)
        self.assertTrue(all(not (game/name).exists() for name in ('last_act.json', 'retry.json', 'sim_state.json')))
        before = list(self.session.client.calls)
        with self.assertRaisesRegex(ActError, 'observe'):
            self.session.act(['1,R'], 'old plan', None)
        with self.assertRaisesRegex(ActError, 'observe'):
            self.session.simulate({})
        self.assertEqual(self.session.client.calls, before)
        await self.server.call_tool('observe', {'detail': 'player'})
        self.assertTrue(self.session.reload_observation_required)
        result = await self.server.call_tool('observe', {})
        self.assertFalse(result.is_error)
        self.assertFalse(self.session.reload_observation_required)
        self.assertFalse(any(command == 'step' for command, _ in self.session.client.calls))

    async def test_action_queued_before_reload_is_rejected_even_after_observe_ack(self):
        await self.session.tool_lock.acquire()
        pending = asyncio.create_task(self.server.call_tool('act', {'inputs': ['1,R'], 'thought': 'old plan'}))
        await asyncio.sleep(0)
        self.session.after_bridge_reload(observation())
        self.session.acknowledge_reload_observation(self.session.reload_epoch)
        self.session.tool_lock.release()
        result = await pending
        text = '\n'.join(getattr(block, 'text', '') for block in result.content)
        self.assertIn('旧计划未执行', text)
        self.assertFalse(any(command == 'step' for command, _ in self.session.client.calls))

    async def test_old_observation_cannot_clear_new_reload_barrier(self):
        self.session.after_bridge_reload(observation())
        old = self.session.reload_epoch
        self.session.after_bridge_reload(observation())
        self.session.acknowledge_reload_observation(old)
        self.assertTrue(self.session.reload_observation_required)


if __name__ == '__main__':
    unittest.main()
