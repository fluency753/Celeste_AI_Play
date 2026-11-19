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

    def observation(self):
        obs = super().observation()
        if obs.get('level'):
            obs['level'].update(chapterTimeValid=self.valid_time, chapterSessionId=self.attempt)
        if obs.get('completion'):
            obs['completion']['chapterTimeValid'] = self.valid_time
        return obs

    def call(self, command, **options):
        if command == 'speedrun_practice_room':
            self.commands.append(command)
            assert options['slot'] == self.slot and options['sid'] == CITY and options['mode'] == 0
            assert options['allow_reconnect'] is False
            if options['room'] != 'practice':
                raise RuntimeError('practice room does not exist')
            self.room, self.valid_time, self.ticks = options['room'], False, 0
            self.attempt += 1
            return {'room': self.room}
        reply = super().call(command, **options)
        if command == 'hello':
            reply['speedrun_practice_room'] = True
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
            result = await server.call_tool('practice_room', {'room': 'practice'})
            self.assertFalse(result.is_error, result)
            payload = json.loads(result.content[0].text.splitlines()[0])
            self.assertIs(payload['chapter_time_valid'], False)
            self.assertNotEqual(payload['attempt'], payload['previous_attempt'])
            self.assertEqual(payload['room'], 'practice')
            self.assertIs(self.metrics.state.chapter_time_valid, False)

    def test_gray_completion_does_not_finish_or_restrict_settlement(self):
        self.cfg.cutscene_mode = 'auto'
        self.session.practice_room('practice')
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
            self.session.practice_room('practice')
        self.assertNotIn('speedrun_practice_room', self.bridge.commands)
        configure(self.cfg, ActionPermissions(restart_chapter=True))
        with self.assertRaisesRegex(ActError, 'does not exist'):
            self.session.practice_room('missing')
        self.assertIs(self.session.last_obs, before)
        self.assertTrue(self.bridge.valid_time)

    def test_expired_model_turn_cannot_send_room_swap(self):
        ticket = REQUEST_TICKET.set(RequestTicket(lambda: '发起该请求的轮次已结束'))
        try:
            with self.assertRaisesRegex(ActError, '轮次已结束'):
                self.session.practice_room('practice')
        finally:
            REQUEST_TICKET.reset(ticket)
        self.assertNotIn('speedrun_practice_room', self.bridge.commands)
