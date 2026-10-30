"""主控代为换图（travel）：目标解析、存档解锁进度核对，以及会话里离开关卡、进入目标检查点、回到计划章节的回归。

游戏边界沿用 detour_test 的 DetourGame（按桥接的门禁与暂停菜单规则），只补上 enter_checkpoint 与存档文件位置。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import CHAPTER_SIDS
from orchestrator.game_session import ActError
from orchestrator.metrics import Metrics
from orchestrator.segments import load_table
from orchestrator.travel import (available, chapter_id, describe_points, description, locked_reason, read_progress,
                                 resolve)

import detour_test

DetourGame, HELLO, CORE = detour_test.DetourGame, detour_test.HELLO, detour_test.CORE

OLD_SITE, CITY = CHAPTER_SIDS[2], CHAPTER_SIDS[1]

SAVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<SaveData xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Name>opus5.5</Name>
  <UnlockedAreas>9</UnlockedAreas>
  <Areas>
    <AreaStats ID="1" Cassette="false" SID="Celeste/1-ForsakenCity"><Modes>
      <AreaModeStats Completed="true"><Strawberries /><Checkpoints><string>6</string><string>9b</string></Checkpoints></AreaModeStats>
      <AreaModeStats Completed="false"><Strawberries /><Checkpoints /></AreaModeStats>
    </Modes></AreaStats>
    <AreaStats ID="2" Cassette="false" SID="Celeste/2-OldSite"><Modes>
      <AreaModeStats Completed="true"><Checkpoints><string>3</string><string>end_3</string></Checkpoints></AreaModeStats>
    </Modes></AreaStats>
    <AreaStats ID="3" Cassette="false" SID="Celeste/3-CelestialResort"><Modes>
      <AreaModeStats Completed="false"><Checkpoints><string>08-a</string></Checkpoints></AreaModeStats>
    </Modes></AreaStats>
    <AreaStats ID="9" Cassette="false" SID="Celeste/9-Core"><Modes>
      <AreaModeStats Completed="false"><Checkpoints /></AreaModeStats>
    </Modes></AreaStats>
  </Areas>
</SaveData>
"""


