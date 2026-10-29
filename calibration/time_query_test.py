"""Real observe tool and clock projection, with no game connection or model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from calibration.collision_contract_test import observation
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


class TimeQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cfg=RunConfig(agent='time-test',save='test',model='test',runtime='host',sim=False)
        self.metrics=Metrics(self.root/'metrics.json','time-test','test','high',600,study=True)
        self.session=GameSession(self.cfg,self.root,self.metrics)
        self.session.client=Mock()
        self.session.client.call.side_effect=AssertionError('time query must not contact the bridge')
        self.session.last_obs=observation(600)
        self.session.last_obs['level']['chapterTimeTicks']=20_000_000
        self.session.game_process_id=1
        self.gate=Mock(enabled=False)
        self.server=build_server(self.session,gate=self.gate)

    def tearDown(self):
        self.session.close(release=False)

    async def query(self, **options):
        result=await self.server.call_tool('observe',{'detail':'time',**options})
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content),1)
        self.assertEqual(result.content[0].type,'text')
        return json.loads(result.content[0].text)

    async def test_cached_clocks_without_goal_do_not_start_play_or_gate(self):
        data=await self.query()
        self.assertEqual(data['game']['frame'],600)
        self.assertEqual(data['game']['bridge_elapsed_seconds'],10)
        self.assertEqual(data['game']['chapter_elapsed_seconds'],2)
        self.assertEqual(data['game']['sample'],'cached_observation_or_progress')
        self.assertEqual(data['real'],{'elapsed_seconds':0,'playing':False})
        self.assertEqual(data['goal']['time_conditions'],[])
        self.assertFalse(self.metrics.playing)
        self.gate.start.assert_not_called();self.session.client.call.assert_not_called()

    async def test_projection_can_satisfy_one_condition_without_mutating_goal(self):
        self.session.goals.set({'conditions':[{'field':'game_elapsed','op':'delta_gte','value':2},
            {'field':'room','op':'changed'}],'match':'all'},self.session.goal_values())
        before=self.session.goals.path.read_bytes()
        self.session._goal_progress={'frame':720,'chapter_time_ticks':100_000_000}
        data=await self.query()
        condition=data['goal']['time_conditions'][0]
        self.assertEqual(condition,{'field':'game_elapsed','op':'delta_gte','target_seconds':2,
                                    'used_seconds':2,'remaining_seconds':0,'satisfied_now':True})
        self.assertEqual(data['game']['frame'],720)
        self.assertEqual(data['game']['chapter_elapsed_seconds'],10)
        self.assertTrue(data['goal']['active']);self.assertFalse(data['goal']['reached'])
        self.assertEqual(data['goal']['match'],'all')
        self.assertNotIn('remaining_seconds',data['goal'])
        self.assertFalse(self.session.goals.stop_motion.is_set())
        self.assertEqual(self.session.goals.path.read_bytes(),before)
        self.session.goals.check(self.session.goal_values())
        self.assertFalse(self.session.goals.stop_motion.is_set())  # room did not change
        self.assertEqual(self.session.goals.snapshot()['game_clock']['advanced_frames'],120)

    async def test_latest_observation_is_not_overwritten_by_older_progress(self):
        self.session.goals.set({'conditions':[{'field':'game_elapsed','op':'delta_gte','value':2}]},self.session.goal_values())
        self.session._goal_progress={'frame':630}
        self.session.last_obs=observation(660)
        data=await self.query()
        self.assertEqual(data['game']['frame'],660)
        self.assertEqual(data['goal']['time_conditions'][0]['used_seconds'],1)
        self.assertEqual(data['goal']['time_conditions'][0]['remaining_seconds'],1)

    async def test_process_change_preserves_checked_duration_without_inventing_new_frames(self):
        self.session.goals.set({'conditions':[{'field':'game_elapsed','op':'delta_gte','value':3}]},self.session.goal_values())
        self.session.goals.check({**self.session.goal_values(),'frame':660})
        self.session._goal_progress={'frame':660,'game_epoch':1,'chapter_time_ticks':99_000_000}
        self.session.game_process_id=2;self.session.last_obs=observation(12)
        data=await self.query()
        self.assertEqual(data['game']['game_process_id'],2)
        self.assertEqual(data['game']['frame'],12)
        self.assertEqual(self.session.goal_values()['frame'],12)
        self.assertEqual(data['goal']['time_conditions'][0]['used_seconds'],1)
        self.assertEqual(data['goal']['time_conditions'][0]['remaining_seconds'],2)

    async def test_old_process_progress_does_not_fabricate_an_unknown_current_sample(self):
        self.session.game_process_id=2;self.session.last_obs=None
        self.session._goal_progress={'frame':660,'game_epoch':1}
        data=await self.query()
        self.assertIsNone(data['game']['frame'])
        self.assertIsNone(data['game']['bridge_elapsed_seconds'])

    async def test_wall_delta_and_legacy_operators_have_distinct_countdown_semantics(self):
        self.metrics.start_play();self.metrics.pause(status='paused');self.metrics._offset=100
        self.session.goals.set({'conditions':[
            {'field':'elapsed','op':'delta_gte','value':20}, {'field':'elapsed','op':'gte','value':150},
            {'field':'elapsed','op':'lte','value':200}, {'field':'elapsed','op':'eq','value':50}], 'match':'any'},
            {**self.session.goal_values(),'elapsed':90})
        data=await self.query();conditions=data['goal']['time_conditions']
        self.assertAlmostEqual(conditions[0]['used_seconds'],10,delta=.05)
        self.assertAlmostEqual(conditions[0]['remaining_seconds'],10,delta=.05)
        self.assertAlmostEqual(conditions[1]['remaining_seconds'],50,delta=.05)
        self.assertIsNone(conditions[2]['remaining_seconds']);self.assertTrue(conditions[2]['satisfied_now'])
        self.assertIsNone(conditions[3]['remaining_seconds']);self.assertFalse(conditions[3]['satisfied_now'])
        self.assertFalse(data['goal']['reached'])

    async def test_unknown_game_sample_and_cleared_goal_are_explicit(self):
        self.session.last_obs=None
        data=await self.query()
        self.assertIsNone(data['game']['frame']);self.assertIsNone(data['game']['bridge_elapsed_seconds'])
        self.assertIsNone(data['game']['chapter_elapsed_seconds'])
        self.session.goals.set({'conditions':[{'field':'elapsed','op':'gte','value':100}]},self.session.goal_values())
        self.session.goals.clear()
        self.assertEqual((await self.query())['goal']['time_conditions'],[])

    async def test_existing_permissions_and_time_parameters_are_enforced(self):
        self.session.control_guard=lambda:'用户已暂停试玩'
        blocked=await self.server.call_tool('observe',{'detail':'time'})
        self.assertIn('暂停',blocked.content[0].text)
        self.session.control_guard=None
        invalid=await self.server.call_tool('observe',{'detail':'time','region':[0,0,8,8]})
        self.assertIn('不接受区域',invalid.content[0].text)
        self.assertFalse(self.metrics.playing)


if __name__=='__main__':unittest.main()
