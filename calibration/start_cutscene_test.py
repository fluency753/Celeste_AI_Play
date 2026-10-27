"""auto 剧情模式：跳过剧情的淡出过程中角色已报告可操作（例如 5A 开场），主控仍要接着推进到剧情结束，
智能体的第一次 act 不落在黑屏上，observe 也不让它看着黑屏判断。"""
import copy
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics


def skipping_screen():
    return {'scene': 'Level', 'frame': 962, 'controllable': True, 'storyInput': None, 'storyDecision': None,
            'level': {'sid': 'Celeste/5-MirrorTemple', 'room': 'a-00b', 'inCutscene': True, 'skippingCutscene': True,
                      'dialogOpen': False, 'dialogWaiting': False, 'paused': False, 'transitioning': False,
                      'frozen': False, 'completed': False, 'canPause': False},
            'player': {'pos': [100, 200]}}


class SkipClient:
    def __init__(self, frames_to_finish=30):
        self.obs = skipping_screen()
        self.left = frames_to_finish
        self.steps = []

    def call(self, cmd, **args):
        if cmd in {'observe', 'activate'}:
            return {'obs': copy.deepcopy(self.obs)}
        if cmd == 'step':
            inputs = args['inputs']
            self.steps.append(inputs)
            consumed = sum(int(x.split(',')[0]) for x in inputs)
            self.left -= consumed
            if self.left <= 0:
                self.obs['level'].update(inCutscene=False, skippingCutscene=False, canPause=True)
            self.obs['frame'] += consumed
            return {'result': {'consumed': consumed, 'planned': consumed, 'stop': 'done', 'events': [],
                               'trajectory': []}, 'obs': copy.deepcopy(self.obs)}
        return {}

    def close(self):
        pass


class StartCutsceneTests(unittest.TestCase):
    def session(self, client, mode='auto'):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        cfg = RunConfig(agent='skip-test', save='player', model='test', runtime='host', cutscene_mode=mode)
        s = GameSession(cfg, path, Metrics(path / 'metrics.json', 'test', 'test', 'high', 60))
        self.addCleanup(s.close, False)
        s.client = client
        s.last_obs = skipping_screen()
        return s

    def test_settled_needs_the_cutscene_to_end(self):
        self.assertFalse(GameSession._settled(skipping_screen()))
        done = skipping_screen()
        done['level']['inCutscene'] = False
        self.assertTrue(GameSession._settled(done))
        done['controllable'] = False
        self.assertFalse(GameSession._settled(done))

    def test_first_act_finishes_the_skip_before_the_agents_keys(self):
        client = SkipClient()
        s = self.session(client)
        notes = s._resume_auto_handling()
        self.assertTrue(client.steps)                                        # 主控先推进
        self.assertFalse(s.last_obs['level']['inCutscene'])
        self.assertTrue(any('执行前' in str(n) for n in notes))
        self.assertFalse(any('J' in x or 'R' in x for st in client.steps for x in st))   # 推进时不按智能体的键

    def test_settled_screen_is_left_alone(self):
        client = SkipClient()
        s = self.session(client)
        s.last_obs['level']['inCutscene'] = False
        self.assertEqual(s._resume_auto_handling(), [])
        self.assertEqual(client.steps, [])

    def test_observe_in_auto_mode_finishes_the_skip(self):
        client = SkipClient()
        s = self.session(client)
        obs, _ = s.observe()
        self.assertFalse(obs['level']['inCutscene'])
        self.assertTrue(client.steps)

    def test_agent_mode_observe_does_not_auto_skip(self):
        client = SkipClient()
        s = self.session(client, mode='agent')
        s.observe()
        self.assertEqual(client.steps, [])                                   # 正在跳过时不算待处理剧情，原样交给智能体


if __name__ == '__main__':
    unittest.main()