class TravelGame(DetourGame):
    """DetourGame 加上桥接的 enter_checkpoint（从地图进入某章 A 面的检查点）与观测里的存档文件位置。"""

    def __init__(self, save_file, **kw):
        self.save_file = str(save_file)
        self.entered, self.saving_left, self.pending_room = [], 0, None
        super().__init__(**kw)

    def _enter(self, sid, side):
        super()._enter(sid, side)
        if self.pending_room:
            self.room, self.pending_room = self.pending_room, None

    def observation(self):
        obs = super().observation()
        obs['save'] = {**obs['save'], 'file': self.save_file}
        obs['saving'] = self.saving_left > 0
        return obs

    def call(self, command, on_event=None, **options):
        if command == 'hello':
            self.commands.append(command)
            return {**HELLO, 'checkpoints': True}
        if command == 'enter_checkpoint':
            self.commands.append(command)
            assert self.state in {'select', 'panel'}, f'enter_checkpoint 只从地图发出（当前 {self.state}）'
            assert self.saving_left == 0, '存档还在写入时不进入下一张地图'
            chapter, index = options['chapter'], options['checkpoint']
            table = load_table()
            self.entered.append((chapter, index))
            self.pending_room = str(table[chapter][index]['level'])
            self.map, self.state, self.wait = (CHAPTER_SIDS[chapter], 'A'), 'loading', 15
            return {'chapter': chapter, 'checkpoint': index, 'room': self.pending_room}
        if command == 'step' and self.saving_left > 0:
            self.saving_left -= 1
        return super().call(command, on_event, **options)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.table = load_table()

    def test_numbers_names_and_rooms(self):
        self.assertEqual(chapter_id('核心'), 9)
        self.assertEqual(chapter_id('8 核心'), 9)
        self.assertEqual(chapter_id('Core'), 9)
        self.assertEqual(chapter_id(' 2 '), 2)
        dest = resolve(self.table, 2, 'Intervention')
        self.assertEqual((dest.chapter, dest.checkpoint, dest.room), (2, 1, '3'))
        self.assertEqual(resolve(self.table, 6, '04').checkpoint, 2)
        self.assertEqual(resolve(self.table, 6, '洞穴').room, '04')
        self.assertEqual(resolve(self.table, 7, 6).room, 'g-00')
        self.assertEqual(resolve(self.table, 1, 0).room, '1')
        self.assertIn('开头', resolve(self.table, 9, 0).label)

    def test_bad_targets(self):
        for chapter, checkpoint, phrase in ((8, 0, '终章除外'), (11, 0, '没有可去的章节'), (1, 3, '0～2'),
                                            (1, 'nowhere', '没有叫'), ('月球', 0, '不认识的章节'), (True, 0, 'chapter 须为'),
                                            (1, 1.5, 'checkpoint 须为')):
            with self.subTest(chapter=chapter, checkpoint=checkpoint), self.assertRaisesRegex(ValueError, phrase):
                resolve(self.table, chapter, checkpoint)
        self.assertIn('9 = 8 核心', description())
        self.assertIn('1 = 十字路口', describe_points(self.table, 1))

    def test_save_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '8.celeste'
            path.write_text(SAVE_XML, encoding='utf-8')
            progress = read_progress(path)
        self.assertEqual(progress.unlocked_areas, 9)
        self.assertEqual(progress.checkpoints[1], frozenset({'6', '9b'}))
        self.assertIsNone(locked_reason(progress, resolve(self.table, 1, 2)))
        self.assertIsNone(locked_reason(progress, resolve(self.table, 9, 0)))
        self.assertIn('还没有到过', locked_reason(progress, resolve(self.table, 3, 2)))
        self.assertIn('还没有解锁', locked_reason(progress, resolve(self.table, 10, 0)))
        listing = available(self.table, progress)
        self.assertIn('3（3 天空度假村）：0 开头、1 08-a', listing)
        self.assertNotIn('09-d', listing)
        self.assertNotIn('10（', listing)
        with self.assertRaises(ValueError):
            read_progress(None)


class TravelSessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.save_file = Path(self.folder.name) / '8.celeste'
        self.save_file.write_text(SAVE_XML, encoding='utf-8')

    def session(self, **kw):
        helper = detour_test.DetourSessionTests()
        helper._cleanups = []
        self.addCleanup(helper.doCleanups)
        from unittest.mock import patch
        game_holder = {}

        def make_game(*args, **kwargs):
            game = TravelGame(self.save_file)
            game_holder['game'] = game
            return game
        with patch('detour_test.DetourGame', side_effect=make_game):
            session, game = helper.session(**kw)
        self.assertIsInstance(game, TravelGame)
        session.cfg.checkpoints = load_table()
        return session, game

    def test_travel_leaves_by_pause_menu_and_enters_checkpoint_then_returns_to_plan(self):
        session, game = self.session()
        obs, image, notes = session.travel(2, 0, '去旧址找心')
        self.assertEqual(game.entered, [(2, 0)])
        self.assertEqual((obs['scene'], obs['level']['sid'], obs['level']['room']), ('Level', OLD_SITE, 'start'))
        self.assertTrue(obs['controllable'])
        self.assertTrue(any('主控已把你送到2 旧址 A面开头' in n for n in notes), notes)
        self.assertTrue(any('已进入计划外地图2 旧址 A面' in n for n in notes), notes)
        self.assertEqual(session.metrics.state.detour, {'chapter': OLD_SITE, 'side': 'A'})
        # 离开核心用的是主控的离开许可（这一关卡的 departure），不是智能体的返回地图权限
        departures = [p for p in game.policies if p and p.get('departure') == 'current_level']
        self.assertTrue(departures)
        self.assertTrue(all(p['entry_target']['sid'] == OLD_SITE for p in departures))
        # 回到会话自己的门禁：之后智能体的动作照旧用计划外地图的官方地图集合
        self.assertIn('maps', session._step_options()['action_policy']['entry_target'])
        obs, _, notes = session.travel('旧址', 'Intervention', '换个检查点')
        self.assertEqual(game.entered[-1], (2, 1))
        self.assertEqual(obs['level']['room'], '3')
        obs, _, notes = session.travel(9, 0, '回核心')
        self.assertEqual((obs['level']['sid'], obs['level']['room']), (CORE, '00'))
        self.assertTrue(any('已回到计划章节8 核心' in n for n in notes), notes)
        self.assertIsNone(session._detour)
        records = [json.loads(line) for line in (session.run_dir / 'travel.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(r['chapter'], r['checkpoint'], r['arrived']) for r in records], [(2, 0, True), (2, 1, True), (9, 0, True)])
        events = [json.loads(line) for line in (session.run_dir / 'public_events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIn('去旧址找心', [e['text'] for e in events])

    def test_rejections_send_no_input(self):
        session, game = self.session()
        cases = ((3, 2, '还没有到过'), (10, 0, '还没有解锁'), (9, 0, '等于重启章节'), (1, 7, '序号只有'), (8, 0, '终章除外'))
        for chapter, checkpoint, phrase in cases:
            with self.subTest(chapter=chapter):
                steps = len(game.steps)
                with self.assertRaisesRegex(ActError, phrase):
                    session.travel(chapter, checkpoint, '试试')
                self.assertEqual(len(game.steps), steps)
                self.assertEqual(game.entered, [])
        with self.assertRaisesRegex(ActError, 'thought 不能为空'):
            session.travel(1, 0, ' ')
        self.save_file.write_text('not xml', encoding='utf-8')
        with self.assertRaisesRegex(ActError, '读不到存档'):
            session.travel(1, 0, '试试')

    def test_switch_off_limits_travel_to_plan(self):
        session, game = self.session()
        session.travel(1, 'Crossing', '去城里')
        session.cfg.allow_detour = False
        with self.assertRaisesRegex(ActError, '只能回到计划章节'):
            session.travel(2, 0, '再去旧址')
        obs, _, _ = session.travel(9, 0, '回核心')
        self.assertEqual(obs['level']['sid'], CORE)

    def test_waits_for_save_before_entering(self):
        session, game = self.session()
        game.saving_left = 0
        original = game._press

        def press(key, policy, events):
            block = original(key, policy, events)
            if game.state == 'exit':
                game.saving_left = 25          # 返回地图后游戏还在写存档
            return block
        game._press = press
        session.travel(1, 0, '去城里')
        self.assertEqual(game.saving_left, 0)
        self.assertEqual(game.entered, [(1, 0)])


class TravelEdgeTests(TravelSessionTests):
    """审查发现的情形：换下一截、通关待确认、片尾交给智能体、计划章节会话计时。"""

    def finish_core_agent(self, session):
        session.act(['40,R'], '通关核心', None)
        session.act(['1,P', '20'], '打开暂停菜单', None)
        session.act(['1,D', '1,C', '5'], '跳过片尾', None)

    def test_next_piece_entered_by_travel_is_confirmed(self):
        RESORT = CHAPTER_SIDS[3]
        session, game = self.session(mode='agent', chapters=(9, 3))
        self.finish_core_agent(session)
        session.act(['1,C', '5'], '确认结算', None)
        self.assertTrue(session.chapter_finished)
        obs, _, notes = session.travel(3, 0, '去下一截')
        self.assertFalse(session.chapter_finished)
        self.assertEqual(session.piece.chapter, 3)
        self.assertIsNone(session._detour)
        self.assertTrue(any('已确认你进入' in n for n in notes), notes)
        game.next_room = 'zz-02'
        session.act(['1,G', '39'], '过第一个房间', None)
        session.act(['40,R'], '通关度假村', None)
        session.act(['1,P', '20'], 'p', None)
        session.act(['1,D', '1,C', '5'], 'skip', None)
        session.act(['1,C', '5'], 'confirm', None)
        self.assertTrue(session.done)
        self.assertEqual([s['chapter'] for s in session.metrics.state.splits], [CORE, RESORT])

    def test_pending_completion_or_ending_refuses_without_input(self):
        session, game = self.session(mode='agent', chapters=(9, 3))
        self.finish_core_agent(session)
        self.assertTrue(session._pending_complete)
        steps = len(game.steps)
        for chapter in (3, 9, 1):
            with self.assertRaisesRegex(ActError, '还在收尾'):
                session.travel(chapter, 0, '直接走')
        self.assertEqual(len(game.steps), steps)
        auto, auto_game = self.session(chapters=(9,))
        auto._ending_agent = True
        with self.assertRaisesRegex(ActError, '还在收尾'):
            auto.travel(3, 0, '去度假村')
        self.assertEqual(auto_game.entered, [])

    def test_agent_mode_results_screen_is_left_to_agent(self):
        session, game = self.session(mode='agent', chapters=(9, 3))
        self.finish_core_agent(session)
        session._pending_complete = False         # 只看结算画面这一条：agent 模式由它自己确认
        with self.assertRaisesRegex(ActError, '结算画面'):
            session.travel(1, 0, '去城里')

    def test_segment_end_cannot_be_reached_by_travel(self):
        from orchestrator.segments import Piece
        session, game = self.session(chapters=(9,), plan=[Piece(9, 0, 2)], checkpoints=load_table())
        session.travel(1, 0, '去城里')
        steps = len(game.steps)
        with self.assertRaisesRegex(ActError, '不能直接进到终点'):
            session.travel(9, 2, '直接去终点')
        self.assertEqual(len(game.steps), steps)
        obs, _, _ = session.travel(9, 0, '回到这一截起点')
        self.assertEqual(obs['level']['room'], '00')

    def test_leaving_plan_banks_its_session_time(self):
        session, game = self.session()
        session.act(['60'], '先走一段', None)
        before = int(session.last_obs['level']['chapterTimeTicks'])
        self.assertGreater(before, 0)
        session.travel(1, 0, '去城里')
        self.assertEqual(int(session.metrics.state.plan_banked_ticks), before)
        self.assertEqual(int(session.metrics.state.plan_session_ticks), 0)
        session.travel(9, 0, '回核心')
        self.assertEqual(int(session.metrics.state.plan_banked_ticks), before)   # 回来时不再重复累加


class TravelToolTests(unittest.TestCase):
    def test_registered_only_with_detour(self):
        from orchestrator.modes import game_tools
        from orchestrator.server import build_server
        from orchestrator.config import RunConfig
        from orchestrator.game_session import GameSession
        for detour in (False, True):
            with self.subTest(detour=detour), tempfile.TemporaryDirectory() as folder:
                cfg = RunConfig(agent='opus5.5', save='opus5.5', model='test', runtime='host', chapter=9, chapters=[9],
                                mode='explore', knowledge_profile='explore', sim=False)
                cfg.allow_detour = detour
                self.assertEqual('travel' in game_tools(cfg), detour)
                session = GameSession(cfg, Path(folder), Metrics(Path(folder) / 'm.json', 'a', 'b', 'high', 60))
                try:
                    tools = {t.name: t for t in asyncio.run(build_server(session).list_tools())}
                finally:
                    session.close(release=False)
                self.assertEqual('travel' in tools, detour)
                self.assertEqual(session.travel_tool, detour)
                if detour:
                    self.assertIn('只限存档里已解锁章节的 A 面', tools['travel'].description)
                    self.assertEqual(set(tools['travel'].input_schema['required']), {'chapter', 'thought'})


if __name__ == '__main__':
    unittest.main()
