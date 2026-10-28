"""auto 剧情模式的最终章片尾：主控用暂停菜单跳过片尾剧情、确认结算、回到选章地图；走不完时交给智能体，
开放确认、返回、暂停等按键，回到选章地图才算完成。"""
import copy
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession, PlanSpec
from orchestrator.metrics import Metrics

SKIP_MENU = {'focused': True, 'selection': 0,
             'items': [{'index': 0, 'id': 'menu_pause_resume', 'selectable': True, 'disabled': False},
                       {'index': 1, 'id': 'menu_pause_skip_cutscene', 'selectable': True, 'disabled': False}]}


def ending_scene():
    return {'scene': 'Level', 'frame': 1000, 'controllable': False, 'storyInput': None, 'storyDecision': 'skip',
            'level': {'sid': 'Celeste/4-GoldenRidge', 'room': 'd-10', 'inCutscene': True, 'skippingCutscene': False,
                      'completed': True, 'dialogOpen': True, 'dialogWaiting': False, 'paused': False, 'canPause': True,
                      'frozen': False, 'transitioning': False}}


class EndingClient:
    """skippable=True：暂停菜单里有"跳过剧情"，跳过后进结算、确认后回到选章地图；False：片尾一直停着。"""

    def __init__(self, skippable=True):
        self.obs = ending_scene()
        self.skippable = skippable
        self.steps = []

    def call(self, cmd, **args):
        if cmd in {'observe', 'activate'}:
            return {'obs': copy.deepcopy(self.obs)}
        if cmd != 'step':
            return {}
        inputs = args['inputs']
        self.steps.append(inputs)
        keys = {k for x in inputs for k in x.split(',')[1:]}
        o = self.obs
        if o['scene'] == 'Level' and 'P' in keys and self.skippable:
            o['pauseMenu'] = copy.deepcopy(SKIP_MENU)
            o['level']['paused'] = True
        elif o.get('pauseMenu') and 'D' in keys:
            o['pauseMenu']['selection'] = 1
        elif o.get('pauseMenu') and 'C' in keys and o['pauseMenu']['selection'] == 1:
            o.pop('pauseMenu')
            o['scene'] = 'AreaComplete'            # 跳过片尾后直接进结算
        elif o['scene'] == 'AreaComplete' and 'C' in keys:
            self.obs = {'scene': 'Overworld', 'menu': {'current': 'OuiChapterSelect', 'next': None}, 'frame': o['frame']}
        elif o['scene'] == 'Overworld' and 'C' in keys:
            raise AssertionError('地图里不能按确认')
        consumed = sum(int(x.split(',')[0]) for x in inputs)
        self.obs['frame'] = self.obs.get('frame', 0) + consumed
        return {'result': {'consumed': consumed, 'planned': consumed, 'stop': 'done', 'events': [], 'trajectory': []},
                'obs': copy.deepcopy(self.obs)}

    def close(self):
        pass


