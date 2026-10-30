"""主控代为换图（travel）：目标解析、存档解锁进度核对，以及会话里离开关卡、进入目标检查点、回到计划章节的回归。

游戏边界沿用 detour_test 的 DetourGame（按桥接的门禁与暂停菜单规则），补上按反编译代码建模的选章页与章节面板、存档文件位置。
"""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import CHAPTER_SIDS
from orchestrator.game_session import ActError
from orchestrator.metrics import Metrics
from orchestrator.segments import load_table
from orchestrator.travel import (available, chapter_id, describe_points, description, locked_reason, panel_position,
                                 read_progress, resolve)

import detour_test

DetourGame, HELLO, CORE = detour_test.DetourGame, detour_test.HELLO, detour_test.CORE

OLD_SITE, CITY, RESORT = CHAPTER_SIDS[2], CHAPTER_SIDS[1], CHAPTER_SIDS[3]

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


AREA_OF = {sid: ident for ident, sid in CHAPTER_SIDS.items()}

# 游戏内存里的存档：章节编号 → [sid, A 面已通关, A 面到过的检查点（记录顺序）, 章节面板里有的面]
AREAS = {
    1: [CITY, True, ['6', '9b'], ['Normal']],
    2: [OLD_SITE, True, ['3', 'end_3'], ['Normal', 'BSide']],
    3: [RESORT, False, ['08-a'], ['Normal']],
    9: [CORE, False, [], ['Normal']],
}


def save_xml(unlocked, areas):
    rows = []
    for ident, (sid, completed, rooms, _) in sorted(areas.items()):
        points = ''.join(f'<string>{room}</string>' for room in rooms)
        rows.append(f'<AreaStats ID="{ident}" Cassette="false" SID="{sid}"><Modes><AreaModeStats '
                    f'Completed="{str(completed).lower()}"><Checkpoints>{points}</Checkpoints></AreaModeStats></Modes></AreaStats>')
    return (f'<?xml version="1.0" encoding="utf-8"?><SaveData><Name>opus5.5</Name><UnlockedAreas>{unlocked}'
            f'</UnlockedAreas><Areas>{"".join(rows)}</Areas></SaveData>')


