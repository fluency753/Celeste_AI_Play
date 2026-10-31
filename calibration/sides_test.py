"""官方章节的 B／C 面：计划解析、存档解锁核对、菜单宏选面、完成判定与计划外判定、续跑追加、控制台选项的回归。

游戏边界只用替身：会话沿用 detour_test 的 DetourGame（按桥接门禁、暂停菜单与通关规则），菜单宏用 PanelGame
（按反编译的 OuiChapterPanel 建模选面与检查点列表）。主控的解析、核对、记账与菜单宏都走生产代码。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import CHAPTER_SIDS, chapter_name_by_id, parse_chapter_item, side_code, side_label
from orchestrator.launch_options import build, command_line, normalize
from orchestrator.saves import side_locked_reason, side_progress, validate_sides
from orchestrator.segments import (Piece, load_table, parse_chapters, parse_segments, piece_label, pieces_of,
                                   plan_has_sides, validate_segments)
from tools.enter_save import enter_save

import detour_test

DetourGame = detour_test.DetourGame
CORE, RIDGE, CITY = CHAPTER_SIDS[9], CHAPTER_SIDS[4], CHAPTER_SIDS[1]
SIDE_CHAPTERS = (1, 2, 3, 4, 5, 6, 7, 9)


def save_xml(cassettes=(), b_hearts=(), cheat=None):
    """存档 XML（只写主控读的字段）：哪些章拿到了磁带、哪些章的 B 面拿到了心。"""
    rows = []
    for ident, sid in CHAPTER_SIDS.items():
        modes = ''.join(f'<AreaModeStats HeartGem="{str(i == 1 and ident in b_hearts).lower()}"><Checkpoints /></AreaModeStats>'
                        for i in range(3))
        rows.append(f'<AreaStats ID="{ident}" Cassette="{str(ident in cassettes).lower()}" SID="{sid}"><Modes>{modes}</Modes></AreaStats>')
    cheat_xml = '' if cheat is None else f'<CheatMode>{str(cheat).lower()}</CheatMode>'
    return (f'<?xml version="1.0" encoding="utf-8"?><SaveData><Name>s</Name>{cheat_xml}<UnlockedAreas>10</UnlockedAreas>'
            f'<Areas>{"".join(rows)}</Areas></SaveData>')


class ParseTests(unittest.TestCase):
    def test_chapter_items(self):
        self.assertEqual(parse_chapter_item('4'), (4, 'A'))
        self.assertEqual(parse_chapter_item('4b'), (4, 'B'))
        self.assertEqual(parse_chapter_item(' 8C '), (9, 'C'))       # 8 是核心（内部编号 9）
        self.assertEqual(parse_chapter_item('1A'), (1, 'A'))
        self.assertEqual(parse_chapter_item('10'), (10, 'A'))
        for bad, phrase in (('0B', '没有'), ('9B', '没有'), ('10C', '没有'), ('4D', '不对'), ('xB', '不对'), ('0A', '纯编号')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, phrase):
                parse_chapter_item(bad)

    def test_plan_pieces_and_rows(self):
        plan = parse_chapters('0,4B,8c')
        self.assertEqual(plan, [Piece(0), Piece(4, 0, None, 'B'), Piece(9, 0, None, 'C')])
        self.assertTrue(plan_has_sides(plan))
        self.assertFalse(plan_has_sides(parse_chapters('0,1,2')))
        self.assertEqual([p.row() for p in plan], [[0, 0, None], [4, 0, None, 'B'], [9, 0, None, 'C']])
        self.assertEqual(Piece.from_row([4, 0, None]), Piece(4))          # 旧运行没有面：按 A 面
        self.assertEqual(Piece.from_row([4, 0, None, 'B']), Piece(4, 0, None, 'B'))
        with self.assertRaises(ValueError):
            Piece.from_row([4, 0, None, 'D'])
        self.assertEqual(Piece(4, 0, None, 'B').segments(), [(4, 0, 'B')])

    def test_names_follow_the_game(self):
        self.assertEqual(side_label(9, 'B'), '8B 核心 B 面')
        self.assertEqual(side_label(4, 'C'), '4C 黄金山脊 C 面')
        self.assertEqual(side_code(9, 'C'), '8C')
        self.assertEqual(chapter_name_by_id(4), '4 黄金山脊')               # A 面与原来相同
        self.assertEqual(chapter_name_by_id(4, 'B'), '4 黄金山脊 B 面')
        self.assertEqual(piece_label(Piece(9, 0, None, 'B'), {}), '8 核心 B 面')

    def test_segments_reject_sides_and_keep_order(self):
        table = load_table()
        with self.assertRaisesRegex(ValueError, '只支持 A 面'):
            parse_segments('4B', table)
        self.assertEqual(pieces_of(validate_segments([[4, 0, 'B'], [4, 0], [4, 1], [1, 0]], table), table),
                         [Piece(1, 0, 1), Piece(4, 0, 2), Piece(4, 0, None, 'B')])
        for bad in ([[0, 0, 'B']], [[4, 1, 'B']], [[4, 0, 'D']]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_segments(bad, table)


class UnlockTests(unittest.TestCase):
    def progress(self, **kw):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '3.celeste'
            path.write_text(save_xml(**kw), encoding='utf-8')
            return side_progress(path)

    def test_b_side_needs_the_cassette(self):
        progress = self.progress(cassettes={4})
        self.assertIsNone(side_locked_reason(progress, 4, 'B'))
        self.assertIn('磁带', side_locked_reason(progress, 5, 'B'))
        self.assertIsNone(side_locked_reason(progress, 5, 'A'))
        self.assertIn('没有 B 面', side_locked_reason(progress, 10, 'B'))
        self.assertIsNone(side_locked_reason(self.progress(cheat=True), 5, 'B'))    # 作弊模式：全部开放

    def test_c_side_needs_all_eight_b_side_hearts(self):
        all_hearts = self.progress(b_hearts=set(SIDE_CHAPTERS))
        self.assertIsNone(side_locked_reason(all_hearts, 4, 'C'))
        self.assertIsNone(side_locked_reason(all_hearts, 9, 'C'))
        missing = self.progress(b_hearts=set(SIDE_CHAPTERS) - {9}, cassettes=set(SIDE_CHAPTERS))
        reason = side_locked_reason(missing, 4, 'C')
        self.assertIn('8 章的 B 面都拿到心', reason)
        self.assertIn('8 核心的 B 面', reason)
        self.assertIsNone(side_locked_reason(self.progress(cheat=True), 9, 'C'))
        self.assertIn('没有 C 面', side_locked_reason(all_hearts, 0, 'C'))

    def test_validate_sides_reads_only_when_needed(self):
        with patch('orchestrator.saves.save_file', side_effect=AssertionError('只有 A 面时不读存档')):
            validate_sides([Piece(1), Piece(2)], create=True)
        with self.assertRaisesRegex(ValueError, '新建存档不能计划'):
            validate_sides([Piece(4, 0, None, 'B')], create=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '3.celeste'
            path.write_text(save_xml(cassettes={4}), encoding='utf-8')
            with patch('orchestrator.saves.save_file', return_value=path):
                validate_sides([Piece(4, 0, None, 'B')], create=False, slot=3)
                with self.assertRaisesRegex(ValueError, 'C 面在这个存档里还没有解锁'):
                    validate_sides([Piece(4, 0, None, 'B'), Piece(4, 0, None, 'C')], create=False, slot=3)
            with patch('orchestrator.saves.save_file', return_value=Path(folder) / 'missing.celeste'), \
                    self.assertRaisesRegex(ValueError, '读不到存档'):
                validate_sides([Piece(4, 0, None, 'B')], create=False, slot=3)
            with patch('orchestrator.saves.inventory', return_value=[{'slot': 1, 'name': 's'}, {'slot': 2, 'name': 's'}]), \
                    self.assertRaisesRegex(ValueError, '槽位'):
                validate_sides([Piece(4, 0, None, 'B')], create=False, name='s')


class PanelGame:
    """菜单宏的游戏替身：选章地图上章节面板的原生行为（按反编译的 OuiChapterPanel）。
    - 打开面板时停在 A 面、选面状态；从关卡返回地图（AreaQuit）时面板停在刚玩的那一面，先播成绩动画，期间按键被忽略。
    - 选面：左右在列表里的面之间移动；B 面只在这一章有磁带时在列表里，C 面只在 UnlockedModes ≥ 3 时在列表里。
    - 选面状态下确认：这一面存档里没到过检查点就直接开始，否则切到检查点列表（切换动画期间不接受按键）；
      列表第一项是开头，这一面没通关时光标在最后一项，通关过在开头。列表里确认就从选中的检查点开始。
    - 确认开始后不再接受按键，START 帧后进入关卡。关卡里暂停菜单返回地图。"""
    SWAP, START, STATS = 30, 42, 50

    def __init__(self, *, cassette=True, c_unlocked=True, checkpoints=(), completed=False, start=None, save='s', slot=3):
        self.cassette, self.c_unlocked = cassette, c_unlocked
        self.checkpoints, self.completed = list(checkpoints), completed
        self.save, self.slot, self.frame = save, slot, 0
        self.scene, self.side, self.room, self.area = 'select', 'Normal', None, 4
        self.selecting, self.cp, self.busy, self.starting, self.leaving = True, 0, 0, None, 0
        self.paused, self.pause_sel, self.ignored, self.keys = False, 0, 0, []
        if start is not None:
            self.scene, self.area, self.side = 'level', start[0], start[1]

    def sides(self):
        return ['Normal'] + (['BSide'] if self.cassette else []) + (['CSide'] if self.c_unlocked else [])

    def obs(self):
        base = {'frame': self.frame, 'save': {'name': self.save, 'slot': self.slot}, 'storyInput': None, 'story': []}
        if self.scene == 'level':
            side = {'Normal': 'A', 'BSide': 'B', 'CSide': 'C'}[self.side]
            obs = {**base, 'scene': 'Level', 'controllable': not self.paused,
                   'level': {'sid': CHAPTER_SIDS[self.area], 'side': side, 'room': 'a-00', 'inCutscene': False,
                             'skippingCutscene': False, 'paused': self.paused, 'completed': False, 'dialogChoices': []}}
            if self.paused:
                obs['pauseMenu'] = {'focused': True, 'selection': self.pause_sel, 'items': [
                    {'index': 0, 'id': 'menu_pause_resume', 'selectable': True, 'disabled': False},
                    {'index': 1, 'id': 'menu_pause_return', 'selectable': True, 'disabled': False}]}
            return obs
        if self.scene in ('loading', 'exit'):
            return {**base, 'scene': 'LevelLoader' if self.scene == 'loading' else 'LevelExit', 'level': None}
        menu = ({'current': 'OuiChapterSelect', 'next': None, 'area': self.area} if self.scene == 'select' else
                {'current': 'OuiChapterPanel', 'next': None, 'panelArea': self.area, 'panelSide': self.side,
                 'panelSelectingMode': self.selecting, 'panelCheckpoint': self.cp,
                 'panelCheckpointCount': 0 if self.selecting else 1 + len(self.checkpoints)})
        return {**base, 'scene': 'Overworld', 'level': None, 'menu': menu}

    def press(self, key):
        self.keys.append((self.scene, key))
        if self.scene == 'level':
            if key == 'P':
                self.paused, self.pause_sel = True, 0
            elif self.paused and key == 'D':
                self.pause_sel = 1
            elif self.paused and key == 'C' and self.pause_sel == 1:
                self.paused, self.scene, self.leaving = False, 'exit', 20
            return
        if self.scene == 'select':
            if key == 'C':
                self.scene, self.side, self.selecting, self.cp = 'panel', 'Normal', True, 0
            return
        if self.scene != 'panel':
            return
        if self.busy or self.starting:
            self.ignored += 1
            return
        if self.selecting:
            sides = self.sides()
            if key in ('L', 'R'):
                i = sides.index(self.side) + (1 if key == 'R' else -1)
                if 0 <= i < len(sides):
                    self.side = sides[i]
            elif key == 'C':
                if self.checkpoints and self.side != 'Normal':
                    self.busy, self.flip = self.SWAP, True
                else:
                    self.starting = self.START
            elif key == 'B':
                self.scene = 'select'
        else:
            if key == 'L' and self.cp > 0:
                self.cp -= 1
            elif key == 'R' and self.cp < len(self.checkpoints):
                self.cp += 1
            elif key == 'C':
                self.starting = self.START
            elif key == 'B':
                self.busy, self.flip = self.SWAP, True

    def tick(self):
        self.frame += 1
        if self.busy:
            self.busy -= 1
            if self.busy == self.SWAP // 2 and getattr(self, 'flip', False):
                self.flip = False
                self.selecting = not self.selecting
                if not self.selecting:
                    self.cp = 0 if self.completed else len(self.checkpoints)
        if self.starting:
            self.starting -= 1
            if not self.starting:
                self.entered = (self.area, self.side, self.cp)
                self.scene, self.leaving = 'loading', 15
        if self.leaving:
            self.leaving -= 1
            if not self.leaving:
                if self.scene == 'loading':
                    self.scene = 'level'
                else:      # 返回地图：面板停在刚玩的那一面，先播成绩动画
                    self.scene, self.selecting, self.cp, self.busy = 'panel', True, 0, self.STATS

    def call(self, cmd, **options):
        if cmd == 'hello':
            return {'checkpoints': True, 'create_save': True}
        if cmd in ('observe', 'activate'):
            return {'obs': self.obs()}
        assert cmd == 'step', cmd
        for part in options['inputs']:
            head, *keys = [x.strip() for x in part.split(',')]
            for index in range(int(head)):
                for key in (keys if index == 0 else []):
                    self.press(key)
                self.tick()
                if 'controllable' in (options.get('stop_on') or []) and self.scene == 'level' and not self.paused:
                    return {'obs': self.obs(), 'result': {'stop': 'controllable', 'consumed': index + 1}}
        return {'obs': self.obs(), 'result': {'stop': 'done'}}


class MacroTests(unittest.TestCase):
    def enter(self, game, side, **kw):
        game.scene, game.area = kw.pop('scene', game.scene), kw.pop('area', game.area)
        return enter_save(game, 's', chapter=4, side=side, save_slot=3, cutscene_mode='agent', **kw)

    def test_b_side_without_checkpoints_starts_from_mode_select(self):
        game = PanelGame()
        obs = self.enter(game, 'B')
        self.assertEqual((obs['level']['sid'], obs['level']['side']), (RIDGE, 'B'))
        self.assertEqual(game.entered, (4, 'BSide', 0))
        self.assertEqual([k for where, k in game.keys if where == 'panel'], ['R', 'C'])

    def test_c_side_with_checkpoints_selects_the_start(self):
        game = PanelGame(checkpoints=['b-00', 'c-00'], completed=False)   # 没通关：列表光标在最后一项
        obs = self.enter(game, 'C')
        self.assertEqual(obs['level']['side'], 'C')
        self.assertEqual(game.entered, (4, 'CSide', 0))                    # 从开头进入
        self.assertEqual([k for where, k in game.keys if where == 'panel'], ['R', 'R', 'C', 'L', 'L', 'C'])
        done = PanelGame(checkpoints=['b-00'], completed=True)             # 通关过：光标就在开头
        self.enter(done, 'B')
        self.assertEqual(done.entered, (4, 'BSide', 0))
        self.assertEqual([k for where, k in done.keys if where == 'panel'], ['R', 'C', 'C'])

    def test_leaves_other_side_and_waits_for_busy_panel(self):
        game = PanelGame(start=(4, 'Normal'))                               # 停在 4A 里：先返回地图
        obs = self.enter(game, 'B')
        self.assertEqual(obs['level']['side'], 'B')
        self.assertGreater(game.ignored, 0)                                # 返回地图后的成绩动画期间按键被忽略，重按
        back = PanelGame(start=(4, 'BSide'))                                # 计划 4A（明确给出 A 面），停在 4B 里
        obs = self.enter(back, 'A')
        self.assertEqual(obs['level']['side'], 'A')
        self.assertEqual(back.entered, (4, 'Normal', 0))

    def test_locked_side_stops_without_entering(self):
        game = PanelGame(cassette=False, c_unlocked=False)
        with self.assertRaisesRegex(RuntimeError, '选不到 B 面'):
            self.enter(game, 'B')
        self.assertFalse(hasattr(game, 'entered'))

    def test_wrong_side_after_confirm_stops(self):
        game = PanelGame()
        original = game.tick

        def tick():                      # 替身故障：确认 B 面却进了 A 面
            original()
            if game.scene == 'loading' and game.side == 'BSide':
                game.side = 'Normal'
        game.tick = tick
        with self.assertRaisesRegex(RuntimeError, '不是要求的 B 面'):
            self.enter(game, 'B')

    def test_legacy_a_side_call_is_unchanged(self):
        game = PanelGame()
        obs = enter_save(game, 's', chapter=4, save_slot=3, cutscene_mode='agent')
        self.assertEqual((obs['level']['side'], game.entered), ('A', (4, 'Normal', 0)))
        with self.assertRaisesRegex(ValueError, '只从章节开头'):
            enter_save(game, 's', chapter=4, side='B', checkpoint=1)


class SessionTests(unittest.TestCase):
    """会话：计划是 4B／8C 时的完成判定、计划外判定与计时累加。"""
    def session(self, plan, *, detour=False, start=(RIDGE, 'B'), mode='agent', **kw):
        helper = detour_test.DetourSessionTests()
        helper._cleanups = []
        self.addCleanup(helper.doCleanups)
        chapters = tuple(p.chapter for p in plan)
        session, game = helper.session(detour=detour, mode=mode, chapters=chapters, start=start,
                                       plan=plan, return_to_map=True, **kw)
        return session, game, helper

    def test_b_side_plan_completes_and_records_side(self):
        session, game, helper = self.session([Piece(4, 0, None, 'B')], mode='auto')
        self.assertFalse(session._in_detour())
        helper.act(session, ['40'], '4B 里走一段')
        session._save_chapter_plan()                         # 计划按"章节＋面"写进 run_state
        state = json.loads((session.run_dir / 'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual((state['plan_pieces'], state['navigation_remaining']), ([[4, 0, None, 'B']], [[4, 0, None, 'B']]))
        helper.act(session, ['40,R'], '吃心')                # auto 模式：主控在这次动作里走完吃心动画与结算
        self.assertTrue(session.done)
        self.assertEqual(session.done_reason, '章节完成')
        split = session.metrics.state.splits[0]
        self.assertEqual((split['chapter'], split['side']), (RIDGE, 'B'))
        from orchestrator.segments import split_segments
        self.assertEqual(split_segments(split, {}), [(4, 0, 'B')])

    def test_a_side_of_a_b_side_plan_is_off_plan(self):
        session, game, helper = self.session([Piece(4, 0, None, 'B')], detour=True)
        helper.leave_to_map(session, game)
        helper.enter(session, game, RIDGE, 'A')
        self.assertTrue(session._in_detour())
        self.assertEqual(session.metrics.state.detour, {'chapter': RIDGE, 'side': 'A'})
        helper.act(session, ['40,R'], '通关 4A')
        self.assertFalse(session.done or session._pending_complete)
        self.assertEqual(session.metrics.state.splits or [], [])
        self.assertEqual([(d['chapter'], d['side']) for d in session.metrics.state.detours], [(RIDGE, 'A')])

    def test_without_detour_switch_a_side_still_does_not_complete_b_plan(self):
        session, game, helper = self.session([Piece(4, 0, None, 'B')])
        helper.leave_to_map(session, game)
        helper.enter(session, game, RIDGE, 'A')      # 入场门禁只核对章节：4A 进得去，但不算计划
        self.assertTrue(session._in_detour())
        helper.act(session, ['40,R'], '通关 4A')
        self.assertFalse(session.done or session._pending_complete)
        self.assertEqual(session.metrics.state.splits or [], [])

    def test_plan_time_banks_per_chapter_and_side(self):
        from orchestrator.run import total_time_ticks
        session, game, helper = self.session([Piece(4, 0, None, 'B')], detour=True)
        state = session.metrics.state
        helper.act(session, ['40'], '4B 第一段')
        first = game.ticks
        self.assertEqual(state.plan_session_ticks, first)
        helper.leave_to_map(session, game)
        helper.enter(session, game, RIDGE, 'A')          # 同一章的 A 面：计划外，计时不算
        helper.act(session, ['40'], '4A 里走一段')
        self.assertEqual((state.plan_banked_ticks, state.plan_session_ticks), (0, first))
        self.assertEqual(total_time_ticks(state, session), first)
        helper.leave_to_map(session, game)
        helper.enter(session, game, RIDGE, 'B')          # 回到 4B：新的一段会话，此前那段存起来
        self.assertEqual(state.plan_banked_ticks, first)
        helper.act(session, ['30'], '4B 第二段')
        second = game.ticks
        helper.act(session, ['40,R'], '吃心')
        helper.act(session, ['40'], '等吃心动画')
        helper.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.done)
        self.assertEqual(state.splits[0]['ticks'], first + second)

    def test_a_then_b_of_same_chapter_are_two_pieces(self):
        session, game, helper = self.session([Piece(4), Piece(4, 0, None, 'B')], start=(RIDGE, 'A'))
        helper.act(session, ['40,R'], '通关 4A')
        helper.act(session, ['1,P', '20'], '打开暂停菜单')
        helper.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        helper.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.chapter_finished)
        helper.enter(session, game, RIDGE, 'A')          # 下一截是 4B：再进 4A 不算
        self.assertTrue(session.chapter_finished)
        helper.act(session, ['1,P', '39'], '暂停')
        helper.act(session, ['1,D', '1,D', '1,C', '5'], '选返回地图')
        helper.act(session, ['1,C', '5'], '确认返回地图')
        for _ in range(5):
            if session._at_chapter_select(session.last_obs):
                break
            helper.act(session, ['30'], '等回到地图')
        helper.enter(session, game, RIDGE, 'B')
        self.assertFalse(session.chapter_finished)
        self.assertEqual(session.piece, Piece(4, 0, None, 'B'))
        self.assertEqual(session.chapter_plan()['segments']['current'], [4, 0, 'B'])
        helper.act(session, ['40,R'], '吃心')
        helper.act(session, ['40'], '等吃心动画')
        helper.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.done)
        self.assertEqual([(s['chapter'], s.get('side', 'A')) for s in session.metrics.state.splits],
                         [(RIDGE, 'A'), (RIDGE, 'B')])

    def test_connect_refuses_wrong_side_and_auto_switch_passes_side(self):
        with self.assertRaisesRegex(RuntimeError, '面不一致'):
            self.session([Piece(4, 0, None, 'B')], start=(RIDGE, 'A'))
        session, game, helper = self.session([Piece(9), Piece(9, 0, None, 'C')], start=(CORE, 'A'), mode='auto')
        calls = []

        def macro(client, save, chapter=None, **options):
            calls.append((chapter, options.get('side'), options.get('checkpoint')))
            client.frame += 300
            client._enter(CHAPTER_SIDS[chapter], options.get('side') or 'A')
        with patch('enter_save.enter_save', side_effect=macro):
            helper.act(session, ['40,R'], '通关 9A')
        self.assertEqual(calls, [(9, 'C', 0)])
        self.assertEqual(session.piece, Piece(9, 0, None, 'C'))

    def test_a_only_plan_is_unchanged(self):
        session, game, helper = self.session([Piece(9)], start=(CORE, 'A'), mode='auto')
        self.assertFalse(session._classifying())
        calls = []
        session2, game2, helper2 = self.session([Piece(9), Piece(10)], start=(CORE, 'A'), mode='auto')

        def macro(client, save, chapter=None, **options):
            calls.append(sorted(options))
            client._enter(CHAPTER_SIDS[chapter], 'A')
        with patch('enter_save.enter_save', side_effect=macro):
            helper2.act(session2, ['40,R'], '通关 9A')
        self.assertNotIn('side', calls[0])                # 只有 A 面时菜单宏的调用与原来相同


class RunArgsTests(unittest.TestCase):
    TABLE = load_table()

    def parse(self, root, *argv):
        from orchestrator.run import parse_args
        with patch('orchestrator.config.REPO_ROOT', root), \
                patch.object(sys, 'argv', ['run', '--agent', 'a', '--save', 's', '--no-sim', '--runtime', 'host', *argv]):
            return parse_args()

    def test_chapters_with_sides(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, _ = self.parse(root, '--chapters', '0,1,2')
            self.assertIsNone(cfg.plan)                                   # 纯数字与原来相同
            self.assertEqual(cfg.chapters, [0, 1, 2])
            cfg, _ = self.parse(root, '--chapters', '4,4b,8C')
            self.assertEqual(cfg.plan, [Piece(4), Piece(4, 0, None, 'B'), Piece(9, 0, None, 'C')])
            self.assertEqual((cfg.chapters, cfg.chapter), ([4, 4, 9], 4))
            for argv, phrase in ((('--chapters', '0B'), '没有'), (('--chapters', '4B,4b'), '两次'),
                                 (('--segments', '4B'), '只支持 A 面'),
                                 (('--chapters', '4B', '--scope', 'campaign'), '只按 A 面')):
                with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, phrase):
                    self.parse(root, *argv)

    def test_run_state_rows_and_resume_extension(self):
        from orchestrator.run import write_run_state
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, args = self.parse(root, '--chapters', '4B')
            out = root / 'new'
            out.mkdir()
            write_run_state(out, cfg, 'sess', None)
            state = json.loads((out / 'run_state.json').read_text(encoding='utf-8'))
            self.assertEqual((state['plan_pieces'], state['navigation_remaining']), ([[4, 0, None, 'B']], [[4, 0, None, 'B']]))
            cfg, _ = self.parse(root, '--chapters', '1')
            write_run_state(out, cfg, 'sess', None)
            state = json.loads((out / 'run_state.json').read_text(encoding='utf-8'))
            self.assertEqual((state['plan_pieces'], state['navigation_remaining']), (None, [[1, 0, None]]))
            # 续跑：原计划（A 面）已完成，追加 B 面
            run = root / 'runs' / 'a' / '20261030-000000'
            run.mkdir(parents=True)
            base = {'agent': 'a', 'save': 's', 'engine': 'claude', 'model': 'opus', 'effort': 'high', 'max_minutes': 60,
                    'runtime': 'host', 'sim': False, 'record': False, 'mode': 'speedrun', 'knowledge_profile': 'full',
                    'session_id': 'sess', 'tuning': {}}
            (run / 'run_state.json').write_text(json.dumps({**base, 'all_chapters': [4], 'plan_pieces': None}), encoding='utf-8')
            (run / 'metrics.json').write_text(json.dumps({'splits': [{'chapter': RIDGE}]}), encoding='utf-8')
            cfg, args = self.parse(root, '--resume', str(run), '--chapters', '4B')
            self.assertEqual(cfg.plan, [Piece(4, 0, None, 'B')])
            self.assertEqual(cfg._extended_plan, [Piece(4), Piece(4, 0, None, 'B')])
            write_run_state(out, cfg, 'sess', args.previous)
            state = json.loads((out / 'run_state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['plan_pieces'], [[4, 0, None], [4, 0, None, 'B']])
            # 带面的计划中途续跑：从 run_state 的 4 元项接着走
            (run / 'run_state.json').write_text(json.dumps({**base, 'all_chapters': [4, 9],
                                                            'plan_pieces': [[4, 0, None, 'B'], [9, 0, None, 'C']]}), encoding='utf-8')
            (run / 'metrics.json').write_text(json.dumps({'splits': [{'chapter': RIDGE, 'side': 'B'}]}), encoding='utf-8')
            cfg, _ = self.parse(root, '--resume', str(run))
            self.assertEqual(cfg.plan, [Piece(9, 0, None, 'C')])
            # 剩下的只有 A 面：仍按章节＋面区分（4B 之后的 4A 不会被 4B 的通关顶替）
            (run / 'run_state.json').write_text(json.dumps({**base, 'all_chapters': [4],
                                                            'plan_pieces': [[4, 0, None, 'B'], [4, 0, None]]}), encoding='utf-8')
            cfg, _ = self.parse(root, '--resume', str(run))
            self.assertEqual((cfg.plan, cfg._side_plan), ([Piece(4)], True))

    def test_main_checks_unlock_before_any_game_input(self):
        from orchestrator import run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, _ = self.parse(root, '--chapters', '4B', '--save-slot', '3')
            path = root / '3.celeste'
            path.write_text(save_xml(cassettes=set()), encoding='utf-8')
            with patch('orchestrator.saves.save_file', return_value=path), self.assertRaisesRegex(SystemExit, '磁带'):
                run.check_sides(cfg)
            path.write_text(save_xml(cassettes={4}), encoding='utf-8')
            with patch('orchestrator.saves.save_file', return_value=path):
                run.check_sides(cfg)
            cfg.create_save = True
            with self.assertRaisesRegex(SystemExit, '新建存档不能计划'):
                run.check_sides(cfg)


class ConsoleTests(unittest.TestCase):
    def options(self, **extra):
        return {'agent': 'dev-opus', 'save': {'name': 'test', 'slot': 7, 'create': False}, **extra}

    def test_command_preview(self):
        argv = build(self.options(chapters=[4], sides=[[9, 'C'], [4, 'B']]))
        self.assertEqual(argv[argv.index('--chapters') + 1], '4,4B,8C')
        argv = build(self.options(chapters=[], sides=[[9, 'B']]))
        self.assertEqual(argv[argv.index('--chapters') + 1], '8B')
        self.assertEqual(build(self.options()), build(self.options(sides=[])))      # 没选 B／C 面时命令与原来相同
        self.assertIn('--chapter 0', command_line(build(self.options())))
        for bad, phrase in (({'sides': [[0, 'B']]}, '没有'), ({'sides': [[4, 'A']]}, '须写成'),
                            ({'sides': [[4, 'B']], 'segments': [[4, 0]]}, '整章'),
                            ({'sides': [[4, 'B']], 'save': {'name': 'x', 'create': True}}, '新建存档不能计划'),
                            ({'chapters': [], 'sides': []}, '请选择')):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, phrase):
                normalize(self.options(**bad))

    def test_options_list_sides(self):
        from overlay import console_api
        with patch('orchestrator.saves.inventory', return_value=[]):
            data = console_api.options()
        sides = data['sides']
        self.assertEqual([(x['id'], x['side']) for x in sides if x['side'] == 'B'], [(c, 'B') for c in SIDE_CHAPTERS])
        self.assertEqual(len(sides), 16)
        self.assertIn({'id': 9, 'side': 'B', 'code': '8B', 'label': '8B 核心 B 面'}, sides)
        self.assertEqual(console_api.video_label(RIDGE, 'B'), '4B 黄金山脊 B 面')
        self.assertEqual(console_api.video_label(RIDGE, 'A'), '4A 黄金山脊')

    def test_console_start_checks_unlock(self):
        from overlay import console_api
        with patch('orchestrator.saves.validate_sides', side_effect=ValueError('B 面在这个存档里还没有解锁')), \
                patch.object(console_api, '_launch', side_effect=AssertionError('没解锁时不启动')), \
                self.assertRaisesRegex(ValueError, '还没有解锁'):
            console_api.start(self.options(chapters=[], sides=[[4, 'B']]))

    def test_running_plan_control_accepts_side_segments(self):
        from orchestrator.control import RunControl
        with tempfile.TemporaryDirectory() as folder:
            control = RunControl(Path(folder))
            self.assertTrue(control.enqueue('chapters', json.dumps({'segments': [[4, 0, 'B'], [9, 0]]})))
            with self.assertRaises(ValueError):
                control.enqueue('chapters', json.dumps({'segments': [[4, 0, 'D']]}))

    def test_page_has_side_rows_and_hint(self):
        html = (Path(__file__).resolve().parent.parent / 'overlay' / 'console.html').read_text(encoding='utf-8')
        self.assertIn('目前只支持蔚蓝的官方章节（A/B/C 面）；MOD 地图在未来版本中支持。', html)
        self.assertIn('id="sideList"', html)
        self.assertIn('sides: ui.sides', html)


if __name__ == '__main__':
    unittest.main()
