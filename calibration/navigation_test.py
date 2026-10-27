"""Navigation commits through real session entry paths; bridge/menu IO is external."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import CHAPTER_SIDS, RunConfig
from orchestrator.game_session import ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.segments import Piece
from calibration.chapter_plan_test import TABLE
from calibration.map_entry_guard_test import MenuClient


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        cfg = RunConfig(agent='test', save='test', save_slot=10, model='test', runtime='host',
                        chapters=[1, 2, 3], cutscene_mode='auto', checkpoints=TABLE)
        metrics = Metrics(self.root/'metrics.json', 'test', 'test', 'medium', 600)
        metrics.update(chapter=CHAPTER_SIDS[1])
        self.session = GameSession(cfg, self.root, metrics)
        self.session.entry_guard_supported = True
        self.session.chapter_reset_guard_supported = True
        (self.root/'run_state.json').write_text('{}', encoding='utf-8')

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def finish_current(self):
        self.session._chapter_complete()
        self.assertTrue(self.session.chapter_finished)
        self.assertEqual(self.session.navigation.target, Piece(2))

    def test_result_metadata_cannot_commit_an_automatic_handoff(self):
        self.finish_current()
        for scene, completed in [('AreaComplete', False), ('LevelExit', False), ('Level', True)]:
            with self.subTest(scene=scene, completed=completed):
                class EndingClient(MenuClient):
                    def call(client, cmd, **args):
                        reply = super().call(cmd, **args)
                        # The real menu macro has entered Level; its following
                        # observation instead reports an ending with retained SID.
                        if cmd == 'observe' and reply['obs'].get('scene') == 'Level':
                            reply['obs']['scene'] = scene
                            reply['obs']['level']['completed'] = completed
                        return reply
                self.session.client = EndingClient(self.session._step_options)
                with self.assertRaisesRegex(ActError, '保留当前计划'):
                    self.session._enter_next_chapter()
                self.assertEqual(self.session.chapter_queue, [Piece(2), Piece(3)])
                self.assertEqual(self.session.navigation.target, Piece(2))
                self.assertTrue(self.session.chapter_finished)
                self.assertTrue(json.loads((self.root/'run_state.json').read_text())['piece_pending'])

    def test_successful_handoff_commits_once_and_keeps_guard_on_current_piece(self):
        self.finish_current()
        self.session.client = MenuClient(self.session._step_options)
        self.session._enter_next_chapter()
        self.assertFalse(self.session.chapter_finished)
        self.assertEqual(self.session.piece, Piece(2))
        self.assertEqual(self.session.chapter_queue, [Piece(3)])
        self.assertEqual(self.session._step_options()['entry_guard']['sid'], CHAPTER_SIDS[2])
        self.assertFalse(self.session._confirm_next_entry(self.session.client.obs))
        saved = json.loads((self.root/'run_state.json').read_text())
        self.assertEqual(saved['plan_pieces'], [[1, 0, None], [2, 0, None], [3, 0, None]])
        self.assertFalse(saved['piece_pending'])

    def test_resume_at_map_records_departure_before_first_agent_input(self):
        self.session.close()
        cfg = self.session.cfg
        cfg.plan, cfg.resume_pending, cfg.cutscene_mode = [Piece(1)], True, 'agent'
        self.session = GameSession(cfg, self.root, self.session.metrics)
        menu = {'scene':'Overworld', 'storyInput':None, 'save':{'name':'test','slot':10},
                'menu':{'current':'OuiChapterSelect'}}
        class Client:
            obs = menu
            steps = []
            def __init__(client, **kwargs): pass
            def call(client, cmd, **kwargs):
                self.assertNotEqual(cmd, 'step')
                return {'obs':copy.deepcopy(client.obs), 'entry_guard':True}
            def close(client): pass
        with patch('orchestrator.game_session.BridgeClient', Client):
            self.session.connect()
        self.session.client.obs = {'scene':'Level', 'level':{'sid':CHAPTER_SIDS[1], 'room':'1'}}
        self.session._observe_next_chapter()
        self.assertFalse(self.session.chapter_finished)
        self.assertEqual(self.session.piece, Piece(1))
        self.assertEqual(self.session.chapter_queue, [])

    def test_agent_entry_at_wrong_checkpoint_keeps_the_target_queued(self):
        self.finish_current()
        self.session.cfg.cutscene_mode = 'agent'
        class Client:
            def call(client, cmd, **kwargs):
                return {'obs': {'scene':'Level', 'level':{'sid':CHAPTER_SIDS[2], 'room':'3'}}}
            def close(client): pass
        self.session.client = Client()
        self.session._observe_next_chapter()
        self.assertTrue(self.session.chapter_finished)
        self.assertEqual(self.session.chapter_queue, [Piece(2), Piece(3)])

    def test_same_chapter_restart_rejects_the_old_level_until_departure(self):
        self.session.cfg.cutscene_mode = 'agent'
        self.session.chapter_queue = [Piece(1)]
        self.session._chapter_complete()
        old = {'scene':'Level','level':{'sid':CHAPTER_SIDS[1],'room':'1'}}
        class Client:
            obs = old
            def call(client, cmd, **kwargs):
                self.assertIn(cmd, {'observe', 'deactivate'})
                return {'obs':client.obs}
            def close(client): pass
        self.session.client = Client()
        self.session._observe_next_chapter()
        self.assertTrue(self.session.chapter_finished)
        self.session.client.obs = {'scene':'Overworld'}
        self.session._observe_next_chapter()
        self.session.client.obs = old
        self.session._observe_next_chapter()
        self.assertFalse(self.session.chapter_finished)


if __name__ == '__main__':
    unittest.main()