class TravelGame(DetourGame):
    """DetourGame 加上原生选章地图，按反编译的 OuiChapterSelect／OuiChapterPanel：
    选章页左右移光标、确认打开面板；面板先选面（A 面在最左），确认后半秒的切换动画里（第 15 帧换成检查点列表）不接受按键，
    存档里这一章没到过检查点时确认直接进入；检查点列表第一项是开头，之后按存档记录的顺序，A 面没通关时光标停在最后一项；
    确认后面板立刻不再接受按键，0.7 秒后开始载入。返回地图（AreaQuit）后停在刚离开那一章的面板，先播成绩动画，期间按键被忽略；
    存档写完才落盘（save_frames）。观测里带存档文件位置与 saving。"""
    STATS, SWAP, FLIP, OPEN, START = 50, 30, 15, 10, 42
    # 关卡里的暂停菜单，与用户游戏里桥接报告的一致（Level.Pause 的顺序，Everest 加了"Mod 选项"）：(id, 可选)。
    # 重来在不能重生时变灰（Disabled）；上下移动跳过不可选和变灰的项，可停的项多于 2 个时到头会绕回（TextMenu.MoveSelection）
    PAUSE = [('menu_pause_title', False), ('menu_pause_resume', True), ('menu_pause_retry', True), ('menu_pause_options', True),
             (None, True), ('menu_pause_savequit', True), (None, False), ('menu_pause_restartarea', True),
             ('menu_pause_return', True)]

    def __init__(self, save_file, areas=None, unlocked=9, **kw):
        self.save_file, self.unlocked = str(save_file), unlocked
        self.areas = copy.deepcopy(AREAS if areas is None else areas)
        self.entered, self.saving_left, self.save_frames, self.write_on_save = [], 0, 0, True
        self.pending_room, self.area, self.panel_area, self.panel_side, self.selecting = None, 0, 0, 'Normal', True
        self.cp_list, self.cp_index, self.busy, self.flip_left, self.menu_move, self.starting = [], 0, 0, 0, None, None
        self.ignored = self.keys_while_saving = 0
        self.retry_disabled = False
        super().__init__(**kw)
        self.write_save()

    def _greyed(self, index):
        return self.PAUSE[index][0] == 'menu_pause_retry' and self.retry_disabled

    def move(self, index, delta):
        hoverable = [i for i, (_, selectable) in enumerate(self.PAUSE) if selectable and not self._greyed(i)]
        wrap = len(hoverable) > 2
        while True:
            index += delta
            if wrap:
                index %= len(self.PAUSE)
            elif not 0 <= index < len(self.PAUSE):
                return max(0, min(len(self.PAUSE) - 1, index))
            if index in hoverable:
                return index

    def write_save(self):
        Path(self.save_file).write_text(save_xml(self.unlocked, self.areas), encoding='utf-8')

    def reached(self, area):
        return list(self.areas.get(area, [None, False, [], ['Normal']])[2]) if self.panel_side == 'Normal' else []

    def _enter(self, sid, side):
        super()._enter(sid, side)
        if self.pending_room:
            self.room, self.pending_room = self.pending_room, None

    def _open_panel(self, area, side, stats):
        self.state, self.panel_area, self.panel_side, self.selecting = 'panel', area, side, True
        self.busy, self.flip_left = stats, 0

    def observation(self):
        obs = super().observation()
        obs['save'] = {**obs['save'], 'file': self.save_file}
        obs['saving'] = self.saving_left > 0
        if self.state == 'pause' and not self.completed:
            items = []
            for i, (ident, selectable) in enumerate(self.PAUSE):
                item = {'index': i, 'selectable': selectable, 'disabled': self._greyed(i)}
                if ident:
                    item['id'] = ident
                items.append(item)
            obs['pauseMenu'] = {'focused': True, 'selection': self.selection, 'items': items}
        if self.state == 'select':
            obs['menu'] = {'current': 'OuiChapterSelect', 'next': 'OuiChapterPanel' if self.menu_move else None,
                           'area': self.area}
        elif self.state == 'panel':
            obs['menu'] = {'current': 'OuiChapterPanel', 'next': 'OuiChapterSelect' if self.menu_move else None,
                           'panelArea': self.panel_area, 'panelSide': self.panel_side, 'panelSelectingMode': self.selecting,
                           'panelCheckpoint': self.cp_index, 'panelCheckpointCount': len(self.cp_list)}
        return obs

    def _press(self, key, policy, events):
        if self.state in {'select', 'panel'} and self.saving_left > 0:
            self.keys_while_saving += 1
        if self.state == 'results' and key == 'C':
            self.state, self.area = 'select', AREA_OF[self.map[0]]
            return None
        if self.state == 'level' and key == 'P' and not self.completed:
            block = super()._press(key, policy, events)
            self.selection = 1                       # 第一项是标题，光标停在"继续游戏"
            return block
        if self.state == 'pause' and not self.completed:
            self.menu_keys.append(('pause', key))
            ident = self.PAUSE[self.selection][0]
            if key in {'U', 'D'}:
                self.selection = self.move(self.selection, 1 if key == 'D' else -1)
            elif key == 'B' or (key == 'C' and ident == 'menu_pause_resume'):
                self.state = 'level'
            elif key == 'C' and ident == 'menu_pause_return':
                self.state = 'confirm'
            return None
        if self.state == 'select':
            self.menu_keys.append(('select', key))
            if self.menu_move:
                self.ignored += 1
            elif key == 'L' and self.area > 0:
                self.area -= 1
            elif key == 'R' and self.area < self.unlocked:
                self.area += 1
            elif key == 'C':
                self.menu_move = ['panel', self.OPEN]
            return None
        if self.state != 'panel':
            return super()._press(key, policy, events)
        self.menu_keys.append(('panel', key))
        if self.busy or self.menu_move:
            self.ignored += 1
            return None
        sides = self.areas.get(self.panel_area, [None, False, [], ['Normal']])[3]
        if key in {'L', 'R'}:
            delta = 1 if key == 'R' else -1
            if self.selecting:
                index = sides.index(self.panel_side) + delta
                if 0 <= index < len(sides):
                    self.panel_side = sides[index]
            elif 0 <= self.cp_index + delta < len(self.cp_list):
                self.cp_index += delta
        elif key == 'C':
            if self.selecting and self.reached(self.panel_area):
                self.busy, self.flip_left = self.SWAP, self.FLIP
                return None
            side = 'A' if self.panel_side == 'Normal' else 'B'
            block = self._entry_block(policy, CHAPTER_SIDS[self.panel_area], side)
            if block:
                return block
            room = None if self.selecting else self.cp_list[self.cp_index]
            self.busy, self.starting = 10 ** 9, [self.START, self.panel_area, side, room]
        elif key == 'B':
            if self.selecting:
                self.menu_move = ['select', self.OPEN]
            else:
                self.busy, self.flip_left = self.SWAP, self.FLIP
        return None

    def _frame(self, before, events):
        if self.state == 'exit' and before != 'exit':
            self.saving_left = self.save_frames
        if self.saving_left > 0:
            self.saving_left -= 1
            if self.saving_left == 0 and self.write_on_save:
                self.write_save()
        if 0 < self.busy < 10 ** 9:
            self.busy -= 1
        if self.flip_left:
            self.flip_left -= 1
            if not self.flip_left:
                self.selecting = not self.selecting
                if not self.selecting:
                    rooms = {str(p['level']) for p in load_table()[self.panel_area][1:]}
                    self.cp_list = [None] + [room for room in self.reached(self.panel_area) if room in rooms]
                    self.cp_index = 0 if self.areas[self.panel_area][1] else len(self.cp_list) - 1
        if self.menu_move:
            self.menu_move[1] -= 1
            if self.menu_move[1] <= 0:
                target, self.menu_move = self.menu_move[0], None
                if target == 'panel':
                    self._open_panel(self.area, 'Normal', 0)
                else:
                    self.state, self.area = 'select', self.panel_area
        if self.starting:
            self.starting[0] -= 1
            if self.starting[0] <= 0:
                _, area, side, room = self.starting
                room = room or str(load_table()[area][0]['level'])
                self.starting, self.busy = None, 0
                self.entered.append((area, room))
                self.pending_room = room
                self.map, self.state, self.wait = (CHAPTER_SIDS[area], side), 'loading', 15

    def _wait_done(self, events):
        if self.state != 'exit':
            return super()._wait_done(events)
        sid, side = self.map
        self._open_panel(AREA_OF[sid], 'Normal' if side == 'A' else 'BSide', self.STATS)
        if self.saving_left == 0 and self.write_on_save:
            self.write_save()

    def call(self, command, on_event=None, **options):
        assert command != 'enter_checkpoint', '换图要按玩家的菜单步骤走，不能用桥接的 enter_checkpoint'
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
        self.assertEqual(progress.checkpoints[1], ('6', '9b'))
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

    def test_panel_position_follows_save_order(self):
        """面板第一项是开头，之后按存档记录的顺序（游戏 OuiChapterPanel._GetCheckpoints），去掉本章没有的房间。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '8.celeste'
            areas = {**AREAS, 1: [CITY, False, ['9b', 'nowhere', '6'], ['Normal']]}
            path.write_text(save_xml(9, areas), encoding='utf-8')
            progress = read_progress(path)
        self.assertEqual(progress.checkpoints[1], ('9b', 'nowhere', '6'))
        self.assertEqual(panel_position(self.table, progress, resolve(self.table, 1, 1)), (2, 3))
        self.assertEqual(panel_position(self.table, progress, resolve(self.table, 1, '9b')), (1, 3))
        self.assertEqual(panel_position(self.table, progress, resolve(self.table, 1, 0)), (0, 3))
        self.assertEqual(panel_position(self.table, progress, resolve(self.table, 9, 0)), (0, 1))
        with self.assertRaisesRegex(ValueError, '还没有到过'):
            panel_position(self.table, progress, resolve(self.table, 3, 2))


class TravelSessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.save_file = Path(self.folder.name) / '8.celeste'
        self.areas = None

    def session(self, **kw):
        helper = detour_test.DetourSessionTests()
        helper._cleanups = []
        self.addCleanup(helper.doCleanups)
        from unittest.mock import patch
        game_holder = {}

        def make_game(*args, **kwargs):
            game = TravelGame(self.save_file, self.areas)
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
        self.assertEqual(game.entered, [(2, 'start')])
        self.assertEqual((obs['scene'], obs['level']['sid'], obs['level']['room']), ('Level', OLD_SITE, 'start'))
        self.assertTrue(obs['controllable'])
        self.assertTrue(any('主控已把你送到2 旧址 A面开头' in n for n in notes), notes)
        self.assertTrue(any('已进入计划外地图2 旧址 A面' in n for n in notes), notes)
        self.assertEqual(session.metrics.state.detour, {'chapter': OLD_SITE, 'side': 'A'})
        # 按玩家的步骤：返回地图后停在核心的面板（成绩动画期间按键被忽略，重按）→ 退回选章页 → 左移 7 次到旧址 →
        # 打开面板 → 确认 A 面 → 检查点列表里光标已在开头（旧址通关过）→ 确认进入
        self.assertEqual([k for k in game.menu_keys if k[0] == 'select'], [('select', 'L')] * 7 + [('select', 'C')])
        panel = [k for _, k in game.menu_keys[game.menu_keys.index(('select', 'C')) + 1:]]
        self.assertEqual(panel, ['C', 'C'])
        self.assertIn(('panel', 'B'), game.menu_keys)
        self.assertGreater(game.ignored, 0)
        # 离开核心用的是主控的离开许可（这一关卡的 departure），不是智能体的返回地图权限
        departures = [p for p in game.policies if p and p.get('departure') == 'current_level']
        self.assertTrue(departures)
        self.assertTrue(all(p['entry_target']['sid'] == OLD_SITE for p in departures))
        # 回到会话自己的门禁：之后智能体的动作照旧用计划外地图的官方地图集合
        self.assertIn('maps', session._step_options()['action_policy']['entry_target'])
        game.menu_keys.clear()
        obs, _, notes = session.travel('旧址', 'Intervention', '换个检查点')
        self.assertEqual(game.entered[-1], (2, '3'))
        self.assertEqual(obs['level']['room'], '3')
        self.assertNotIn(('select', 'C'), game.menu_keys)       # 返回地图就停在旧址的面板，不用回选章页
        self.assertEqual([k for _, k in game.menu_keys][-2:], ['R', 'C'])
        obs, _, notes = session.travel(9, 0, '回核心')
        self.assertEqual((obs['level']['sid'], obs['level']['room']), (CORE, '00'))
        self.assertEqual(game.entered[-1], (9, '00'))           # 核心存档里没到过检查点：选 A 面确认即进入
        self.assertTrue(any('已回到计划章节8 核心' in n for n in notes), notes)
        self.assertIsNone(session._detour)
        records = [json.loads(line) for line in (session.run_dir / 'travel.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(r['chapter'], r['checkpoint'], r['arrived']) for r in records], [(2, 0, True), (2, 1, True), (9, 0, True)])
        events = [json.loads(line) for line in (session.run_dir / 'public_events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIn('去旧址找心', [e['text'] for e in events])

    def test_panel_order_follows_save_and_unfinished_chapter_starts_at_latest(self):
        self.areas = {**AREAS, 3: [RESORT, False, ['09-d', '08-a'], ['Normal']]}
        session, game = self.session()
        obs, _, _ = session.travel(3, '08-a', '去度假村')
        self.assertEqual(game.entered, [(3, '08-a')])
        tail = [k for _, k in game.menu_keys[game.menu_keys.index(('select', 'C')) + 1:]]
        self.assertEqual(tail, ['C', 'C'])                       # 没通关：列表光标停在最后一项，正好是 08-a
        game.menu_keys.clear()
        obs, _, notes = session.travel(3, 2, '去电梯井')
        self.assertEqual(game.entered[-1], (3, '09-d'))
        self.assertEqual(obs['level']['room'], '09-d')
        self.assertEqual([k for _, k in game.menu_keys][-3:], ['C', 'L', 'C'])
        self.assertFalse(any('注意：进到的房间' in n for n in notes), notes)

    def test_rereads_save_written_on_return_to_map(self):
        session, game = self.session()
        session.travel(3, 0, '去度假村')
        self.assertEqual(game.entered, [(3, 's0')])
        game.areas[3][2].append('09-d')          # 这次在度假村新到的检查点：游戏内存里有，返回地图写存档时才落盘
        game.save_frames = 80                      # 回到地图之后存档还要写一会儿
        obs, _, notes = session.travel(3, '08-a', '回检查点')
        self.assertEqual(game.keys_while_saving, 0)
        self.assertEqual(game.entered[-1], (3, '08-a'))
        self.assertEqual(obs['level']['room'], '08-a')
        self.assertTrue(any('主控已把你送到' in n for n in notes), notes)

    def test_panel_listing_mismatch_stops_without_entering(self):
        session, game = self.session()
        session.travel(3, 0, '去度假村')
        game.areas[3][2].append('09-d')
        game.write_on_save = False                 # 面板按内存列出 3 项，存档文件里只有 2 项：不猜，停下
        obs, _, notes = session.travel(3, '08-a', '回检查点')
        self.assertEqual(game.entered, [(3, 's0')])
        self.assertIn('主控换图没有完成', notes[0])
        self.assertIn('列出 3 项', notes[0])
        self.assertEqual((obs['scene'], obs['menu']['current']), ('Overworld', 'OuiChapterPanel'))
        record = json.loads((session.run_dir / 'travel.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertFalse(record['arrived'])

    def test_return_to_map_with_retry_greyed_out(self):
        """2A 吃心之后有一段不能重生：暂停菜单里"重来"变灰，上下移动跳过它，"返回地图"从 6 下变成 5 下；
        按固定 6 下会绕回"继续游戏"（智能体自己操作时遇到过）。主控按选中项的位置走，不按固定次数。"""
        session, game = self.session()
        session.travel(2, 0, '去旧址')
        game.retry_disabled = True
        at = 1
        for _ in range(6):
            at = game.move(at, 1)
        self.assertEqual(game.PAUSE[at][0], 'menu_pause_resume')      # 替身复现了固定 6 下的问题
        game.menu_keys.clear()
        obs, _, notes = session.travel(1, 0, '回城里')
        self.assertEqual(game.entered[-1], (1, '1'))
        self.assertEqual([k for where, k in game.menu_keys if where == 'pause'], ['D'] * 5 + ['C'])
        game.retry_disabled = False
        game.menu_keys.clear()
        session.travel(2, 0, '再去旧址')
        self.assertEqual([k for where, k in game.menu_keys if where == 'pause'], ['D'] * 6 + ['C'])

    def test_b_side_panel_is_switched_to_a_side(self):
        session, game = self.session()
        session.travel(2, 0, '去旧址')
        game.map = (OLD_SITE, 'B')                 # 假设智能体随后去了旧址 B 面：返回地图后面板停在 B 面
        game.menu_keys.clear()
        obs, _, _ = session.travel(2, 1, '回 A 面的检查点')
        self.assertEqual(game.entered[-1], (2, '3'))
        self.assertEqual([k for _, k in game.menu_keys if k != 'B'][-4:], ['L', 'C', 'R', 'C'])

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
        self.assertEqual(game.entered, [(1, '6')])
        session.cfg.allow_detour = False
        with self.assertRaisesRegex(ActError, '只能回到计划章节'):
            session.travel(2, 0, '再去旧址')
        obs, _, _ = session.travel(9, 0, '回核心')
        self.assertEqual(obs['level']['sid'], CORE)

    def test_waits_for_save_before_entering(self):
        session, game = self.session()
        game.save_frames = 90
        session.travel(1, 0, '去城里')
        self.assertEqual(game.saving_left, 0)
        self.assertEqual(game.keys_while_saving, 0)
        self.assertEqual(game.entered, [(1, '1')])


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
