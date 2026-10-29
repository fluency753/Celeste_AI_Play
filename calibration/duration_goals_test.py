"""Duration goals use real goal/controller state; only bridge and clocks are external."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from calibration import goals_test
from calibration.goals_test import BASE
from orchestrator.goals import GoalManager,describe_goal,validate_goal
from orchestrator.metrics import Metrics


class DurationGoalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.goal=GoalManager(self.root)
        self.initial={**BASE,'frame':72000,'game_elapsed':1200,'game_epoch':1}

    def spec(self,seconds=1):
        return {'conditions':[{'field':'game_elapsed','op':'delta_gte','value':seconds}]}

    def test_fractional_seconds_and_repeated_stale_frames(self):
        self.goal.set(self.spec(.1),self.initial)
        self.assertFalse(self.goal.check({**self.initial,'frame':72003}))
        self.assertFalse(self.goal.check({**self.initial,'frame':72001}))
        self.assertFalse(self.goal.check({**self.initial,'frame':72003}))
        self.assertTrue(self.goal.check({**self.initial,'frame':72006}))
        self.assertEqual(self.goal.snapshot()['game_clock']['advanced_frames'],6)

    def test_game_restart_and_resume_preserve_accrued_duration(self):
        self.goal.set(self.spec(2),self.initial)
        self.assertFalse(self.goal.check({**self.initial,'frame':72030,'map':'new:A'}))
        restored=GoalManager(self.root)
        self.assertFalse(restored.check({**self.initial,'frame':0,'game_epoch':2}))
        self.assertFalse(restored.check({**self.initial,'frame':89,'game_epoch':2}))
        self.assertTrue(restored.check({**self.initial,'frame':90,'game_epoch':2}))
        self.assertEqual(restored.snapshot()['game_clock']['advanced_frames'],120)

    def test_unknown_epoch_is_reanchored_without_inventing_time(self):
        self.goal.set(self.spec(1),{**self.initial,'game_epoch':None})
        self.assertFalse(self.goal.check({**self.initial,'frame':90000}))
        self.assertTrue(self.goal.check({**self.initial,'frame':90060}))

    def test_thinking_and_planning_replays_do_not_advance_game_clock(self):
        self.goal.set(self.spec(1),self.initial)
        self.assertFalse(self.goal.check({**self.initial,'elapsed':10000}))
        self.assertEqual(self.goal.snapshot()['game_clock']['advanced_frames'],0)

    def test_unchanged_clock_does_not_rewrite_goal(self):
        self.goal.set(self.spec(1),self.initial)
        with patch.object(self.goal,'_save') as save:
            self.goal.check(self.initial);self.goal.check(self.initial)
        save.assert_not_called()

    def test_wall_duration_pauses_and_resumes_with_metrics(self):
        with patch('orchestrator.metrics.time.monotonic',return_value=1000):
            metrics=Metrics(self.root/'metrics.json','test','model','high',600)
            metrics.start_play();metrics.pause(status='paused')
        self.goal.set({'conditions':[{'field':'elapsed','op':'delta_gte','value':1}]},{**BASE,'elapsed':0})
        with patch('orchestrator.metrics.time.monotonic',return_value=1100):
            self.assertFalse(self.goal.check({**BASE,'elapsed':metrics.elapsed()}))
            metrics.resume(quota=False)
        with patch('orchestrator.metrics.time.monotonic',return_value=1101):
            self.assertTrue(self.goal.check({**BASE,'elapsed':metrics.elapsed()}))

    def test_invalid_duration_and_agent_notification(self):
        for value in (True,-1,'60',float('nan'),float('inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_goal(self.spec(value))
        with self.assertRaises(ValueError):validate_goal({'conditions':[{'field':'game_elapsed','op':'gte','value':1}]})
        text=describe_goal({'conditions':[{'field':'game_elapsed','op':'delta_gte','value':60},
                                        {'field':'elapsed','op':'delta_gte','value':600}],'match':'any'})
        self.assertIn('任一条件',text);self.assertIn('60',text);self.assertIn('600',text)
        self.assertIn('游戏推进时长',text);self.assertIn('现实运行时长',text)


class DurationSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.goal=GoalManager(self.directory)

    def session(self):
        return goals_test.GoalTests.session(self)

    def test_new_goal_anchors_at_fresh_observation_not_stale_menu(self):
        session=self.session();session.lock=threading.Lock();session.game_process_id=1
        session.client.call=Mock(return_value={'obs':{**session.last_obs,'frame':72000}})
        spec={'conditions':[{'field':'game_elapsed','op':'delta_gte','value':.1}]}
        session.set_goal(spec)
        self.assertEqual(session.goals.snapshot()['baseline']['frame'],72000)
        self.assertFalse(session.goals.check(session.goal_values()))
        session._on_progress({'event':'progress','frame':72006})
        self.assertTrue(session.goals.stop_motion.is_set())
        session.client.cancel_pending.assert_called_once()

    def test_inflight_goal_uses_progress_without_waiting_or_second_connection(self):
        session=self.session();session.lock=threading.Lock();session.game_process_id=1
        session._goal_progress={'frame':72000}
        session.client.call=Mock(side_effect=AssertionError('cannot query during action'))
        session.lock.acquire()
        try:session.set_goal({'conditions':[{'field':'game_elapsed','op':'delta_gte','value':.1}]})
        finally:session.lock.release()
        self.assertEqual(session.goal_values()['game_elapsed'],1200)
        session._on_progress({'event':'progress','frame':72006})
        self.assertTrue(session.goals.stop_motion.is_set())
        session.client.call.assert_not_called()

    def test_update_restarts_relative_duration(self):
        session=self.session();session.lock=threading.Lock()
        session.client.call=Mock(side_effect=[{'obs':{**session.last_obs,'frame':72000}},
                                            {'obs':{**session.last_obs,'frame':72030}}])
        spec={'conditions':[{'field':'game_elapsed','op':'delta_gte','value':1}]}
        session.set_goal(spec);session.goals.check({**session.goal_values(),'frame':72030})
        session.set_goal(spec)
        self.assertEqual(session.goals.snapshot()['game_clock']['advanced_frames'],0)
        self.assertFalse(session.goals.check({**session.goal_values(),'frame':72089}))
        self.assertTrue(session.goals.check({**session.goal_values(),'frame':72090}))


if __name__=='__main__':unittest.main()