class EndingTests(unittest.TestCase):
    def session(self, client, mode='auto'):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        cfg = RunConfig(agent='ending', save='player', model='test', runtime='host', cutscene_mode=mode)
        s = GameSession(cfg, path, Metrics(path / 'metrics.json', 'test', 'test', 'high', 60))
        self.addCleanup(s.close, False)
        s.client = client
        s.chapter_queue = []
        s.last_obs = ending_scene()
        return s

    def test_final_completion_waits_for_the_ending_instead_of_ending_the_run(self):
        s = self.session(EndingClient())
        s._chapter_complete()
        self.assertFalse(s.done)
        self.assertTrue(s._ending_pending)
        s._chapter_complete()                                     # 同一次通关不会记两次成绩
        self.assertEqual(len(s.metrics.state.splits), 1)

    def test_auto_ending_waits_for_loader_postcard_input_then_confirms(self):
        class PromptClient(EndingClient):
            def __init__(self):
                super().__init__()
                self.stage = 0
                self.obs = {'scene': 'OverworldLoader', 'frame': 10, 'level': None,
                            'ui': {'type': 'Postcard', 'inputReady': False}, 'storyInput': None}

            def call(self, cmd, **args):
                if cmd != 'step':
                    return super().call(cmd, **args)
                inputs = args['inputs']
                self.steps.append(inputs)
                confirm = any(',C' in item for item in inputs)
                if self.stage == 0:
                    if confirm: raise AssertionError('Animation must not receive confirmation')
                    self.obs['storyInput'] = 'postcard'
                    self.obs['ui']['inputReady'] = True
                elif self.stage == 1:
                    if not confirm: raise AssertionError('Ready postcard needs explicit auto-mode confirmation')
                    self.obs = {'scene': 'Overworld', 'frame': 40, 'menu': {'current': 'OuiChapterSelect'}}
                elif confirm:
                    raise AssertionError('Map must not receive confirmation')
                self.stage += 1
                consumed = sum(int(item.split(',')[0]) for item in inputs)
                return {'obs': copy.deepcopy(self.obs), 'result': {'consumed': consumed, 'events': []}}

        client = PromptClient()
        s = self.session(client)
        s._run_ending(client.obs)
        self.assertEqual(client.steps, [['30'], ['1,C', '29'], ['60']])

    def test_skippable_ending_is_skipped_and_results_confirmed(self):
        client = EndingClient()
        s = self.session(client)
        s._chapter_complete()
        obs, _, notes = s._finish_ending()
        self.assertTrue(s.done)
        self.assertEqual(s.done_reason, '章节完成')
        self.assertEqual(obs['scene'], 'Overworld')
        self.assertIn(['1,P'], client.steps)                     # 用暂停菜单跳过，没有空等五分钟
        self.assertLess(sum(int(x.split(',')[0]) for st in client.steps for x in st), 600)
        self.assertTrue(any('回到选章地图' in n for n in notes))

    def test_unfinished_ending_is_handed_to_the_agent(self):
        client = EndingClient(skippable=False)
        s = self.session(client)
        s._chapter_complete()
        obs, _, notes = s._finish_ending()
        self.assertFalse(s.done)
        self.assertTrue(s._ending_agent)
        self.assertTrue(any('act 自行处理' in n for n in notes))
        self.assertEqual(s._check_plan(PlanSpec(inputs=['1,P', '20'])), ['P'] + [''] * 20)   # 暂停键也开放
        self.assertNotIn('complete', s._stop_list(None))                                   # 已通关：不用 complete 截断
        self.assertEqual(s._resume_auto_handling(), [])                                    # 主控不再抢着推进

    def test_stuck_ending_is_handed_over_without_burning_five_minutes(self):
        client = EndingClient(skippable=False)
        s = self.session(client)
        s._chapter_complete()
        s._finish_ending()
        frames = sum(int(x.split(',')[0]) for st in client.steps for x in st)
        self.assertLess(frames, 1500)                             # 原来空等 600 × 30 = 18000 帧

    def test_agent_back_on_the_map_finishes_the_run(self):
        client = EndingClient(skippable=False)
        s = self.session(client)
        s._chapter_complete()
        s._finish_ending()
        client.obs = {'scene': 'Overworld', 'menu': {'current': 'OuiChapterSelect', 'next': None}, 'frame': 5000}
        result, obs, image, game_s, auto_frames, notes = s._execute(
            PlanSpec(inputs=['1,B', '9']), ['B'] + [''] * 9, '回到地图', ['death', 'room'], None, 0)
        self.assertTrue(s.done)
        self.assertEqual(s.done_reason, '章节完成')
        self.assertTrue(any('主控确认本章完成' in n for n in notes))

    def test_user_stop_after_completion_still_finishes_as_chapter_complete(self):
        s = self.session(EndingClient(skippable=False))
        s._chapter_complete()
        s.finish_user_stop()
        self.assertTrue(s.done)
        self.assertEqual(s.done_reason, '章节完成')

    def test_finish_at_results_skips_the_ending_too(self):
        client = EndingClient()
        s = self.session(client)
        s.done, s.done_reason = True, '章节完成'
        s.finish_at_results()
        self.assertEqual(s.end_scene, 'Overworld')
        self.assertIn(['1,P'], client.steps)


if __name__ == '__main__':
    unittest.main()
