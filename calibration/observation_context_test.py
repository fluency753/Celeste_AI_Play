import json
import unittest
from orchestrator.observation import compact_entity, room_json, terrain_regions, describe_obs


class ObservationContextTest(unittest.TestCase):
    def test_visible_story_text_reaches_agent_in_text_and_json(self):
        story={'kind':'dialogue','text':'你好，向山而行。','speakerPortrait':{'sprite':'granny','expression':'laugh'},'page':1,'waitingForInput':True}
        obs={'scene':'Level','frame':1,'level':{'sid':'Celeste/0-Intro','room':'2','inCutscene':True,'chapterTimeTicks':0,'deaths':0,'deathsInRoom':0,'bounds':[0,0,320,180],'camera':[0,0],'side':'A'},'story':[story]}
        self.assertEqual(json.loads(room_json(obs))['story'],[story])
        self.assertIn(story['text'],describe_obs(obs,full_room=False))
        self.assertIn('granny',describe_obs(obs,full_room=False))

    def test_static_wall_separates_target_but_not_same_side(self):
        observation = {"tiles": {"scope": "room", "origin": [0, 0], "cell": 8,
                                 "rows": ["..#..", "..#..", "..#.."]},
                       "level": {"bounds": [0, 0, 40, 24]},
                       "player": {"hitbox": [0, 0, 8, 11]},
                       "entities": [{"id": 1, "type": "Spring", "cat": "spring", "x": 32, "y": 8, "w": 4, "h": 4},
                                    {"id": 2, "type": "Strawberry", "cat": "collectible", "x": 8, "y": 8, "w": 4, "h": 4}]}
        text = "\n".join(terrain_regions(observation))
        self.assertIn("Spring#1", text)
        self.assertNotIn("Strawberry#2", text)
        observation["tiles"]["rows"][0] = "....."
        self.assertEqual(terrain_regions(observation), [])
        observation["tiles"]["scope"] = "view"
        self.assertEqual(terrain_regions(observation), [])

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


if __name__ == "__main__":
    unittest.main()
