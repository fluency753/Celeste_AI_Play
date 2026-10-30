"""计划外地图（--allow-detour）：入场门禁、绕行通关、回到计划章节、续跑与开关的回归。

只模拟外部边界：DetourGame 代替桥接连接（按桥接 MapEntryGuard／ActionPolicy 的同一规则核对入场与暂停菜单动作，
实现暂停菜单返回地图、选章面板、通关结算的原生行为）；主控的门禁目标、自动推进、跳过剧情、结算确认与成绩记账
都走生产代码。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestrator.action_policy import ActionPermissions, configure, permissions_for
from orchestrator.config import CHAPTER_SIDS, RunConfig
from orchestrator.control import RunControl
from orchestrator.detour import OFFICIAL_MAPS, instructions
from orchestrator.game_session import ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.segments import Piece

CORE, RESORT, RIDGE = CHAPTER_SIDS[9], CHAPTER_SIDS[3], CHAPTER_SIDS[4]
HELLO = {'pico8_entry_guard': True, 'entry_guard': True, 'action_policy': 1, 'map_entry_set': True,
         'chapter_reset_guard': True, 'checkpoints': False}
PAUSE_ITEMS = ['menu_pause_resume', 'menu_pause_retry', 'menu_pause_return']


class DetourGame:
    """桥接连接的替身。地图入场按桥接 MapEntryGuard.Check（槽位、sid、地图集合与面），暂停菜单的返回地图按
    ActionPolicy.CheckMenu（返回地图权限或离开许可）核对；按键只在每段的第一帧算一次"按下"。
    关卡里 X 代表死亡、R 代表走到终点：A 面进入可用暂停菜单跳过的片尾剧情；B 面吃到心，画面停在已通关、
    不在剧情里的一段动画后自己进入结算；G 走进 next_room。与桥接一样：关卡里每推进 progress 帧推送一次进度，
    每帧一条轨迹（不在关卡里时、死亡后的 20 帧死亡动画里没有角色），按 stop_rooms 在进入指定房间的那一帧停下。"""

    def __init__(self, start=(CORE, 'A'), slot=7, save='opus5.5'):
        self.slot, self.save, self.frame = slot, save, 1000
        self.commands, self.steps, self.policies, self.menu_keys = [], [], [], []
        self.step_options = self.after_step = self.cancel_check = None
        self.panel_target = None     # 选章界面里光标停在哪张地图哪一面（智能体用方向键选择，这里直接摆好）
        self.next_room = 'a-01'
        self.deaths = 0
        self.cancelled = False
        self.dead_left = 0           # 死亡动画还剩几帧（这期间轨迹里没有角色）
        self._enter(*start)

    # ---- 原生状态
    def _enter(self, sid, side):
        self.map, self.state, self.selection, self.wait = (sid, side), 'level', 0, 0
        self.completed, self.ticks, self.deaths, self.room = False, 0, 0, 'a-00'

    def in_level(self):
        return self.state in {'level', 'pause', 'ending', 'confirm', 'heart'}

    def observation(self):
        base = {'frame': self.frame, 'storyInput': None, 'storyDecision': None, 'story': [],
                'save': {'name': self.save, 'slot': self.slot}, 'controllable': False}
        if self.in_level():
            sid, side = self.map
            cutscene = self.state == 'ending' or (self.state == 'pause' and self.completed)
            obs = {**base, 'scene': 'Level', 'controllable': self.state == 'level',
                   'level': {'sid': sid, 'side': side, 'room': self.room, 'inCutscene': cutscene, 'skippingCutscene': False,
                             'paused': self.state in {'pause', 'confirm'}, 'canPause': True, 'transitioning': False,
                             'frozen': False, 'completed': self.completed, 'dialogOpen': False, 'dialogWaiting': False,
                             'dialogChoices': [], 'deaths': self.deaths, 'deathsInRoom': 0,
                             'chapterTimeTicks': self.ticks, 'camera': [0, 0], 'bounds': [0, 0, 320, 180]},
                   'player': {'pos': [10, 20], 'speed': [0, 0], 'dead': False,
                              'state': 'Normal' if self.state == 'level' else 'Dummy', 'inControl': self.state == 'level',
                              'dashes': 1, 'maxDashes': 1, 'onGround': True, 'hitbox': [6, 9, 8, 11]}}
            if self.state == 'pause':
                items = (['menu_pause_resume', 'menu_pause_skip_cutscene'] if self.completed else PAUSE_ITEMS)
                obs['pauseMenu'] = {'focused': True, 'selection': self.selection, 'items': [
                    {'index': i, 'id': ident, 'selectable': True, 'disabled': False} for i, ident in enumerate(items)]}
            elif self.state == 'confirm':
                obs['pauseMenu'] = {'focused': True, 'selection': 1, 'items': [
                    {'index': 0, 'id': 'menu_pause_return', 'selectable': False, 'disabled': False},
                    {'index': 1, 'id': 'menu_pause_return', 'selectable': True, 'disabled': False}]}
            return obs
        if self.state == 'exit':
            return {**base, 'scene': 'LevelExit', 'level': None}
        if self.state == 'loading':
            return {**base, 'scene': 'LevelLoader', 'level': None}
        if self.state == 'results':
            return {**base, 'scene': 'AreaComplete', 'level': None,
                    'completion': {'sid': self.map[0], 'side': self.map[1], 'room': 'end', 'chapterTimeTicks': self.ticks}}
        current = 'OuiChapterPanel' if self.state == 'panel' else 'OuiChapterSelect'
        return {**base, 'scene': 'Overworld', 'level': None, 'menu': {'current': current, 'next': None}}

    def _entry_block(self, policy, sid, side):
        """与桥接 MapEntryGuard.Check 相同的顺序：槽位、单地图 sid、地图集合（区分面）。"""
        target = (policy or {}).get('entry_target') or {}
        if target.get('slot') is not None and target['slot'] != self.slot:
            return {'stop': 'entry_blocked', 'rule_id': 'target.save_slot', 'action': 'enter_map',
                    'expected': target['slot'], 'actual': self.slot}
        if target.get('sid') is not None and target['sid'] != sid:
            return {'stop': 'entry_blocked', 'rule_id': 'target.map', 'action': 'enter_map',
                    'expected': target['sid'], 'actual': sid}
        if 'maps' in target and {'sid': sid, 'side': side} not in target['maps']:
            return {'stop': 'entry_blocked', 'rule_id': 'target.maps', 'action': 'enter_map',
                    'expected': target['maps'], 'actual': {'sid': sid, 'side': side}}
        return None

    def _press(self, key, policy, events):
        if self.state == 'level':
            if key == 'P':
                self.state, self.selection = 'pause', 0
                events.append({'type': 'uncontrollable'})
            elif key == 'X':
                self.deaths += 1
                self.dead_left = 20
                events.append({'type': 'death'})
            elif key == 'G':
                self.room = self.next_room
                events.append({'type': 'room', 'room': self.room})
            elif key == 'R':
                self.completed = True
                self.state, self.wait = ('heart', 30) if self.map[1] == 'B' else ('ending', 0)
                events.append({'type': 'uncontrollable'})
        elif self.state == 'ending' and key == 'P':
            self.state, self.selection = 'pause', 0
        elif self.state == 'pause':
            items = ['menu_pause_resume', 'menu_pause_skip_cutscene'] if self.completed else PAUSE_ITEMS
            if key in {'U', 'D'}:
                self.selection = max(0, min(len(items) - 1, self.selection + (1 if key == 'D' else -1)))
            elif key == 'B' or (key == 'C' and items[self.selection] == 'menu_pause_resume'):
                self.state = 'ending' if self.completed else 'level'
            elif key == 'C' and items[self.selection] == 'menu_pause_skip_cutscene':
                self.state = 'results'
            elif key == 'C' and items[self.selection] == 'menu_pause_return':
                self.state = 'confirm'
        elif self.state == 'confirm':
            if key == 'B':
                self.state = 'pause'
            elif key == 'C':
                permissions = (policy or {}).get('permissions') or {}
                if not (permissions.get('return_to_map') or (policy or {}).get('departure') == 'current_level'):
                    return {'stop': 'chapter_reset_blocked', 'rule_id': 'permission.return_to_map',
                            'action': 'return_to_map', 'expected': True, 'actual': False}
                self.state, self.wait = 'exit', 40
        elif self.state == 'results' and key == 'C':
            self.state = 'select'
        elif self.state == 'select' and key == 'C':
            self.menu_keys.append(('select', key))
            self.state = 'panel'
        elif self.state == 'panel':
            self.menu_keys.append(('panel', key))
            if key == 'B':
                self.state = 'select'
            elif key == 'C':
                block = self._entry_block(policy, *self.panel_target)
                if block:
                    return block
                self.map, self.state, self.wait = self.panel_target, 'loading', 15
        return None

    def _frame(self, before, events):
        """每帧按键之后调用（子类在这里推进菜单动画等计时）。"""

    def _wait_done(self, events):
        """转场等待结束：返回地图到选章页，心的动画到结算，载入到关卡第一帧。"""
        if self.state == 'exit':
            self.state = 'select'
        elif self.state == 'heart':
            self.state = 'results'
        else:
            self._enter(*self.map)
            events.append({'type': 'controllable'})

    def call(self, command, on_event=None, **options):
        self.commands.append(command)
        if command == 'hello':
            return dict(HELLO)
        if command in {'observe', 'activate'}:
            return {'obs': self.observation()}
        if command == 'areas':
            return {'areas': []}
        if command != 'step':
            return {}
        if self.step_options is not None:
            options.update(self.step_options())      # 与 BridgeClient.call 相同：会话的门禁随每次推进发送
        policy = options.get('action_policy')
        self.policies.append(policy)
        inputs, stop_on = list(options['inputs']), set(options.get('stop_on') or [])
        stop_rooms, every = set(options.get('stop_rooms') or []), int(options.get('progress') or 0)
        self.steps.append(inputs)
        events, consumed, stop, block, rows = [], 0, None, None, []
        self.cancelled = False
        for part in inputs:
            head, *keys = [x.strip() for x in part.split(',')]
            for index in range(int(head)):
                if self.cancelled and not stop:
                    stop = 'cancelled'
                if stop:
                    break
                self.frame += 1
                consumed += 1
                before = self.state
                for key in (keys if index == 0 else []):
                    block = self._press(key, policy, events)
                    if block:
                        stop = block['stop']
                        break
                self._frame(before, events)
                if self.state == 'level' and before == 'level' and not self.completed:
                    self.ticks += 170_000
                if self.state in {'exit', 'loading', 'heart'} and before == self.state:
                    self.wait -= 1
                    if self.wait <= 0:
                        self._wait_done(events)
                if self.completed and self.state in {'ending', 'pause', 'heart'}:
                    events.append({'type': 'complete'})
                for event in events:
                    if event.get('frame') is None and not stop and (
                            event['type'] in stop_on or (event['type'] == 'room' and event.get('room') in stop_rooms)):
                        stop = event['type']
                for event in events:
                    event.setdefault('frame', self.frame)
                alive = self.in_level() and self.dead_left <= 0
                rows.append([self.frame, 10.0, 20.0, 0.0, 0.0, 'Normal', 1, 1] if alive else [self.frame, None])
                if self.in_level() and self.dead_left > 0:
                    self.dead_left -= 1
                if every and on_event and self.in_level() and (consumed % every == 0 or stop):
                    on_event({'event': 'progress', 'frame': self.frame, 'sid': self.map[0], 'room': self.room,
                              'chapterTimeTicks': self.ticks, 'deaths': self.deaths, 'dashes': 1, 'maxDashes': 1,
                              'dead': False})
        result = {'consumed': consumed, 'planned': consumed, 'stop': stop or 'done', 'trajectory': rows,
                  'events': [{'type': e['type'], 'frame': e['frame']} for e in events]}
        if block:
            result['block'] = {k: v for k, v in block.items() if k != 'stop'}
        reply = {'obs': self.observation(), 'result': result}
        if self.after_step is not None:
            self.after_step(options, reply)
        return reply

    def cancel_pending(self):
        self.cancelled = True        # 与 BridgeClient.cancel_pending 相同：只从进度回调里调用，这一步在下一帧前停下

    def close(self):
        pass


class DetourSessionTests(unittest.TestCase):
    def session(self, *, detour=True, mode='auto', chapters=(9,), start=(CORE, 'A'), return_to_map=None, restore=None,
                extended=False, plan=None, checkpoints=None, game_ticks=0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        cfg = RunConfig(agent='opus5.5', save='opus5.5', model='test', runtime='host', save_slot=7,
                        cutscene_mode=mode, chapter=chapters[0], chapters=list(chapters), collision_image='off',
                        provider_info={'images': False})
        cfg.thought_lead_s = 0
        cfg.allow_detour = detour
        if plan is not None:
            cfg.plan, cfg.checkpoints = list(plan), dict(checkpoints or {})
        if extended:
            cfg._extended_plan = [Piece(c) for c in chapters]   # 续跑追加了新计划（run.parse_args 的 --resume … --chapters）
        configure(cfg, ActionPermissions(return_to_map=detour if return_to_map is None else return_to_map))
        (root / 'run_state.json').write_text('{}', encoding='utf-8')
        metrics = Metrics(root / 'metrics.json', 'opus5.5', 'test', 'high', 600)
        if restore is not None:
            metrics.restore(restore, 'previous')        # 续跑：接着上一段运行的统计
        session = GameSession(cfg, root, metrics)
        self.addCleanup(session.close, release=False)
        game = DetourGame(start=start)
        game.ticks = game_ticks
        with patch('orchestrator.game_session.BridgeClient', return_value=game):
            session.connect()
        return session, game

    def act(self, session, inputs, thought='试一下'):
        return session.act(inputs, thought, None)

    def leave_to_map(self, session, game):
        """智能体自己按 P 打开暂停菜单、移到「返回地图」并确认（auto 模式开了计划外地图才有 P）。"""
        self.act(session, ['1,P', '39'], '回地图找心')
        self.assertTrue(session.last_obs['level']['paused'])
        self.act(session, ['1,D', '1,D', '1,C', '5'], '选返回地图')
        self.act(session, ['1,C', '5'], '确认返回地图')
        for _ in range(5):                 # agent 剧情模式：回地图的转场也由智能体自己等（auto 模式主控已等完）
            if session._at_chapter_select(session.last_obs):
                break
            self.act(session, ['30'], '等回到地图')
        self.assertTrue(session._at_chapter_select(session.last_obs), session.last_obs)

    def enter(self, session, game, sid, side):
        game.panel_target = (sid, side)
        self.act(session, ['1,C', '5'], '打开章节面板')
        return self.act(session, ['1,C', '5'], '进入地图')

    def test_entry_policy_wire_format_follows_switch(self):
        session, game = self.session(detour=False)
        target = session._step_options()['action_policy']['entry_target']
        self.assertEqual(target, {'sid': CORE, 'slot': 7})
        session.cfg.allow_detour = True
        target = session._step_options()['action_policy']['entry_target']
        # sid 为空：桥接不再限定计划章节；槽位仍核对，地图限于官方章节的各面
        self.assertEqual(target, {'sid': None, 'slot': 7, 'maps': OFFICIAL_MAPS})
        self.assertIn({'sid': RIDGE, 'side': 'B'}, OFFICIAL_MAPS)
        self.assertNotIn({'sid': CHAPTER_SIDS[0], 'side': 'B'}, OFFICIAL_MAPS)
        self.assertIsNone(game._entry_block(session._step_options()['action_policy'], RIDGE, 'B'))
        self.assertEqual(game._entry_block(session._step_options()['action_policy'], 'SJ/1-Beginner', 'A')['rule_id'],
                         'target.maps')
        game.slot = 3
        self.assertEqual(game._entry_block(session._step_options()['action_policy'], RIDGE, 'B')['rule_id'],
                         'target.save_slot')
        session.map_entry_set_supported = False
        with self.assertRaisesRegex(ActError, '1.10.0'):
            session._step_options()

    def test_off_blocks_non_plan_map_and_keeps_auto_keys(self):
        session, game = self.session(detour=False, mode='agent', return_to_map=True)
        self.leave_to_map(session, game)
        outcome = self.enter(session, game, RIDGE, 'B')
        self.assertEqual(outcome.result['stop'], 'entry_blocked')
        self.assertEqual(game.state, 'panel')
        self.assertTrue(any('地图与任务不符' in note for note in outcome.auto_notes))
        self.assertIsNone(session._detour)
        auto, _ = self.session(detour=False)
        with self.assertRaises(ActError):
            self.act(auto, ['1,P', '39'])           # 关时 auto 模式照旧没有暂停键

    def test_on_blocks_mod_maps_with_detour_note(self):
        session, game = self.session()
        self.leave_to_map(session, game)
        outcome = self.enter(session, game, 'StrawberryJam2021/1-Beginner/lobby', 'A')
        self.assertEqual(outcome.result['stop'], 'entry_blocked')
        self.assertTrue(any('计划外地图只限蔚蓝官方章节' in note for note in outcome.auto_notes))

    def test_auto_b_side_detour_then_plan_completion_records_one_split(self):
        session, game = self.session()
        queue = list(session.chapter_queue)
        self.leave_to_map(session, game)
        self.assertFalse(session.done)
        outcome = self.enter(session, game, RIDGE, 'B')
        self.assertEqual(session.last_obs['level']['side'], 'B')
        self.assertTrue(any('已进入计划外地图4 黄金山脊 B面' in note for note in outcome.auto_notes))
        self.assertEqual(session.metrics.state.detour, {'chapter': RIDGE, 'side': 'B'})
        self.act(session, ['40,X'], '试跳')                # 计划外地图里死一次
        steps_before = len(game.steps)
        outcome = self.act(session, ['40,R'], '吃心')
        state = session.metrics.state
        # 计划外通关：不结束、不记分段、不动计划；auto 模式照常跳过片尾、确认结算，停在选章地图交给智能体
        self.assertFalse(session.done, session.done_reason)
        self.assertEqual(state.splits or [], [])
        self.assertEqual(state.completed_ticks, 0)
        self.assertFalse(state.piece_done)
        self.assertFalse(session.chapter_finished)
        self.assertEqual(session.piece, Piece(9))
        self.assertEqual(list(session.chapter_queue), queue)
        self.assertEqual([(d['chapter'], d['side']) for d in state.detours], [(RIDGE, 'B')])
        self.assertTrue(session._at_chapter_select(session.last_obs))
        # 计划外地图的收尾回到选章地图就交回：不在地图上停留，不记 end_scene
        self.assertNotIn(['60'], game.steps[steps_before:])
        self.assertIsNone(session.end_scene)
        self.assertTrue(any('不计入本次计划' in note for note in outcome.auto_notes))
        self.assertTrue(any('主控不代选' in note for note in outcome.auto_notes))
        # 主控只在结算画面按确认，选章地图里没有替智能体按任何键
        self.assertEqual([key for key in game.menu_keys], [('select', 'C'), ('panel', 'C')])
        self.assertIn(['1,C', '29'], game.steps[steps_before:])
        self.assertTrue(session.off_plan())
        # 回到计划章节：门禁照常放行，计划通关后本次运行结束，只记一条成绩
        outcome = self.enter(session, game, CORE, 'A')
        self.assertTrue(any('已回到计划章节' in note for note in outcome.auto_notes))
        self.assertIsNone(session._detour)
        self.assertIsNone(session.metrics.state.detour)
        self.assertFalse(session.off_plan())
        self.act(session, ['40,X'], '核心里死一次')
        self.act(session, ['40,R'], '通关核心')
        self.assertTrue(session.done)
        self.assertEqual(session.done_reason, '章节完成')
        # 死亡统一记录：计划这一截的死亡包括期间在计划外地图里的死亡
        self.assertEqual([(s['chapter'], s['deaths']) for s in state.splits], [(CORE, 2)])
        self.assertEqual(state.completed_ticks, state.splits[0]['ticks'])
        self.assertEqual(len(state.detours), 1)
        self.assertEqual(state.deaths, 2)

    def test_auto_a_side_detour_keeps_plan_queue_and_segment(self):
        session, game = self.session(chapters=(9, 10))
        self.assertEqual(session.chapter_queue, [Piece(10)])
        self.leave_to_map(session, game)
        self.enter(session, game, RESORT, 'A')
        self.assertTrue(session._in_detour())
        self.assertEqual(session._stop_rooms(), {})
        self.assertFalse(session._note_room(RESORT, 'a-01'))
        self.act(session, ['40,R'], '走完 3A')
        self.assertFalse(session.done or session.chapter_finished)
        self.assertEqual(session.chapter_queue, [Piece(10)])
        self.assertEqual(session.piece, Piece(9))
        self.assertEqual(session.metrics.state.splits or [], [])
        self.assertEqual(session.metrics.state.detours[0]['side'], 'A')
        plan = json.loads((session.run_dir / 'run_state.json').read_text(encoding='utf-8'))
        self.assertNotIn('plan_pieces', plan)              # 绕行不改写计划
        self.assertEqual(session.chapter_plan()['detour'], {'chapter': RESORT, 'side': 'A', 'completed': True})

    def test_agent_mode_detour_completion_presses_nothing(self):
        session, game = self.session(mode='agent')
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        acted = len(game.steps)
        outcome = self.act(session, ['40,R'], '吃心')
        self.assertEqual(game.steps[acted:], [['40,R']])   # 主控没有补按任何键
        self.assertFalse(session._pending_complete)
        self.assertFalse(json.loads((session.run_dir / 'run_state.json').read_text()).get('completion_pending'))
        self.assertNotIn('complete', session._stop_list(None))
        self.assertTrue(any('由你用 act 操作' in note for note in outcome.auto_notes))
        self.act(session, ['40'], '等吃心动画')
        self.assertEqual(session.last_obs['scene'], 'AreaComplete')
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session._at_chapter_select(session.last_obs))
        self.assertFalse(session.done)
        self.assertEqual(session.metrics.state.splits or [], [])
        self.enter(session, game, CORE, 'A')
        self.act(session, ['40,R'], '通关核心')
        self.assertTrue(session._pending_complete)
        self.assertFalse(session.done)
        self.act(session, ['1,P', '20'], '打开暂停菜单')
        self.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.done)
        self.assertEqual(len(session.metrics.state.splits), 1)

    def test_agent_pause_menu_is_not_closed_in_auto_mode(self):
        session, game = self.session()
        outcome = self.act(session, ['1,P', '39'], '打开暂停菜单')
        self.assertEqual(game.steps, [['1,P', '39']])
        self.assertEqual(game.state, 'pause')
        self.assertEqual(outcome.auto_frames, 0)
        self.assertFalse(any('主控停止自动播放' in str(note) for note in outcome.auto_notes))   # 不当作剧情菜单接管
        self.assertEqual(session._resume_auto_handling(), [])
        self.act(session, ['1,B', '5'], '关掉菜单')
        self.assertTrue(session.last_obs['controllable'])

    def test_resume_inside_detour_and_completed_detour(self):
        session, game = self.session(start=(RIDGE, 'B'))
        self.assertTrue(session._in_detour())
        self.assertFalse(session._pending_complete)
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            self.session(detour=False, start=(RIDGE, 'B'))
        session, game = self.session(start=(RIDGE, 'B'))
        game.completed, game.state = True, 'ending'
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        resumed = GameSession(session.cfg, Path(temp.name), Metrics(Path(temp.name) / 'm.json', 'a', 'b', 'high', 60))
        self.addCleanup(resumed.close, release=False)
        with patch('orchestrator.game_session.BridgeClient', return_value=game):
            resumed.connect()
        self.assertFalse(resumed._pending_complete)
        self.assertTrue(resumed._detour_completed())
        self.assertEqual(resumed.metrics.state.detours, None)   # 中断前已记过：续跑不补记
        resumed.last_obs = game.observation()
        with self.assertRaisesRegex(ActError, '计划外地图已通关'):
            self.act(resumed, ['40'])
        self.assertTrue(resumed._at_chapter_select(resumed.last_obs))
        self.assertFalse(resumed.done)

    def test_map_complete_goal_waits_for_plan_chapter(self):
        # "当前地图通关"绑定设目标时的地图：在计划章节设的目标不会被计划外地图的通关触发
        session, game = self.session()
        session.goals.set({'conditions': [{'field': 'map_complete', 'op': 'eq', 'value': True}]},
                          session.goal_values(game.observation()))
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        self.act(session, ['40,R'], '吃心')
        self.assertFalse(session.goals.stop_motion.is_set())
        self.assertFalse(session.done)
        self.enter(session, game, CORE, 'A')
        self.act(session, ['40,R'], '通关核心')
        self.assertTrue(session.goals.stop_motion.is_set())
        self.assertEqual(len(session.metrics.state.splits), 1)

    def test_old_bridge_is_refused_before_any_input(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        cfg = RunConfig(agent='opus5.5', save='opus5.5', model='test', runtime='host', save_slot=7, chapter=9, chapters=[9])
        cfg.allow_detour = True
        session = GameSession(cfg, root, Metrics(root / 'm.json', 'a', 'b', 'high', 60))
        self.addCleanup(session.close, release=False)
        game = DetourGame()
        game.call = lambda command, **options: ({**HELLO, 'map_entry_set': False} if command == 'hello'
                                                  else self.fail('不应在核对能力前发送 ' + command))
        with patch('orchestrator.game_session.BridgeClient', return_value=game), \
             self.assertRaisesRegex(RuntimeError, 'map_entry_set'):
            session.connect()

    def test_user_stop_in_detour_keeps_queue_and_total(self):
        session, game = self.session(chapters=(9, 10))
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        game.completed = True
        session.last_obs = game.observation()
        session.finish_user_stop()
        self.assertEqual(session.chapter_queue, [Piece(10)])
        self.assertEqual(session.metrics.state.splits or [], [])
        self.assertTrue(session.off_plan())


    # ---- 计划章节的计时按每次进入累计（--allow-detour）
    def finish_core_agent(self, session):
        """agent 剧情模式：通关核心后自己用暂停菜单跳过片尾、确认结算。"""
        self.act(session, ['40,R'], '通关核心')
        self.act(session, ['1,P', '20'], '打开暂停菜单')
        self.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        self.act(session, ['1,C', '5'], '确认结算')

    def test_plan_time_sums_plan_sessions_and_excludes_detours(self):
        from orchestrator.run import total_time_ticks
        session, game = self.session()
        state = session.metrics.state
        self.act(session, ['40'], '核心第一段')
        first = game.ticks
        self.assertEqual(state.plan_session_ticks, first)
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        self.act(session, ['40'], '4B 里走一段')
        self.assertGreater(state.chapter_time_ticks, 0)
        self.assertEqual((state.plan_banked_ticks, state.plan_session_ticks), (0, first))   # 计划外地图的计时不算
        self.assertEqual(total_time_ticks(state, session), first)
        self.leave_to_map(session, game)
        self.enter(session, game, CORE, 'A')            # 重新进入计划章节：新的一段会话，此前那段存起来
        self.assertEqual(state.plan_banked_ticks, first)
        self.act(session, ['30', '1,P', '9'], '核心第二段')       # 第二段走 30 帧后停在暂停菜单（计时不走）
        self.act(session, ['1,B', '5'], '关掉菜单')
        second = game.ticks
        self.assertLess(second, first)
        self.assertEqual(total_time_ticks(state, session), first + second)
        self.act(session, ['40,R'], '通关核心')
        self.assertTrue(session.done)
        self.assertEqual(state.splits[0]['ticks'], first + second)
        self.assertEqual(state.completed_ticks, first + second)

    def test_plan_time_follows_progress_until_leaving_in_one_act(self):
        from orchestrator.run import total_time_ticks
        session, game = self.session()
        self.act(session, ['40'], '核心第一段')
        game.panel_target = (RIDGE, 'B')
        # 一次动作里再走 30 帧、经暂停菜单返回地图并进入 4B：离开前这 30 帧的计时由进度事件跟上
        self.act(session, ['30', '1,P', '5', '1,D', '1,D', '1,C', '5', '1,C', '45', '1,C', '5', '1,C', '30'], '走完这段就去 4B')
        self.assertTrue(session._in_detour())
        state = session.metrics.state
        self.assertEqual(state.plan_session_ticks, 70 * 170_000)
        self.assertEqual(total_time_ticks(state, session), 70 * 170_000)

    def test_next_piece_does_not_carry_previous_banked_time(self):
        session, game = self.session(mode='agent', chapters=(9, 10))
        state = session.metrics.state
        self.act(session, ['40'], '核心第一段')
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        self.leave_to_map(session, game)
        self.enter(session, game, CORE, 'A')
        self.act(session, ['40'], '核心第二段')
        self.finish_core_agent(session)
        self.assertEqual(state.splits[0]['ticks'], 80 * 170_000)       # 两段会话之和
        self.assertTrue(session.chapter_finished)
        self.enter(session, game, CHAPTER_SIDS[10], 'A')                 # 进入下一截：从这段会话重新累计
        self.assertEqual(session.piece, Piece(10))
        self.assertEqual(state.plan_banked_ticks, 0)
        self.act(session, ['40'], '告别里走一段')
        farewell = game.ticks
        self.act(session, ['40,R'], '通关告别')
        self.act(session, ['1,P', '20'], '打开暂停菜单')
        self.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.done)
        self.assertEqual([s['ticks'] for s in state.splits], [80 * 170_000, farewell])

    def test_auto_next_chapter_does_not_carry_previous_banked_time(self):
        session, game = self.session(chapters=(9, 10))
        state = session.metrics.state
        self.act(session, ['40'], '核心第一段')
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        self.leave_to_map(session, game)
        self.enter(session, game, CORE, 'A')
        self.assertEqual(state.plan_banked_ticks, 40 * 170_000)
        self.act(session, ['40'], '核心第二段')

        def macro(client, save, chapter=None, **options):     # 开局菜单宏：走完结算、选章并进入下一章
            client.frame += 300
            client._enter(CHAPTER_SIDS[chapter], 'A')
        with patch('enter_save.enter_save', side_effect=macro):
            self.act(session, ['40,R'], '通关核心')            # auto 模式：主控记成绩后自动换到告别
        self.assertEqual(state.splits[0]['ticks'], 80 * 170_000)
        self.assertEqual(session.piece, Piece(10))
        self.assertEqual(state.plan_banked_ticks, 0)          # 新的一截从这段会话重新累计
        self.act(session, ['40'], '告别里走一段')
        farewell = game.ticks
        self.act(session, ['40,R'], '通关告别')
        self.assertTrue(session.done)
        self.assertEqual([s['ticks'] for s in state.splits], [80 * 170_000, farewell])

    def test_plan_time_off_is_unchanged(self):
        session, game = self.session(detour=False, mode='agent', return_to_map=True)
        state = session.metrics.state
        self.act(session, ['40'], '核心第一段')
        self.leave_to_map(session, game)
        self.enter(session, game, CORE, 'A')
        self.act(session, ['40'], '核心第二段')
        second = game.ticks
        self.finish_core_agent(session)
        self.assertTrue(session.done)
        self.assertEqual(state.plan_banked_ticks, 0)
        self.assertEqual(state.splits[0]['ticks'], second)   # 从没开过：与原来相同，只算最后一段会话

    def test_plan_time_across_restart_resume(self):
        previous = {'chapter': CORE, 'chapter_time_ticks': 9_000_000, 'plan_session_ticks': 9_000_000}
        restarted, _ = self.session(restore=previous, game_ticks=1_000_000)     # 游戏重启后重新进入：计时更小
        self.assertEqual(restarted.metrics.state.plan_banked_ticks, 9_000_000)
        continued, _ = self.session(restore=previous, game_ticks=9_500_000)     # 同一段会话接着玩：不累加
        self.assertEqual(continued.metrics.state.plan_banked_ticks, 0)
        off, _ = self.session(detour=False, restore=previous, game_ticks=1_000_000)
        self.assertEqual(off.metrics.state.plan_banked_ticks, 0)
        extended, _ = self.session(restore=previous, game_ticks=1_000_000, extended=True)   # 追加新计划：不接上一个计划的计时
        self.assertEqual((extended.metrics.state.plan_banked_ticks, extended.metrics.state.plan_session_ticks), (0, 1_000_000))

    # ---- 开关在计划外地图里关掉
    def test_turning_off_inside_detour_still_lets_agent_leave(self):
        from orchestrator.detour import notice
        session, game = self.session()
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        session.cfg.allow_detour = False                 # 运行中在控制台关掉（session_controls 只改 cfg 与许可）
        configure(session.cfg, ActionPermissions(return_to_map=False))
        self.assertEqual(session._allowed_letters(), set(__import__('orchestrator.game_session', fromlist=['LETTERS']).LETTERS))
        policy = session._step_options()['action_policy']
        self.assertTrue(policy['permissions']['return_to_map'])        # 离开计划外地图之前返回地图照样可用
        self.assertEqual(policy['entry_target'], {'sid': CORE, 'slot': 7})
        self.leave_to_map(session, game)
        self.assertTrue(session._in_detour())
        outcome = self.enter(session, game, RESORT, 'A')
        self.assertEqual(outcome.result['stop'], 'entry_blocked')        # 关掉后不能再去别的计划外地图
        self.act(session, ['1,B', '5'], '退回选章地图')
        self.enter(session, game, CORE, 'A')
        self.assertFalse(session._in_detour())
        self.assertFalse(session._step_options()['action_policy']['permissions']['return_to_map'])
        with self.assertRaises(ActError):
            self.act(session, ['1,P', '39'])                              # 回到计划后 auto 模式照旧没有暂停键
        text = notice(session.cfg, False)
        for phrase in ('作废', '离开前 B、P、N 仍可用', '返回地图在离开它之前始终可用'):
            self.assertIn(phrase, text)
        session.cfg.cutscene_mode = 'agent'
        self.assertNotIn('B、P、N', notice(session.cfg, False))

    def test_switch_off_keeps_classifying_plan_b_side(self):
        session, game = self.session(mode='agent')
        state = session.metrics.state
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        self.act(session, ['40,R'], '吃心')
        self.act(session, ['40'], '等吃心动画')
        self.act(session, ['1,C', '5'], '确认结算')
        session.cfg.allow_detour = False
        self.enter(session, game, CORE, 'B')              # 关掉后入场只核对章节：计划章节的 B 面也进得去
        self.assertEqual(session.last_obs['level']['side'], 'B')
        self.assertEqual((session._detour['sid'], session._detour['side']), (CORE, 'B'))   # 仍算计划外地图
        self.act(session, ['40,R'], '通关 9B')
        self.assertEqual([(d['chapter'], d['side']) for d in state.detours], [(RIDGE, 'B'), (CORE, 'B')])
        self.assertFalse(session._pending_complete or session.done)
        self.assertEqual(state.splits or [], [])
        self.act(session, ['40'], '等吃心动画')
        self.act(session, ['1,C', '5'], '确认结算')
        self.enter(session, game, CORE, 'A')
        self.finish_core_agent(session)
        self.assertTrue(session.done)
        self.assertEqual([s['chapter'] for s in state.splits], [CORE])

    def test_plan_chapter_b_side_is_a_detour(self):
        session, game = self.session()
        self.leave_to_map(session, game)
        self.enter(session, game, CORE, 'B')
        self.assertEqual(session.metrics.state.detour, {'chapter': CORE, 'side': 'B'})
        self.act(session, ['40,R'], '通关 9B')
        self.assertFalse(session.done)
        self.assertEqual(session.metrics.state.splits or [], [])
        self.assertEqual([(d['chapter'], d['side']) for d in session.metrics.state.detours], [(CORE, 'B')])
        self.assertTrue(session._at_chapter_select(session.last_obs))

    def test_next_piece_needs_plan_a_side(self):
        session, game = self.session(mode='agent', chapters=(9, 4))
        self.finish_core_agent(session)
        self.assertTrue(session.chapter_finished)
        self.enter(session, game, RIDGE, 'B')              # 下一截是 4A：4B 不算进入下一截
        self.assertTrue(session.chapter_finished)
        self.assertTrue(session._in_detour())
        self.assertEqual(session.navigation.target, Piece(4))
        self.act(session, ['40,R'], '吃心')
        self.act(session, ['40'], '等吃心动画')
        self.act(session, ['1,C', '5'], '确认结算')
        self.enter(session, game, RIDGE, 'A')
        self.assertFalse(session.chapter_finished)
        self.assertEqual(session.piece, Piece(4))
        self.assertFalse(session._in_detour())
        self.assertEqual(len(session.metrics.state.splits), 1)

    def test_one_act_from_results_into_detour_commits_plan_first(self):
        session, game = self.session(mode='agent', chapters=(9, 10))
        self.act(session, ['40'], '核心里走一段')
        self.act(session, ['40,R'], '通关核心')
        self.act(session, ['1,P', '20'], '打开暂停菜单')
        self.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        self.assertTrue(session._pending_complete)
        game.panel_target = (RIDGE, 'B')
        self.act(session, ['1,C', '5', '1,C', '5', '1,C', '30'], '确认结算后直接去 4B')
        self.assertEqual(session.last_obs['level']['sid'], RIDGE)
        # 先按计划确认那次通关：成绩记在核心名下、按核心最后的计时（这一步的进度事件已换成 4B 的）
        self.assertEqual([(s['chapter'], s['ticks']) for s in session.metrics.state.splits], [(CORE, 40 * 170_000)])
        self.assertFalse(session._pending_complete)
        self.assertTrue(session.chapter_finished)
        self.assertEqual(session.metrics.state.detour, {'chapter': RIDGE, 'side': 'B'})

    def test_one_act_from_panel_completes_plan_after_detour(self):
        session, game = self.session(mode='agent')
        self.leave_to_map(session, game)
        self.enter(session, game, RIDGE, 'B')
        self.act(session, ['40,R'], '吃心')
        self.act(session, ['40'], '等吃心动画')
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session._detour_completed())
        game.panel_target = (CORE, 'A')
        self.act(session, ['1,C', '5'], '打开章节面板')
        # 一次动作：进入核心、通关、跳过片尾停在结算。先认清地图再处理事件：通关算计划的
        self.act(session, ['1,C', '20', '1,R', '10', '1,P', '5', '1,D', '1,C', '5'], '进核心并通关')
        self.assertEqual(session.last_obs['scene'], 'AreaComplete')
        self.assertIsNone(session._detour)
        self.assertTrue(session._pending_complete)
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.done)
        self.assertEqual([s['chapter'] for s in session.metrics.state.splits], [CORE])

    # ---- 按段通关：终点检查点的房间名在计划外地图（同章 B 面）里也有
    SEGMENTS = {9: [{'index': 0, 'level': 'a-00', 'name': '开头'}, {'index': 1, 'level': 'b-00', 'name': '一'},
                    {'index': 2, 'level': 'c-00', 'name': '二'}]}
    ONE_ACT_SWITCH = ['1,P', '5', '1,D', '1,D', '1,C', '5', '1,C', '45', '1,C', '5', '1,C', '20', '1,G', '5']

    def test_segment_end_room_in_detour_does_not_finish_piece(self):
        session, game = self.session(plan=[Piece(9, 0, 2)], checkpoints=self.SEGMENTS)
        self.assertEqual(session._stop_rooms(), {'stop_rooms': ['c-00']})
        self.leave_to_map(session, game)
        self.enter(session, game, CORE, 'B')
        self.assertTrue(session._in_detour())
        game.next_room = 'c-00'
        outcome = self.act(session, ['10', '1,G', '29'], '9B 里走进同名房间')
        self.assertEqual(outcome.result['stop'], 'room')
        self.assertFalse(session.done)
        self.assertEqual(session.metrics.state.splits or [], [])

    def test_segment_one_act_switches_are_judged_by_full_observation(self):
        session, game = self.session(plan=[Piece(9, 0, 2)], checkpoints=self.SEGMENTS)
        self.act(session, ['40'], '9A 走一段')
        first = game.ticks
        game.panel_target, game.next_room = (CORE, 'B'), 'c-00'
        # 一次动作里从 9A 换到 9B 并走进同名房间：进度事件不带面别，离开计划会话后不再据它判断终点
        self.act(session, self.ONE_ACT_SWITCH, '换到 9B')
        self.assertEqual(session.last_obs['level']['side'], 'B')
        self.assertTrue(session._in_detour())
        self.assertFalse(session.done)
        self.assertEqual(session.metrics.state.splits or [], [])
        # 再一次动作从 9B 回到 9A 并走进终点：桥接照常按 stop_rooms 停下，按完整观测完成这一截
        game.panel_target = (CORE, 'A')
        self.act(session, self.ONE_ACT_SWITCH, '回到 9A 走进终点')
        second = game.ticks
        self.assertTrue(session.done)
        self.assertEqual(session.done_reason, '分段完成')
        split = session.metrics.state.splits[0]
        self.assertEqual((split['ticks'], split['from'], split['to']), (first + second, 0, 2))


def hud_view(metrics):
    """游戏内 HUD 实际显示的统计与计时（overlay/modhud.view_model）。"""
    from overlay.modhud import view_model
    config = json.loads((Path(__file__).resolve().parent.parent / 'configs' / 'hud.json').read_text(encoding='utf-8'))
    m = json.loads(json.dumps(metrics.snapshot()))
    vm = view_model(m, config, [], 0.0)
    return {key: value for key, value, _ in vm.stats}, vm


def level_frames(metrics):
    return (metrics.state.game_clock or {}).get('frames')


class RunWideHudTests(unittest.TestCase):
    """HUD 的统计与主计时按一局算（开关开或关都一样）：换图、换章、绕行都不清零；中断后续跑接着算，
    续跑追加新计划才重新起算。主计时只算关卡里真正执行的帧。"""
    session, act, leave_to_map, enter = (DetourSessionTests.session, DetourSessionTests.act,
                                         DetourSessionTests.leave_to_map, DetourSessionTests.enter)

    def test_off_single_chapter_counts_from_run_start(self):
        session, game = self.session(detour=False)
        self.assertIsNone(session.metrics.state.chapter_base)
        self.assertEqual(level_frames(session.metrics), 0)
        self.act(session, ['40,X'], '死一次')
        self.act(session, ['40'], '走一段')
        stats, vm = hud_view(session.metrics)
        self.assertEqual(stats['死亡'], '1 次')
        self.assertEqual(level_frames(session.metrics), 41)          # 死亡那一步停在第 1 帧，之后 40 帧（含死亡动画）
        self.assertEqual(vm.timer_base + vm.timer_extra, round(41 * 1e7 / 60))
        self.assertEqual(vm.timer_extra, game.ticks)                  # 关卡里由桥接按章节计时逐帧走
        self.assertTrue(vm.timer_live)

    def test_off_campaign_chapter_change_does_not_reset(self):
        session, game = self.session(detour=False, mode='agent', chapters=(9, 10))
        metrics = session.metrics
        self.act(session, ['40,X'], '核心里死一次')
        self.act(session, ['40,R'], '通关核心')
        self.act(session, ['1,P', '20'], '打开暂停菜单')
        self.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.chapter_finished)
        frames = level_frames(metrics)
        self.enter(session, game, CHAPTER_SIDS[10], 'A')
        self.assertEqual(session.piece, Piece(10))
        self.assertEqual(metrics.state.chapter, CHAPTER_SIDS[10])
        self.assertIsNone(metrics.state.chapter_base)                 # 换章不重取基准
        self.act(session, ['40,X'], '告别里死一次')
        stats, _ = hud_view(metrics)
        self.assertEqual(stats['死亡'], '2 次')
        self.assertGreater(level_frames(metrics), frames)
        self.assertEqual(metrics.state.splits[0]['deaths'], 1)        # 分段成绩照旧按每一截

    def test_on_detour_deaths_and_frames_count_for_the_run(self):
        session, game = self.session(mode='agent')
        metrics = session.metrics
        self.leave_to_map(session, game)
        in_core = level_frames(metrics)
        menu_frames = sum(int(part.split(',')[0]) for step in game.steps[3:] for part in step)
        self.assertGreater(menu_frames, 0)
        self.enter(session, game, RIDGE, 'B')
        self.assertEqual(level_frames(metrics), in_core + 1)          # 选章、面板与加载的帧不算，进关卡后的帧算
        self.act(session, ['40,X'], '4B 里死一次')
        stats, vm = hud_view(metrics)
        self.assertEqual(stats['死亡'], '1 次')                        # 计划外地图的死亡照样计入
        self.assertEqual(vm.timer_base + vm.timer_extra, round(level_frames(metrics) * 1e7 / 60))
        self.act(session, ['40,R'], '吃心')
        self.act(session, ['40'], '等吃心动画')
        self.act(session, ['1,C', '5'], '确认结算')
        self.enter(session, game, CORE, 'A')
        self.act(session, ['40,R'], '通关核心')
        self.act(session, ['1,P', '20'], '打开暂停菜单')
        self.act(session, ['1,D', '1,C', '5'], '跳过片尾')
        self.act(session, ['1,C', '5'], '确认结算')
        self.assertTrue(session.done)
        self.assertEqual(metrics.state.splits[0]['deaths'], 1)        # 计划这一截的死亡包括计划外地图里的
        self.assertIsNone(metrics.state.chapter_base)
        self.assertEqual(hud_view(metrics)[0]['死亡'], '1 次')

    def test_replay_and_idle_frames_are_not_level_frames(self):
        session, game = self.session()
        self.act(session, ['40'], '走一段')
        game.frame += 900                                              # 规划回放、菜单空转：桥接帧号在走，但没有推进关卡
        self.act(session, ['40'], '再走一段')
        self.assertEqual(level_frames(session.metrics), 80)

    def test_interrupted_resume_keeps_baseline_and_level_time(self):
        session, game = self.session(detour=False)
        self.act(session, ['40,X'], '死一次')
        self.act(session, ['40'], '走一段')
        previous = json.loads(json.dumps(session.metrics.snapshot()))
        self.assertEqual(previous['game_clock']['committed'], 41)
        for detour in (False, True):
            with self.subTest(detour=detour):
                resumed, _ = self.session(detour=detour, restore={**previous, 'chapter_base': {'deaths': 0}})
                self.assertEqual(resumed.metrics.state.chapter_base, {'deaths': 0})
                self.assertEqual(level_frames(resumed.metrics), 41)
                self.act(resumed, ['40'], '接着走')
                self.assertEqual(level_frames(resumed.metrics), 81)
                self.assertEqual(hud_view(resumed.metrics)[0]['死亡'], '1 次')

    def test_resume_of_old_metrics_continues_old_hud_timer(self):
        five_minutes = 5 * 60 * 10_000_000
        old = {'chapter': CORE, 'chapter_time_ticks': five_minutes, 'completed_ticks': 0, 'deaths': 4, 'splits': []}
        session, game = self.session(detour=False, restore=old, game_ticks=five_minutes)   # 旧版主控写的统计：没有 game_clock
        self.assertEqual(level_frames(session.metrics), 5 * 60 * 60)
        _, vm = hud_view(session.metrics)
        self.assertEqual(vm.timer_base + vm.timer_extra, five_minutes)                       # 接着旧 HUD 显示的 5:00，不跳回 0
        self.act(session, ['40'], '接着走')
        self.assertEqual(level_frames(session.metrics), 5 * 60 * 60 + 40)
        campaign = {**old, 'chapter': CHAPTER_SIDS[10], 'completed_ticks': 7 * five_minutes,
                    'chapter_base': {'chapter': CHAPTER_SIDS[10], 'deaths': 1}, 'splits': [{'chapter': CORE, 'ticks': 1, 'deaths': 1}]}
        resumed, _ = self.session(detour=False, chapters=(10,), restore=campaign, start=(CHAPTER_SIDS[10], 'A'))
        self.assertEqual(level_frames(resumed.metrics), 5 * 60 * 60)                         # 旧 HUD 换章后只显示本章计时
        done = {**old, 'piece_done': True, 'completed_ticks': five_minutes}
        finished, _ = self.session(detour=False, restore=done)
        self.assertEqual(level_frames(finished.metrics), 5 * 60 * 60)                        # 这一截已计入：不重复加
        extended, _ = self.session(detour=False, restore=old, extended=True)
        self.assertEqual(level_frames(extended.metrics), 0)                                  # 追加新计划：新的一局

    def test_new_plan_resume_starts_a_new_run(self):
        previous = {'chapter': CHAPTER_SIDS[7], 'deaths': 5, 'thinking_s': 60.0,
                    'chapter_base': {'deaths': 2}, 'game_clock': {'frames': 900, 'committed': 900}}
        for detour in (False, True):
            with self.subTest(detour=detour):
                session, _ = self.session(detour=detour, restore=previous, extended=True)
                base = session.metrics.state.chapter_base
                self.assertEqual((base['deaths'], base['thinking_s']), (5, 60.0))
                self.assertEqual(level_frames(session.metrics), 0)
                self.assertEqual(hud_view(session.metrics)[0]['死亡'], '0 次')

    def test_metrics_never_reset_baseline_on_chapter_change(self):
        with tempfile.TemporaryDirectory() as folder:
            metrics = Metrics(Path(folder) / 'metrics.json', 'a', 'b', 'high', 60)
            metrics.update(chapter=CORE)
            metrics.add(deaths=3)
            metrics.update(chapter=RIDGE)
            metrics.update(chapter=CORE)
            self.assertIsNone(metrics.state.chapter_base)
            metrics.take_run_base()
            self.assertEqual(metrics.state.chapter_base['deaths'], 3)
            self.assertIsNone(metrics.state.game_clock)


class DetourConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def parse(self, *extra, previous=None):
        from orchestrator.run import parse_args
        argv = ['run', '--engine', 'codex', '--agent', 'opus5.5', '--save', 'opus5.5', '--no-sim', '--no-agent', *extra]
        with patch.object(sys, 'argv', argv), patch('orchestrator.config.REPO_ROOT', self.root), \
             patch('orchestrator.run.load_previous', return_value=previous):
            return parse_args()[0]

    def previous(self, **updates):
        state = {'agent': 'opus5.5', 'save': 'opus5.5', 'save_slot': 7, 'model': 'opus', 'effort': 'high',
                 'engine': 'codex', 'provider': 'anthropic', 'sim': False, 'runtime': 'container',
                 'play_scope': 'chapter', 'all_chapters': [8], 'plan_pieces': [[8, 0, None]],
                 'knowledge_profile': 'explore', 'mode': 'explore', 'cutscene_mode': 'auto',
                 'action_permissions': {'restart_chapter': False, 'return_to_map': False, 'save_quit': False}}
        state.update(updates)
        old = self.root / 'previous'
        old.mkdir(exist_ok=True)
        return {'state': state, 'metrics': {'splits': [{'chapter': CHAPTER_SIDS[8], 'ticks': 1, 'deaths': 0}]}, 'dir': old}

    def test_new_run_switch_enables_only_return_to_map(self):
        cfg = self.parse('--chapter', '9', '--allow-detour', '--allow-save-quit')
        self.assertTrue(cfg.allow_detour)
        self.assertEqual(cfg.action_permissions, {'restart_chapter': False, 'return_to_map': True, 'save_quit': True})
        cfg = self.parse('--chapter', '9')
        self.assertFalse(cfg.allow_detour)
        self.assertFalse(any(cfg.action_permissions.values()))
        for flags in (('--allow-detour', '--no-allow-return-to-map'),
                      ('--scope', 'visited', '--save-slot', '7', '--allow-detour')):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.parse('--chapter', '9', *flags)

    def test_resume_enables_persists_and_reuses_switch(self):
        from orchestrator.run import write_run_state
        from orchestrator.runtimes import config_from_state
        previous = self.previous()
        cfg = self.parse('--resume', str(previous['dir']), '--chapters', '9', previous=previous)
        self.assertFalse(cfg.allow_detour)                 # 默认关：原系列续跑不变
        self.assertFalse(any(cfg.action_permissions.values()))
        cfg = self.parse('--resume', str(previous['dir']), '--chapters', '9', '--allow-detour', previous=previous)
        self.assertTrue(cfg.allow_detour)
        self.assertEqual(cfg.chapters, [9])
        self.assertEqual(cfg.action_permissions, {'restart_chapter': False, 'return_to_map': True, 'save_quit': False})
        out = self.root / 'next'
        out.mkdir()
        write_run_state(out, cfg, 'session', previous)
        state = json.loads((out / 'run_state.json').read_text(encoding='utf-8'))
        self.assertIs(state['allow_detour'], True)
        self.assertTrue(config_from_state(state).allow_detour)
        again = {'state': state, 'metrics': {'splits': []}, 'dir': out}
        cfg = self.parse('--resume', str(out), previous=again)
        self.assertTrue(cfg.allow_detour)
        self.assertTrue(cfg.action_permissions['return_to_map'])
        RunControl(out).set_detour(False)                   # 运行中在控制台关掉：续跑以它为准
        self.assertFalse(self.parse('--resume', str(out), previous=again).allow_detour)
        RunControl(out).set_detour(True)
        cfg = self.parse('--resume', str(out), '--no-allow-detour', previous=again)
        self.assertFalse(cfg.allow_detour)

    def test_console_launch_and_preview_carry_switch(self):
        from orchestrator.launch_options import build, config_for, normalize
        options = {'agent': 'opus5.5', 'allow_detour': True,
                   'action_permissions': {'restart_chapter': False, 'return_to_map': False, 'save_quit': False}}
        self.assertTrue(normalize(options)['action_permissions']['return_to_map'])
        argv = build(options)
        self.assertIn('--allow-detour', argv)
        self.assertIn('--allow-return-to-map', argv)
        self.assertNotIn('--allow-detour', build({'agent': 'opus5.5'}))
        self.assertTrue(config_for(options).allow_detour)
        with self.assertRaises(ValueError):
            normalize({'agent': 'opus5.5', 'allow_detour': 'yes'})

    def test_resume_position_accepts_detour_map_only_when_on(self):
        from orchestrator.run import check_resume_position
        game = DetourGame(start=(RIDGE, 'B'))
        cfg = RunConfig(agent='opus5.5', save='opus5.5', model='test', save_slot=7, chapter=9, chapters=[9])
        with patch('tools.bridge_client.BridgeClient', return_value=game):
            with self.assertRaises(SystemExit):
                check_resume_position(cfg)
            cfg.allow_detour = True
            self.assertTrue(check_resume_position(cfg))

    def test_detour_records_and_plan_time_restore_without_death_split(self):
        metrics = Metrics(self.root / 'metrics.json', 'a', 'b', 'high', 60)
        metrics.add(deaths=3)
        metrics.add_detour(RIDGE, 'B', 50)
        metrics.update(plan_banked_ticks=40, plan_session_ticks=7, game_clock={'frames': 9, 'committed': 9})
        split = metrics.add_split(CORE, 100)
        self.assertEqual(split['deaths'], 3)                    # 死亡统一记录：计划外地图里的死亡也在这一截里
        self.assertEqual(metrics.state.detours, [{'chapter': RIDGE, 'side': 'B', 'ticks': 50}])
        self.assertFalse(hasattr(metrics.state, 'detour_deaths'))
        restored = Metrics(self.root / 'other.json', 'a', 'b', 'high', 60)
        restored.restore(json.loads((self.root / 'metrics.json').read_text(encoding='utf-8')), 'x')
        for key in ('detours', 'plan_banked_ticks', 'plan_session_ticks', 'game_clock'):
            self.assertEqual(getattr(restored.state, key), getattr(metrics.state, key), key)

    def test_detour_always_needs_return_to_map(self):
        previous = self.previous(allow_detour=True,
                                 action_permissions={'restart_chapter': False, 'return_to_map': False, 'save_quit': False})
        resume = ('--resume', str(previous['dir']), '--chapters', '9')
        cfg = self.parse(*resume, previous=previous)
        self.assertTrue(cfg.allow_detour)
        self.assertTrue(cfg.action_permissions['return_to_map'])   # 续跑沿用开关：返回地图一并打开
        for flags in (('--no-allow-return-to-map',), ('--no-allow-chapter-reset',)):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.parse(*resume, *flags, previous=previous)
        cfg = self.parse(*resume, '--no-allow-detour', '--no-allow-return-to-map', previous=previous)
        self.assertEqual((cfg.allow_detour, cfg.action_permissions['return_to_map']), (False, False))

    def test_resume_position_plan_results_take_the_original_path(self):
        from orchestrator.run import check_resume_position
        cfg = RunConfig(agent='opus5.5', save='opus5.5', model='test', save_slot=7, chapter=9, chapters=[9],
                        cutscene_mode='agent')
        cfg.allow_detour = True
        for where, expected in (((CORE, 'A'), False), ((RIDGE, 'B'), True), ((CORE, 'B'), True)):
            game = DetourGame(start=where)
            game.state = 'results'
            with self.subTest(where=where), patch('tools.bridge_client.BridgeClient', return_value=game):
                # 计划章节自己的结算走原来的路（用菜单宏进入），只有计划外地图的结算交给智能体确认
                self.assertIs(check_resume_position(cfg), expected)

    def test_total_time_counts_plan_sessions_not_detours(self):
        from orchestrator.run import total_time_ticks
        state = SimpleNamespace(completed_ticks=100, chapter_time_ticks=50, plan_banked_ticks=30, plan_session_ticks=20,
                                piece_done=False)
        session = SimpleNamespace(chapter_finished=False, done_reason='', off_plan=lambda: False)
        self.assertEqual(total_time_ticks(state, session), 180)
        session.off_plan = lambda: True                          # 停在计划外地图：只算计划章节此前的各段会话
        self.assertEqual(total_time_ticks(state, session), 150)
        state.piece_done = True
        self.assertEqual(total_time_ticks(state, session), 100)
        old = SimpleNamespace(completed_ticks=100, chapter_time_ticks=50, piece_done=False)   # 没有绕行：与原来相同
        self.assertEqual(total_time_ticks(old, SimpleNamespace(chapter_finished=False, done_reason='')), 150)


class DetourPromptTests(unittest.TestCase):
    def cfg(self, **changes):
        cfg = RunConfig(agent='opus5.5', save='opus5.5', model='test', runtime='host', chapter=9, chapters=[9],
                        mode='explore', knowledge_profile='explore')
        for key, value in changes.items():
            setattr(cfg, key, value)
        return cfg

    def test_prompt_mentions_detour_only_when_on(self):
        from orchestrator.prompting import play_prompt
        from orchestrator.startup_prompt import current_settings
        off = play_prompt(self.cfg())
        on = play_prompt(self.cfg(allow_detour=True))
        self.assertNotIn('计划外', off)
        self.assertIn('不得自行扩展。', off)
        self.assertIn(instructions(self.cfg(allow_detour=True)), on)
        for phrase in ('返回地图', '用 travel 工具请主控代为换图', '计划章节仍是本次任务', '不计入分段成绩与总计时',
                       '不要自己用暂停菜单返回地图'):
            self.assertIn(phrase, on)
        self.assertIn('travel', on)
        self.assertNotIn('结算后停在选章地图交给你', instructions(self.cfg(allow_detour=True, cutscene_mode='agent')))
        # 运行中才打开（会话开始时没有 travel 工具）：仍按自己操作菜单写
        manual = instructions(self.cfg(allow_detour=True), travel=False)
        for phrase in ('主控不代选', 'B 返回、P 暂停'):
            self.assertIn(phrase, manual)
        # 不向 agent 透露心门：绕行的说明、运行中开关的通知与返回地图的说法都不提心门或以水晶之心为理由
        from orchestrator.detour import map_rule, notice
        self.assertNotIn('心门', on)
        for text in (instructions(self.cfg(allow_detour=True)), instructions(self.cfg(allow_detour=True, cutscene_mode='agent')),
                     notice(self.cfg(allow_detour=True), True, ''), notice(self.cfg(), False, ''),
                     map_rule(self.cfg(allow_detour=True)), map_rule(self.cfg())):
            for word in ('心门', '水晶之心', 'heart'):
                self.assertNotIn(word, text)
        self.assertNotIn('detour', current_settings(self.cfg())['task'])
        self.assertEqual(current_settings(self.cfg(allow_detour=True))['task']['detour'], 'official_unlocked')
        self.assertNotIn('计划外', play_prompt(self.cfg(allow_detour=True, play_scope='visited', chapters=None)))

    def test_on_wording_matches_effective_rules(self):
        from orchestrator.detour import SCOPE_FROM, SCOPE_TO, act_note, notice
        from orchestrator.prompting import play_prompt
        from orchestrator.server import build_server
        off, on = play_prompt(self.cfg()), play_prompt(self.cfg(allow_detour=True))
        self.assertIn(SCOPE_FROM, off)
        self.assertNotIn(SCOPE_TO, off)
        self.assertIn(SCOPE_TO, on)                       # 开时不再说"只玩已配置的章节"
        self.assertNotIn(SCOPE_FROM, on)
        plan, table = [Piece(9, 0, 2)], DetourSessionTests.SEGMENTS
        partial_off = play_prompt(self.cfg(plan=plan, checkpoints=table))
        self.assertIn('不得自行扩展。', partial_off)
        self.assertNotIn('计划外', partial_off)
        partial_on = play_prompt(self.cfg(plan=plan, checkpoints=table, allow_detour=True))
        self.assertIn('计划不得自行扩展，计划外地图只作绕行（见“计划外地图”一节）。', partial_on)
        self.assertNotIn('不得自行扩展。', partial_on)
        self.assertEqual(act_note(self.cfg()), '')
        self.assertIn('换图用 travel', act_note(self.cfg(allow_detour=True, cutscene_mode='agent')))
        self.assertNotIn('B、P、N', act_note(self.cfg(allow_detour=True, cutscene_mode='agent')))   # agent 模式本来就有全部按键
        self.assertIn('换图用 travel', act_note(self.cfg(allow_detour=True)))
        self.assertIn('B、P、N 只在主控交回的结算或菜单界面里用', act_note(self.cfg(allow_detour=True)))
        self.assertIn('以这里为准', notice(self.cfg(allow_detour=True), True))          # 运行中开启：说明以通知为准
        descriptions = {}
        for detour in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                cfg = self.cfg(allow_detour=detour, sim=False)
                session = GameSession(cfg, Path(folder), Metrics(Path(folder) / 'm.json', 'a', 'b', 'high', 60))
                try:
                    tools = asyncio.run(build_server(session).list_tools())
                    descriptions[detour] = next(t.description for t in tools if t.name == 'act')
                finally:
                    session.close(release=False)
        # act 工具说明：关时与原来相同，开时（auto 剧情模式）只在末尾多一句按键说明
        self.assertNotIn('计划外', descriptions[False])
        self.assertEqual(descriptions[True], descriptions[False] + act_note(self.cfg(allow_detour=True)))

    def test_exit_level_wording_stays_consistent(self):
        from orchestrator.prompting import play_prompt
        from orchestrator.server import build_server
        for detour, expected in ((False, '由用户在控制台调整范围'), (True, '自行绕行到其他已解锁的官方地图')):
            with self.subTest(detour=detour), tempfile.TemporaryDirectory() as folder:
                cfg = self.cfg(live=True, allow_detour=detour, mode='speedrun', knowledge_profile='full', sim=False)
                session = GameSession(cfg, Path(folder), Metrics(Path(folder) / 'm.json', 'a', 'b', 'high', 60))
                try:
                    tools = asyncio.run(build_server(session).list_tools())
                    description = next(t.description for t in tools if t.name == 'exit_level')
                finally:
                    session.close(release=False)
                for text in (description, play_prompt(cfg)):
                    self.assertIn('只进入当前计划或许可允许的地图', text)
                    self.assertIn(expected, text)
                    self.assertNotIn('心门', text)


class DetourControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from orchestrator.agent import ClaudeRunner
        from orchestrator.goals import GoalManager
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent='test', save='test', model='sonnet', runtime='host', chapter=9, chapters=[9])
        self.cfg.provider_info = {'models': [{'id': 'sonnet', 'efforts': ['low', 'high']}]}
        configure(self.cfg, ActionPermissions(save_quit=True))
        self.metrics = Metrics(self.root / 'metrics.json', 'test', 'sonnet', 'high', 100)
        self.session = SimpleNamespace(cfg=self.cfg, metrics=self.metrics, workspace=None, action_policy_supported=1,
                                       map_entry_set_supported=True, done=False, done_reason='',
                                       goals=GoalManager(self.root), goal_values=lambda: {'map_complete': False})
        self.runner = ClaudeRunner(self.cfg, self.root, self.session, self.metrics, '')
        self.packets = []

        def write(raw):
            packet = json.loads(raw)
            self.packets.append(packet)
            if packet['type'] == 'control_request':
                self.runner._handle({'type': 'control_response', 'response': {
                    'request_id': packet['request_id'], 'subtype': 'success'}})
        self.runner.proc = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()))
        self.task = None

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.runner._events.close()
        self.temp.cleanup()

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(.01)

    def action(self, ident):
        return next(a for a in self.runner.control.snapshot()['actions'] if a['id'] == ident)

    async def test_live_toggle_enables_return_to_map_persists_and_notifies(self):
        self.task = asyncio.create_task(self.runner._controls())
        await self.until(lambda: self.runner.control.snapshot().get('detour_control'))
        self.assertIs(self.runner.control.snapshot()['allow_detour'], False)
        with self.assertRaises(ValueError):
            self.runner.control.enqueue('detour', 'yes')
        ident = self.runner.control.enqueue('detour', 'on')
        await self.until(lambda: self.action(ident)['status'] == 'waiting')
        self.assertTrue(self.cfg.allow_detour)
        self.assertIs(self.runner.control.detour_preference(), True)
        self.assertEqual(permissions_for(self.cfg).snapshot(),
                         {'restart_chapter': False, 'return_to_map': True, 'save_quit': True})
        self.assertEqual(self.runner.control.action_permissions_preference()['return_to_map'], True)
        await self.until(lambda: self.runner.control.snapshot().get('allow_detour') is True)
        await self.runner._next_user_turn('continue')
        text = self.packets[-1]['message']['content']
        self.assertIn('用户开启了计划外地图', text)
        self.assertIn('返回地图=允许', text)
        ident = self.runner.control.enqueue('detour', 'off')
        await self.until(lambda: self.action(ident)['status'] == 'waiting')
        self.assertFalse(self.cfg.allow_detour)
        self.assertTrue(permissions_for(self.cfg).return_to_map)   # 关掉不收回返回地图权限
        self.assertIs(self.runner.control.detour_preference(), False)

    async def test_return_to_map_cannot_be_denied_while_detour_on(self):
        self.task = asyncio.create_task(self.runner._controls())
        await self.until(lambda: self.runner.control.snapshot().get('detour_control'))
        ident = self.runner.control.enqueue('detour', 'on')
        await self.until(lambda: self.action(ident)['status'] == 'waiting')
        for kind, text in (('action_permissions', json.dumps({'return_to_map': False})), ('chapter_reset', 'deny')):
            with self.subTest(kind=kind):
                ident = self.runner.control.enqueue(kind, text)
                await self.until(lambda: self.action(ident)['status'] == 'error')
                self.assertIn('先关闭计划外地图', self.action(ident)['detail'])
                self.assertTrue(permissions_for(self.cfg).return_to_map)
        ident = self.runner.control.enqueue('action_permissions', json.dumps({'save_quit': False}))
        await self.until(lambda: self.action(ident)['status'] == 'waiting')     # 其他两项照常可改
        self.assertFalse(permissions_for(self.cfg).save_quit)
        ident = self.runner.control.enqueue('detour', 'off')
        await self.until(lambda: self.action(ident)['status'] == 'waiting')
        ident = self.runner.control.enqueue('action_permissions', json.dumps({'return_to_map': False}))
        await self.until(lambda: self.action(ident)['status'] == 'waiting')     # 关掉计划外地图之后才能禁止
        self.assertFalse(permissions_for(self.cfg).return_to_map)

    async def test_live_toggle_rejected_without_bridge_or_in_visited_scope(self):
        self.session.map_entry_set_supported = False
        self.task = asyncio.create_task(self.runner._controls())
        ident = self.runner.control.enqueue('detour', 'on')
        await self.until(lambda: self.action(ident)['status'] == 'error')
        self.assertIn('1.10.0', self.action(ident)['detail'])
        self.assertFalse(self.cfg.allow_detour)
        self.cfg.play_scope = 'visited'
        await self.until(lambda: self.runner.control.snapshot().get('detour_control') is False)


class DetourUiTests(unittest.TestCase):
    def test_console_and_hud_toggle_render_and_send(self):
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Node needed for real UI state code')
        root = Path(__file__).resolve().parents[1]
        for filename, ident, first, last in (
            ('console.html', 'liveDetour', '  const detourInput = $("liveDetour");', '  $("fastToggle").hidden'),
            ('hud.html', 'permissionDetour', "    const detourInput=$('permissionDetour');", "    $('cutsceneMode').value="),
        ):
            source = (root / 'overlay' / filename).read_text(encoding='utf-8')
            start = source.index(first)
            end = source.index(last, start)
            for control, expected in (({'connected': True, 'detour_control': True, 'allow_detour': True}, ('allow', False)),
                                      ({'connected': True, 'detour_control': True, 'allow_detour': False,
                                        'detour_supported': False}, ('deny', True)),
                                      ({'connected': True, 'detour_control': False}, ('deny', True))):
                script = ('const inputs={}; const $=id=>inputs[id]??={}; const document={activeElement:null};'
                          f'const c={json.dumps(control)}; const connected=c.connected,busy=false,pendingPermissions=false;'
                          + source[start:end] + f'process.stdout.write(JSON.stringify(inputs["{ident}"]));')
                result = json.loads(subprocess.run([node, '-e', script], capture_output=True, text=True, check=True).stdout)
                with self.subTest(filename=filename, control=control):
                    self.assertEqual((result['value'], result['disabled']), expected)
            self.assertIn("sendControl(\"detour\", e.target.value === \"allow\" ? \"on\" : \"off\")" if filename == 'console.html'
                          else "sendControl('detour',$('permissionDetour').value==='allow' ? 'on' : 'off')", source)
        for filename, first, last, ident in (
            ('console.html', "  for (const [key,id] of [['restart_chapter','RestartChapter'],['return_to_map','ReturnToMap'],['save_quit','SaveQuit']]) {\n    const input = $('live'+id);",
             '  const detourInput = $("liveDetour");', 'liveReturnToMap'),
            ('hud.html', "    for (const [key,id] of [['restart_chapter','RestartChapter'],['return_to_map','ReturnToMap'],['save_quit','SaveQuit']]) {\n      const input=$('permission'+id);",
             "    const detourInput=$('permissionDetour');", 'permissionReturnToMap'),
        ):
            source = (root / 'overlay' / filename).read_text(encoding='utf-8')
            start = source.index(first)
            end = source.index(last, start)
            for detour, disabled in ((True, True), (False, False)):
                control = {'connected': True, 'chapter_reset_control': True, 'action_policy_supported': True,
                           'allow_detour': detour, 'action_permissions': {'return_to_map': True}}
                script = ('const inputs={}; const $=id=>inputs[id]??={}; const document={activeElement:null};'
                          f'const c={json.dumps(control)}; const connected=c.connected,busy=false,pendingPermissions=false;'
                          + source[start:end] + f'process.stdout.write(JSON.stringify(inputs["{ident}"]));')
                result = json.loads(subprocess.run([node, '-e', script], capture_output=True, text=True, check=True).stdout)
                with self.subTest(filename=filename, detour=detour):
                    self.assertEqual((result['value'], result['disabled']), ('allow', disabled))
        console = (root / 'overlay' / 'console.html').read_text(encoding='utf-8')
        self.assertIn('$("allowReturnToMap").addEventListener("change", (e) => { if (!e.target.checked) $("allowDetour").checked = false; });', console)
        self.assertIn('allow_detour: $("allowDetour").checked', console)
        self.assertIn('允许去计划外的已解锁地图', console)


if __name__ == '__main__':
    unittest.main()
