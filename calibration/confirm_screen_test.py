"""画面冻结等确认（第一次拿到磁带后的"B 面已解锁"、吃心后的诗句）：auto 模式主控按确认继续，按了不恢复就交回而不空推；
agent 模式交回由智能体按；智能体在 auto 模式下也能用 C 确认，冻结等确认时它的按键照常执行。"""
import copy
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import RunConfig
from orchestrator.game_session import FROZEN_GIVE_UP_FRAMES, GameSession, PlanSpec, awaiting_confirm
from orchestrator.metrics import Metrics


def frozen_screen():
    return {'scene': 'Level', 'frame': 100, 'controllable': False, 'storyInput': None,
            'level': {'sid': 'Celeste/4-GoldenRidge', 'room': 'a-11', 'inCutscene': False, 'dialogOpen': False,
                      'dialogWaiting': False, 'paused': False, 'transitioning': False, 'frozen': True,
                      'completed': False},
            'player': {'pos': [2900, -830]}}


class ConfirmClient:
    """resolve_after：按第几次确认后画面恢复（None 表示按了也不恢复）。"""

    def __init__(self, resolve_after=1):
        self.obs = frozen_screen()
        self.resolve_after = resolve_after
        self.steps, self.confirms = [], 0

    def call(self, cmd, **args):
        if cmd in {'observe', 'activate'}:
            return {'obs': copy.deepcopy(self.obs)}
        if cmd == 'step':
            inputs = args['inputs']
            self.steps.append(inputs)
            if any('C' in x.split(',')[1:] for x in inputs):
                self.confirms += 1
                if self.resolve_after is not None and self.confirms >= self.resolve_after:
                    self.obs['level']['frozen'] = False
                    self.obs['controllable'] = True
            consumed = sum(int(x.split(',')[0]) for x in inputs)
            self.obs['frame'] += consumed
            return {'result': {'consumed': consumed, 'planned': consumed, 'stop': 'done', 'events': [],
                               'trajectory': []}, 'obs': copy.deepcopy(self.obs)}
        return {}

    def close(self):
        pass


class ConfirmScreenTests(unittest.TestCase):
    def session(self, mode, client):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        cfg = RunConfig(agent='confirm-test', save='player', model='test', runtime='host', cutscene_mode=mode)
        s = GameSession(cfg, path, Metrics(path / 'metrics.json', 'test', 'test', 'high', 60))
        self.addCleanup(s.close, False)
        s.client = client
        s.last_obs = frozen_screen()
        return s

    def test_detects_only_frozen_screens_waiting_for_input(self):
        self.assertTrue(awaiting_confirm(frozen_screen()))
        for change in ({'inCutscene': True}, {'dialogWaiting': True}, {'paused': True}, {'transitioning': True},
                       {'frozen': False}, {'completed': True}):
            obs = frozen_screen()
            obs['level'].update(change)
            self.assertFalse(awaiting_confirm(obs), change)
        obs = frozen_screen()
        obs['controllable'] = True
        self.assertFalse(awaiting_confirm(obs))

    def test_auto_mode_presses_confirm_and_continues(self):
        client = ConfirmClient(resolve_after=2)
        s = self.session('auto', client)
        obs, _, total, notes = s._auto_advance(frozen_screen(), None)
        self.assertTrue(obs['controllable'])
        self.assertEqual(client.confirms, 2)
        self.assertLessEqual(total, 60)                          # 不再空推上万帧
        self.assertTrue(any('确认 C' in str(n) for n in notes))

    def test_auto_mode_gives_up_instead_of_burning_the_timer(self):
        client = ConfirmClient(resolve_after=None)
        s = self.session('auto', client)
        obs, _, total, notes = s._auto_advance(frozen_screen(), None)
        self.assertFalse(obs['controllable'])
        self.assertGreaterEqual(total, FROZEN_GIVE_UP_FRAMES)
        self.assertLess(total, FROZEN_GIVE_UP_FRAMES + 60)       # 原来会推满 600 轮 × 30 帧 = 18000 帧
        self.assertTrue(any('停止自动推进' in str(n) for n in notes))

    def test_agent_mode_hands_the_screen_back(self):
        client = ConfirmClient()
        s = self.session('agent', client)
        obs, _, total, notes = s._auto_advance(frozen_screen(), None)
        self.assertEqual(client.steps, [])
        self.assertTrue(any('C 确认' in str(n) for n in notes))

    def test_agent_can_send_confirm_in_auto_mode(self):
        s = self.session('auto', ConfirmClient())
        self.assertEqual(s._check_plan(PlanSpec(inputs=['1,C', '5'])), ['C'] + [''] * 5)

    def test_frozen_screen_does_not_reject_the_agents_act(self):
        client = ConfirmClient(resolve_after=None)
        s = self.session('auto', client)
        notes = s._resume_auto_handling()                        # 原来这里报"游戏还在转场或剧情中"，按键不执行
        self.assertTrue(any('按你的输入执行' in str(n) for n in notes))


if __name__ == '__main__':
    unittest.main()
