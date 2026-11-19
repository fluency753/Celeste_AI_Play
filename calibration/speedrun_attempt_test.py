import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibration.detour_test import DetourGame
from orchestrator.action_policy import ActionPermissions, configure
from orchestrator.config import CHAPTER_SIDS, RunConfig
from orchestrator.game_session import ActError, GameSession, REQUEST_TICKET, RequestTicket
from orchestrator.metrics import Metrics
from orchestrator.server import build_server

CITY = CHAPTER_SIDS[1]


class SpeedrunBridge(DetourGame):
    def _enter(self, sid, side):
        super()._enter(sid, side)
        self.attempt = getattr(self, 'attempt', 0) + 1
        self.valid_time = True
        self.captured_head = False
        self.respawn = None

    def observation(self):
        obs = super().observation()
        obs['saving'] = False
        if obs.get('level'):
            obs['level'].update(chapterTimeValid=self.valid_time, chapterSessionId=self.attempt,
                                chapterRecordedFromStart=self.captured_head)
            if self.respawn is not None:
                obs['respawnPoint'] = self.respawn
                obs['player']['pos'] = self.respawn
        if obs.get('completion'):
            obs['completion']['chapterTimeValid'] = self.valid_time
            obs['completion']['chapterRecordedFromStart'] = self.captured_head
        return obs

    def call(self, command, **options):
        if command == 'speedrun_goto_room':
            self.commands.append(command)
            assert options['slot'] == self.slot and options['sid'] == CITY and options['mode'] == 0
            spawns = [[10, 20], [40, 50]]
            if 'room' not in options:
                return {'rooms': [{'room': 'practice', 'spawns': spawns}]}
            assert options['allow_reconnect'] is False
            if options['room'] != 'practice':
                raise RuntimeError('practice room does not exist')
            index = options.get('spawn_index', 0)
            if index >= len(spawns):
                raise RuntimeError('spawn_index must be from 0 to 1')
            self.room, self.valid_time, self.ticks = options['room'], False, 0
            self.respawn, self.captured_head = spawns[index], False
            self.attempt += 1
            return {'room': self.room, 'spawn': self.respawn}
        reply = super().call(command, **options)
        if command == 'hello':
            reply['speedrun_practice_room'] = True
            reply['speedrun_goto_room'] = True
            reply['speedrun_recorded_attempt'] = True
        if command == 'step':
            self.step_speed = options.get('speed')
        if command == 'step' and not self.valid_time:
            for event in reply['result']['events']:
                if event['type'] == 'complete':
                    event['detail'] = 'invalid_chapter_time'
        return reply


class SpeedrunAttemptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = RunConfig(agent='test', save='test', save_slot=7, chapter=1, mode='speedrun',
                             model='test', cutscene_mode='agent', collision_image='off')
        self.cfg.thought_lead_s = 0
        configure(self.cfg, ActionPermissions(restart_chapter=True, return_to_map=True))
        self.metrics = Metrics(self.root/'metrics.json', 'test', 'test', 'medium', 120)
        self.session = GameSession(self.cfg, self.root, self.metrics)
        self.addCleanup(self.session.close, release=False)
        self.bridge = SpeedrunBridge(start=(CITY, 'A'), save='test')
        with patch('orchestrator.game_session.BridgeClient', return_value=self.bridge):
            self.session.connect()

    async def test_tool_can_repeat_practice_and_records_native_eligibility(self):
        server = build_server(self.session)
        for _ in range(2):
            result = await server.call_tool('goto_room', {'room': 'practice'})
            self.assertFalse(result.is_error, result)
            payload = json.loads(result.content[0].text.splitlines()[0])
            self.assertIs(payload['chapter_time_valid'], False)
            self.assertNotEqual(payload['attempt'], payload['previous_attempt'])
            self.assertEqual(payload['room'], 'practice')
            self.assertIs(self.metrics.state.chapter_time_valid, False)

    def test_gray_completion_does_not_finish_or_restrict_settlement(self):
        self.cfg.cutscene_mode = 'auto'
        self.session.goto_room('practice')
        self.session.set_goal({'conditions': [{'field': 'map_complete', 'op': 'eq', 'value': True}]})
        self.session.act(['1,R'], '跑到练习终点', None)
        self.assertFalse(self.session.done)
        self.assertFalse(self.session._pending_complete)
        self.assertFalse(self.session.goal_values()['map_complete'])
        self.assertFalse(self.session.goals.stop_motion.is_set())
        self.assertFalse(self.metrics.state.splits)
        self.session.act(['1,P'], '打开结尾菜单', None)
        self.assertEqual(self.bridge.state, 'pause')

    def test_native_checkpoint_gray_does_not_poison_later_valid_score(self):
        self.bridge.valid_time = False
        self.session.act(['1,R'], '完成检查点练习', None)
        self.assertFalse(self.session.done)
        self.assertFalse(self.metrics.state.splits)
        self.bridge._enter(CITY, 'A')
        self.bridge.ticks = 12_000_000
        self.metrics.update(plan_banked_ticks=99_000_000)
        self.session.act(['1,R'], '从章首开始的有效尝试通关', None)
        self.assertTrue(self.session._pending_complete)
        self.bridge.state = 'select'
        self.session._commit_completion(self.bridge.observation())
        self.assertTrue(self.session.done)
        self.assertEqual(self.metrics.state.splits[0]['ticks'], 12_000_000)

    def test_denied_or_unknown_room_keeps_the_original_observation(self):
        before = self.session.last_obs
        configure(self.cfg, ActionPermissions())
        with self.assertRaisesRegex(ActError, '重启章节权限'):
            self.session.goto_room('practice')
        self.assertNotIn('speedrun_goto_room', self.bridge.commands)
        configure(self.cfg, ActionPermissions(restart_chapter=True))
        with self.assertRaisesRegex(ActError, 'does not exist'):
            self.session.goto_room('missing')
        self.assertIs(self.session.last_obs, before)
        self.assertTrue(self.bridge.valid_time)

    def test_expired_model_turn_cannot_send_room_swap(self):
        ticket = REQUEST_TICKET.set(RequestTicket(lambda: '发起该请求的轮次已结束'))
        try:
            with self.assertRaisesRegex(ActError, '轮次已结束'):
                self.session.goto_room('practice')
        finally:
            REQUEST_TICKET.reset(ticket)
        self.assertNotIn('speedrun_goto_room', self.bridge.commands)

    def test_goto_lists_current_map_without_advancing_and_selects_native_spawn(self):
        before = self.bridge.frame
        listing = self.session.goto_room()
        self.assertEqual(listing['rooms'], [{'room': 'practice', 'spawns': [[10, 20], [40, 50]]}])
        self.assertEqual(self.bridge.frame, before)
        with self.assertRaisesRegex(ActError, 'spawn_index must be'):
            self.session.goto_room('practice', 2)
        self.assertEqual(self.bridge.frame, before)
        result = self.session.goto_room('practice', 1)
        self.assertEqual(result['respawn_point'], [40, 50])
        self.assertFalse(result['chapter_time_valid'])
        self.bridge.state = 'select'
        with self.assertRaisesRegex(ActError, '进入目标章节'):
            self.session.goto_room()

    def set_speedrun_target(self, ticks=20_000_000):
        self.cfg.speedrun_target_ticks = self.session.speedrun_target_ticks = ticks

    async def test_player_arms_once_from_menu_and_only_recorded_attempt_qualifies(self):
        self.set_speedrun_target()
        self.cfg.speedrun_record_on_demand = True
        self.cfg.play_speed = 4
        starts = []
        def open_recording(authorize):
            authorize()
            starts.append(self.bridge.state)
        self.session.open_speedrun_recording = open_recording
        self.session.verify_speedrun_capture = lambda: bool(starts)
        server = build_server(self.session)
        from mcp.server.mcpserver.exceptions import ToolError
        with self.assertRaisesRegex(ToolError, '稳定选章界面'):
            await server.call_tool('start_speedrun_recording', {})
        self.assertEqual(starts, [])
        self.bridge.ticks = 19_000_000
        self.session.act(['1,R'], '跑完练习', None)
        self.assertEqual(self.bridge.step_speed, 4)
        self.assertFalse(self.metrics.state.speedrun_last_attempt['recorded_from_start'])
        self.assertFalse(self.session.done)
        self.assertFalse(self.session._pending_complete)
        self.bridge.state = 'select'
        for _ in range(2):
            result = await server.call_tool('start_speedrun_recording', {})
            self.assertFalse(result.is_error, result)
        self.assertEqual(starts, ['select'])
        self.assertTrue(self.session.time_snapshot()['speedrun']['recording'])
        self.assertEqual(self.session._step_options()['speed'], 1)
        self.bridge._enter(CITY, 'A')
        self.assertFalse(self.session.goto_room('practice')['chapter_time_valid'])
        self.assertTrue(self.session.speedrun_recording)
        for captured, continuous, ticks, passes in (
                (False, True, 19_000_000, False),
                (True, False, 19_000_000, False),
                (True, True, 20_000_000, False),
                (True, True, 19_999_999, True)):
            with self.subTest(captured=captured, continuous=continuous, ticks=ticks):
                self.bridge._enter(CITY, 'A')
                self.bridge.captured_head = captured
                self.session.verify_speedrun_capture = lambda: continuous
                self.bridge.ticks = ticks
                self.session.act(['1,R'], '跑完本次尝试', None)
                self.assertEqual(self.bridge.step_speed, 1)
                self.bridge.state = 'select'
                self.session.observe()
                self.assertEqual(self.session.done, passes)
        self.assertEqual([row['ticks'] for row in self.metrics.state.splits], [19_999_999])

    def test_expired_turn_cannot_start_capture(self):
        self.set_speedrun_target()
        self.cfg.speedrun_record_on_demand = True
        self.bridge.state = 'select'
        self.session.open_speedrun_recording = lambda _: self.fail('expired request started capture')
        ticket = REQUEST_TICKET.set(RequestTicket(lambda: '发起该请求的轮次已结束'))
        try:
            with self.assertRaisesRegex(ActError, '轮次已结束'):
                self.session.start_speedrun_recording()
        finally:
            REQUEST_TICKET.reset(ticket)

    def test_request_expiring_while_obs_initializes_cannot_start_capture(self):
        self.set_speedrun_target()
        self.cfg.speedrun_record_on_demand = True
        self.bridge.state = 'select'
        expired = False
        def open_recording(authorize):
            nonlocal expired
            expired = True
            authorize()
            self.fail('expired request started capture')
        self.session.open_speedrun_recording = open_recording
        ticket = REQUEST_TICKET.set(RequestTicket(lambda: '发起该请求的轮次已结束' if expired else None))
        try:
            with self.assertRaisesRegex(ActError, '轮次已结束'):
                self.session.start_speedrun_recording()
        finally:
            REQUEST_TICKET.reset(ticket)
        self.assertFalse(self.session.speedrun_recording)

    def test_timed_goal_retries_equal_slow_and_gray_then_records_current_valid_ticks(self):
        self.set_speedrun_target()
        self.session.set_goal({'conditions': [{'field': 'map_complete', 'op': 'eq', 'value': True}]})
        for ticks, valid in ((20_000_000, True), (20_000_001, True), (10_000_000, False), (19_999_999, True)):
            with self.subTest(ticks=ticks, valid=valid):
                self.bridge._enter(CITY, 'A')
                self.bridge.ticks, self.bridge.valid_time = ticks, valid
                self.metrics.update(plan_banked_ticks=99_000_000)
                self.session.act(['1,R'], '跑到终点', None)
                self.assertFalse(self.session.done)
                self.assertFalse(self.session.goals.stop_motion.is_set())
                self.bridge.state = 'select'
                self.session.observe()
                self.assertEqual(self.session.done, valid and ticks < 20_000_000)
        self.assertTrue(self.session.done)
        self.assertEqual([row['ticks'] for row in self.metrics.state.splits], [19_999_999])
        self.assertEqual(self.session.time_snapshot()['speedrun']['target_seconds'], 2)

    def test_auto_cutscene_slow_finish_leaves_menu_controls_available(self):
        self.set_speedrun_target()
        self.cfg.cutscene_mode = 'auto'
        self.bridge.ticks = 21_000_000
        result = self.session.act(['1,R'], '跑到终点', None)
        self.assertIn('未达到严格低于 2 秒', ' '.join(result.auto_notes))
        self.session.act(['1,P'], '打开结尾菜单', None)
        self.assertEqual(self.bridge.state, 'pause')
        self.assertFalse(self.session.done)
        self.assertFalse(self.metrics.state.splits)
        with self.assertRaisesRegex(ActError, '由主控核验'):
            self.session.finish_goal('已完成', '本次通关')

    def test_qualified_results_survive_reconnect_and_same_map_reentry(self):
        self.set_speedrun_target()
        self.bridge.state, self.bridge.ticks = 'results', 15_000_000
        with patch('orchestrator.game_session.BridgeClient', return_value=self.bridge):
            self.session.connect()
        self.session.observe()
        self.assertTrue(self.session._pending_complete)
        self.assertFalse(self.session.done)
        self.bridge._enter(CITY, 'A')
        self.session.observe()
        self.assertTrue(self.session.done)
        self.assertEqual([row['ticks'] for row in self.metrics.state.splits], [15_000_000])
