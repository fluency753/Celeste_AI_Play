from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession, ActError
from orchestrator.metrics import Metrics


class ExitLevelTests(unittest.TestCase):
    def make_session(self, folder):
        cfg=RunConfig(agent='exit-test',save='same',model='test',save_slot=11,runtime='host',allow_chapter_reset=True)
        metrics=Metrics(folder/'metrics.json','exit-test','test','high',60)
        session=GameSession(cfg,folder,metrics)
        session.client=Mock()
        session.last_obs={'frame':10,'level':{'sid':'Celeste/0-Intro','room':'0'}}
        session.chapter_queue=[1,2]
        return session

    def test_agent_exit_uses_owned_connection_and_finishes_without_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            session=self.make_session(Path(folder))
            try:
                with patch('orchestrator.game_session.leave_level_by_pause_menu',return_value={'scene':'Overworld','frame':30}) as leave, \
                     patch('orchestrator.game_session.BridgeClient') as extra:
                    result=session.exit_level('用户要求退出序章')
                leave.assert_called_once_with(session.client, allowed_exit_ids=('menu_pause_return','intro_vignette_quit','menu_pause_savequit'))
                extra.assert_not_called()
                self.assertTrue(result['exited']);self.assertFalse(result['completed'])
                self.assertTrue(session.done and session.user_stopped)
                self.assertEqual(session.end_scene,'Overworld')
                self.assertEqual(session.chapter_queue,[])
                with self.assertRaises(ActError):session.exit_level('再次退出')
            finally:session.close()

    def test_failed_menu_exit_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            session=self.make_session(Path(folder))
            try:
                with patch('orchestrator.game_session.leave_level_by_pause_menu',return_value={'scene':'Level'}),self.assertRaises(ActError):
                    session.exit_level('退出')
                self.assertFalse(session.done)
                self.assertFalse((Path(folder)/'exit_level.json').exists())
            finally:session.close()
