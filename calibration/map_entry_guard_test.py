"""Target checks precede entry inputs, including after a chapter handoff."""
import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig, CHAPTER_SIDS
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from tools.bridge_client import BridgeClient


class MapEntryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        cfg = RunConfig(agent='test',save='test',save_slot=10,model='test',runtime='host',
                        chapters=[1,2],cutscene_mode='agent')
        self.session = GameSession(cfg,root,Metrics(root/'metrics.json','test','test','medium',600))
        self.session.entry_guard_supported = True
        self.session.chapter_reset_guard_supported = True
        self.session.action_policy_supported = 1

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def test_every_step_uses_current_target_and_caller_cannot_override_it(self):
        client = BridgeClient.__new__(BridgeClient)
        client.next_id, client.cancel_check = 1, None
        client.step_options = self.session._step_options
        client.file = Mock()
        client.file.readline.side_effect = [b'{"id":1,"ok":true}\n',b'{"id":2,"ok":true}\n']
        client.call('step', inputs=['1,C'],entry_guard={'sid':'wrong'})
        first = json.loads(client.file.write.call_args.args[0])
        self.assertEqual(first['entry_guard'],{'sid':CHAPTER_SIDS[1],'slot':10})
        self.session.chapter_finished = True
        client.call('step', inputs=['1,C'])
        second = json.loads(client.file.write.call_args.args[0])
        self.assertEqual(second['entry_guard']['sid'],CHAPTER_SIDS[2])

    def test_unsupported_bridge_fails_before_sending_any_step(self):
        self.session.entry_guard_supported = False
        client = BridgeClient.__new__(BridgeClient)
        client.cancel_check = None
        client.step_options = self.session._step_options
        client.file = Mock()
        with self.assertRaisesRegex(ValueError,'1.0.22'):
            client.call('step', inputs=['1,C'])
        client.file.write.assert_not_called()

    def test_connect_refuses_wrong_existing_map_without_sending_inputs(self):
        obs = {'scene':'Level','storyInput':None,'save':{'name':'test','slot':10},
               'level':{'sid':CHAPTER_SIDS[2],'side':'A'}}
        client = Mock()
        client.call.return_value = {'obs':obs,'entry_guard':True}
        with patch('orchestrator.game_session.BridgeClient',return_value=client):
            with self.assertRaisesRegex(RuntimeError,'当前地图.*不一致'):
                self.session.connect()
        self.assertFalse(any(call.args[0] == 'step' for call in client.call.call_args_list))

    def test_unrestricted_chapter_does_not_impose_a_map(self):
        self.session.piece = None
        self.session.cfg.chapter = None
        self.assertEqual(self.session._step_options()['entry_guard'], {'sid':None,'slot':10})
        self.assertEqual(self.session._step_options()['action_policy']['entry_target'], {'sid':None,'slot':10})

    def test_automatic_handoff_runs_real_menu_macro_with_the_target_still_queued(self):
        for queue in ([2], [2,3]):
            with self.subTest(queue=queue):
                self.session.cfg.cutscene_mode = 'auto'
                self.session.chapter_finished = True
                self.session.chapter_queue = list(queue)
                self.session.metrics.update(splits=[{'chapter':CHAPTER_SIDS[1],'ticks':1,'deaths':0}])
                client = MenuClient(self.session._step_options)
                self.session.client = client
                self.session._enter_next_chapter()  # real enter_save; only the bridge is simulated
                self.assertEqual(self.session.piece.chapter,2)
                self.assertEqual(self.session.chapter_queue,list(queue[1:]))
                self.assertFalse(self.session.chapter_finished)
                self.assertTrue(client.guards)
                self.assertTrue(all(g['sid'] == CHAPTER_SIDS[2] for g in client.guards))

    def test_failed_or_stopped_macro_does_not_consume_its_target(self):
        for failure in ('bridge_error','user_stop'):
            with self.subTest(failure=failure):
                self.session.cfg.cutscene_mode = 'auto'
                self.session.chapter_finished = True
                self.session.chapter_queue = [2,3]
                self.session.metrics.update(splits=[{'chapter':CHAPTER_SIDS[1],'ticks':1,'deaths':0}])
                client = MenuClient(self.session._step_options,fail=failure=='bridge_error')
                self.session.client = client
                if failure == 'bridge_error':
                    with self.assertRaisesRegex(RuntimeError,'bridge error'):
                        self.session._enter_next_chapter()
                else:
                    self.session.goals.stop_motion.set()
                    self.session._enter_next_chapter()
                    self.session.goals.stop_motion.clear()
                self.assertEqual(self.session.chapter_queue,[2,3])
                self.assertTrue(self.session.chapter_finished)
                self.assertEqual(self.session._step_options()['entry_guard']['sid'],CHAPTER_SIDS[2])


class MenuClient:
    """External menu state machine; a mismatched guard cannot be mocked as success."""
    def __init__(self, options, fail=False):
        self.options, self.fail, self.guards = options, fail, []
        self.obs = {'scene':'Overworld','save':{'name':'test','slot':10},
                    'menu':{'current':'OuiChapterSelect','area':1}}

    def call(self, cmd, **args):
        if cmd == 'step':
            if self.fail:
                raise RuntimeError('bridge error')
            guard = self.options()['entry_guard']
            self.guards.append(guard)
            consumed = 0
            for segment in args['inputs']:
                parts = segment.split(',')
                count, keys = int(parts[0]),set(parts[1:])
                menu = self.obs.get('menu') or {}
                if menu.get('current') == 'OuiChapterSelect':
                    if 'R' in keys: menu['area'] += 1
                    if 'L' in keys: menu['area'] -= 1
                    if 'C' in keys:
                        self.obs['menu'] = {'current':'OuiChapterPanel','panelArea':menu['area']}
                elif menu.get('current') == 'OuiChapterPanel' and 'C' in keys:
                    sid = CHAPTER_SIDS[menu['panelArea']]
                    if guard['sid'] != sid:
                        return {'obs':copy.deepcopy(self.obs),'result':{'stop':'entry_blocked','consumed':consumed}}
                    self.obs = {'scene':'Level','save':{'name':'test','slot':10},'controllable':True,
                                'level':{'sid':sid,'room':'start','inCutscene':False,'skippingCutscene':False}}
                consumed += count
            return {'obs':copy.deepcopy(self.obs),'result':{'stop':'done','consumed':consumed}}
        return {'obs':copy.deepcopy(self.obs)}

    def close(self):
        pass
