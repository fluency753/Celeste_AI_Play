"""Native counter receipts through real BridgeClient hooks, tools and HUD metrics.

Only the socket/game boundary is replaced; native StepJob receipt generation is
checked separately in calibration/chapter_reset.
"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calibration.story_wait_test import BridgeWire, observation
from orchestrator.action_policy import ActionPermissions, configure
from orchestrator.config import RunConfig
from orchestrator.game_session import ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server
from tools.bridge_client import BridgeClient


class ReceiptBridge:
    def __init__(self, *, deaths=1, events=(), menu=False, fail_exit=False):
        self.obs = observation(controllable=True, cutscene=False, canPause=True)
        self.deaths, self.events = deaths, list(events)
        self.steps, self.fail_exit = 0, fail_exit
        if menu:
            self.obs['level']['paused'] = True
            self.obs['pauseMenu'] = {'focused': True, 'selection': 0, 'items': [
                {'index': 0, 'id': 'menu_pause_savequit', 'selectable': True, 'disabled': False}]}

    def call(self, command, **args):
        if command != 'step':
            return {'obs': copy.deepcopy(self.obs)}
        self.steps += 1
        if self.fail_exit and self.steps > 1:
            raise OSError('exit transition interrupted')
        used = sum(int(x.split(',')[0]) for x in args['inputs'])
        self.obs['frame'] += used
        if 'pauseMenu' in self.obs:
            self.obs = {'scene': 'LevelExit' if self.fail_exit else 'Overworld',
                        'frame': self.obs['frame'], 'level': None, 'player': None}
        return {'result': {'consumed': used, 'planned': used, 'loadingFrames': 0,
                           'registeredDeaths': self.deaths, 'events': self.events,
                           'stop': 'done', 'trajectory': []}, 'obs': copy.deepcopy(self.obs)}


class DeathAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        cfg = RunConfig(agent='death-test', save='test', model='test', runtime='host',
                        cutscene_mode='agent', knowledge_profile='explore', chapter=3)
        cfg.thought_lead_s = 0
        self.session = GameSession(cfg, self.root, Metrics(self.root/'metrics.json', 'test', 'test', 'medium', 600))

    def tearDown(self):
        self.session.close(release=False)
        self.temp.cleanup()

    def connect(self, bridge):
        with patch('tools.bridge_client.socket.create_connection', return_value=BridgeWire(bridge)):
            client = BridgeClient(port=1)
        client.after_step = self.session._after_step
        self.session.client = client
        self.session.last_obs = copy.deepcopy(bridge.obs)
        return build_server(self.session)

    def test_act_receipt_and_physical_event_count_once_in_published_metrics(self):
        server = self.connect(ReceiptBridge(events=[{'type': 'death', 'frame': 101}]))
        result = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': 'test'}))
        self.assertFalse(result.is_error)
        self.assertEqual(self.session.metrics.state.deaths, 1)
        self.assertEqual(json.loads((self.root/'metrics.json').read_text(encoding='utf-8'))['deaths'], 1)
        self.session.observe()
        self.session.observe()
        self.assertEqual(self.session.metrics.state.deaths, 1)

    def test_menu_save_quit_is_counted_without_handle_events_and_repeat_does_not_recount(self):
        bridge = ReceiptBridge(menu=True)
        self.connect(bridge)
        configure(self.session.cfg, ActionPermissions(save_quit=True))
        result = self.session.exit_level('save and quit', finish_run=True)
        self.assertTrue(result['exited'])
        self.assertEqual(self.session.metrics.state.deaths, 1)
        with self.assertRaises(ActError):
            self.session.exit_level('save and quit', finish_run=True)
        self.assertEqual(self.session.metrics.state.deaths, 1)
        self.assertEqual(bridge.steps, 1)

    def test_failure_after_save_quit_retains_actual_count_without_fabricating_completion(self):
        self.connect(ReceiptBridge(menu=True, fail_exit=True))
        configure(self.session.cfg, ActionPermissions(save_quit=True))
        with self.assertRaises(ActError):
            self.session.exit_level('save and quit', finish_run=True)
        self.assertEqual(self.session.metrics.state.deaths, 1)
        self.assertFalse(self.session.done)

    def test_nonstat_event_counts_zero_and_seeker_does_not_hide_registered_deaths(self):
        self.connect(ReceiptBridge(deaths=0, events=[{'type': 'death', 'frame': 101}]))
        self.session._seeker_act = True
        reply = self.session.client.call('step', inputs=['1'])
        self.session._handle_events(reply['result'], reply['obs'])
        self.assertTrue(self.session._seeker_death)
        self.assertEqual(self.session.metrics.state.deaths, 0)
        self.session.client.sock.bridge.deaths = 2
        reply = self.session.client.call('step', inputs=['1'])
        self.session._handle_events(reply['result'], reply['obs'])
        self.assertEqual(self.session.metrics.state.deaths, 2)

    def test_restore_and_multiple_registered_deaths_preserve_cumulative_and_chapter_totals(self):
        self.session.metrics.restore({'deaths': 6, 'chapter_base': {'deaths': 4}}, str(self.root/'previous'))
        self.connect(ReceiptBridge(deaths=2))
        self.session.client.call('step', inputs=['1'])
        self.assertEqual(self.session.metrics.state.deaths, 8)
        self.assertEqual(self.session.metrics.state.deaths-self.session.metrics.state.chapter_base['deaths'], 4)

    def test_zero_and_legacy_results_preserve_compatibility(self):
        self.connect(ReceiptBridge(deaths=0))
        self.session.client.call('step', inputs=['1'])
        self.assertEqual(self.session.metrics.state.deaths, 0)
        self.session._handle_events({'events': [{'type': 'death'}]})
        self.assertEqual(self.session.metrics.state.deaths, 1)
        self.session._seeker_act = True
        self.session._handle_events({'events': [{'type': 'death'}]})
        self.assertEqual(self.session.metrics.state.deaths, 1)


if __name__ == '__main__':
    unittest.main()
