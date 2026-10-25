"""Target checks precede entry inputs, including after a chapter handoff."""
import json
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
        self.assertEqual(self.session._step_options(),{'entry_guard':{'sid':None,'slot':10}})
