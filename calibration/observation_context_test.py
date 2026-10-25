import json
import unittest
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
from orchestrator.observation import compact_entity, room_json, describe_obs, enrich_obs


class ObservationContextTest(unittest.TestCase):
    def test_visible_story_text_reaches_agent_in_text_and_json(self):
        story={'kind':'dialogue','text':'你好，向山而行。','speakerPortrait':{'sprite':'granny','expression':'laugh'},'page':1,'waitingForInput':True}
        obs={'scene':'Level','frame':1,'level':{'sid':'Celeste/0-Intro','room':'2','inCutscene':True,'chapterTimeTicks':0,'deaths':0,'deathsInRoom':0,'bounds':[0,0,320,180],'camera':[0,0],'side':'A'},'story':[story]}
        self.assertEqual(json.loads(room_json(obs))['story'],[story])
        self.assertIn(story['text'],describe_obs(obs,full_room=False))
        self.assertIn('granny',describe_obs(obs,full_room=False))

    def test_full_geometry_is_delivered_without_host_route_verdicts(self):
        observation = {"frame": 12, "tiles": {"scope": "room", "origin": [0, 0], "cell": 8,
                                 "rows": ["..#..", "..#..", "..#.."]},
                       "level": {"sid": "Celeste/1-ForsakenCity", "room": "test", "bounds": [0, 0, 40, 24]},
                       "player": {"hitbox": [0, 0, 8, 11]},
                       "entities": [{"id": 1, "type": "Spring", "cat": "spring", "x": 32, "y": 8, "w": 4, "h": 4},
                                    {"id": 2, "type": "Strawberry", "cat": "collectible", "x": 8, "y": 8, "w": 4, "h": 4}]}
        result = json.loads(room_json(enrich_obs(observation)))
        self.assertEqual(result['solids'],[[16,0,8,24]])
        self.assertEqual(result['entities'],observation['entities'])
        self.assertEqual(result['player']['hitbox'],[0,0,8,11])
        self.assertEqual((result['frame'],result['sid'],result['room']),
                         (12,'Celeste/1-ForsakenCity','test'))
        self.assertNotIn('connectivity',result)

    def test_missing_geometry_is_not_reported_as_empty_space(self):
        self.assertIsNone(json.loads(room_json({}))['solids'])
        self.assertEqual(json.loads(room_json({'solids':[]}))['solids'],[])

    def test_dormant_spinner_mixed_collider_reaches_full_room_json(self):
        collider={'shape':'list','parts':[{'shape':'circle','x':94,'y':44,'w':12,'h':12,'r':6},
                                          {'shape':'rect','x':92,'y':47,'w':16,'h':4}]}
        entities=[{'id':i,'type':'CrystalStaticSpinner','cat':'hazard','collidable':False,
                   'visible':False,'collider':collider} for i in range(668)]
        result=json.loads(room_json({'entities':entities}))
        self.assertEqual(result['entities'],entities)

    def test_room_json_keeps_entities_beyond_summary_display_limit(self):
        entities=[{'id':i,'type':'Solid','cat':'solid','x':i*8,'y':0,'w':8,'h':8,
                   'collidable':i%2==0,'shape':{'type':'list','parts':[[0,0,4,4]]}} for i in range(80)]
        self.assertEqual(json.loads(room_json({'entities':entities}))['entities'],entities)

    def test_collectible_state_survives_without_unrelated_reflection(self):
        entity = {"type": "Strawberry", "cat": "collectible", "fields": {
            "collected": False, "isGhostBerry": True, "Winged": True, "unrelated": "private"}}
        output = compact_entity(entity)
        self.assertFalse(output["collected"])
        self.assertTrue(output["isGhostBerry"])
        self.assertTrue(output["Winged"])
        self.assertNotIn("fields", output)
        self.assertNotIn("unrelated", output)

    def test_exits_are_not_advertised_as_verified_routes(self):
        result = json.loads(room_json({}))
        self.assertIn("不保证", result["exitsNote"])

    def test_real_tool_dispatch_keeps_data_without_reactive_geometry_hints(self):
        from calibration.formal_run_test import _observation
        from orchestrator.config import RunConfig
        from orchestrator.metrics import Metrics
        from orchestrator.server import build_server, ROOM_JSON_HEADER
        for mode in ('explore','speedrun'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                cfg=RunConfig(agent='test',save='test',model='test',runtime='host',mode=mode,
                              knowledge_profile='explore' if mode=='explore' else 'full',sim=False)
                obs=enrich_obs(_observation())
                metrics=Metrics(Path(folder)/'metrics.json','test','test','high',60)
                outcome=SimpleNamespace(result={'consumed':30,'planned':30,'stop':'done','events':[]},
                    obs=obs,attempts=[{}],auto_notes=[],image_b64=None)
                session=SimpleNamespace(cfg=cfg,metrics=metrics,workspace=None,done=False,
                    observe=Mock(return_value=(obs,None)),act=Mock(return_value=outcome))
                server=build_server(session)
                observed=asyncio.run(server.call_tool('observe',{}))
                text='\n'.join(block.text for block in observed.content if hasattr(block,'text'))
                data=json.loads(text.split(ROOM_JSON_HEADER,1)[1])
                self.assertEqual(data['solids'],obs['solids'])
                self.assertEqual(data['entities'],obs['entities'])
                self.assertNotIn('connectivity',data)
                acted=asyncio.run(server.call_tool('act',{'inputs':['30,R'],'thought':'验证通路'}))
                action_text='\n'.join(block.text for block in acted.content if hasattr(block,'text'))
                for output in (text,action_text):
                    for hint in ('【几何核验】','【立即复查】','局部阻挡检查','静态地形检查'):
                        self.assertNotIn(hint,output)


if __name__ == "__main__":
    unittest.main()
