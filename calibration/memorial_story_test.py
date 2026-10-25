import json
import unittest

from orchestrator.observation import describe_obs, room_json
from tools.cutscene import story_pending


class MemorialStoryTests(unittest.TestCase):
    def test_visible_inscription_reaches_agent_without_blocking_movement(self):
        observation = {'scene': 'Level', 'controllable': True,
                       'story': [{'kind': 'memorial', 'text': '献给\n登山者', 'waitingForInput': False}]}
        self.assertIn('当前纪念碑文字：献给\n登山者', describe_obs(observation))
        self.assertEqual(json.loads(room_json(observation))['story'], observation['story'])
        self.assertFalse(story_pending(observation))
