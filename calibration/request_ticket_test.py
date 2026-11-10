"""A model request that became void while the act waited must not reach the game."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest

from calibration.token_menu_test import MenuBridge, client_for
from orchestrator.config import RunConfig
from orchestrator.game_session import ActError, GameSession, REQUEST_TICKET, RequestTicket
from orchestrator.metrics import Metrics


class SlowPlayback:
    """Planning playback still draining when the act starts."""
    def __init__(self, during):
        self.during = during

    def drain(self):
        self.during()

    def final(self):
        pass


class RequestTicketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cfg = RunConfig(agent='fixture', save='fixture', save_slot=15, model='fixture', engine='codex',
                        mode='explore', knowledge_profile='explore', runtime='host', sim=False,
                        cutscene_mode='agent', collision_image='off', thought_lead_s=0)
        self.session = GameSession(cfg, self.root, Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'medium', 600))
        self.addCleanup(self.session.close)
        self.session.game_process_id = 123
        self.bridge = MenuBridge()
        self.session.client = client_for(self.session, self.bridge)
        self.session.last_obs = copy.deepcopy(self.bridge.obs)
        self.session._held_input = ('', 'celeste_ai')
        self.session._start_level_clock(self.session.last_obs)

    def act_with(self, ticket):
        token = REQUEST_TICKET.set(ticket)
        try:
            return self.session.act(['10,R'], '这一段向右试探', None, None)
        finally:
            REQUEST_TICKET.reset(token)

    def published(self):
        path = self.root/'public_events.jsonl'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def test_request_voided_during_playback_drain_sends_nothing_and_shows_no_thought(self):
        valid = threading.Event()
        valid.set()
        self.session.playback = SlowPlayback(valid.clear)      # the turn ends while the act waits
        with self.assertRaisesRegex(ActError, '轮次已结束'):
            self.act_with(RequestTicket(lambda: None if valid.is_set() else '发起此请求的轮次已结束，动作未执行'))
        self.assertEqual(self.bridge.steps, [])
        self.assertNotIn('这一段向右试探', self.published())

    def test_request_voided_at_the_step_itself_sends_nothing(self):
        checks = []
        def check():
            checks.append(1)
            return None if len(checks) < 2 else '模型连接中断后已重新生成，这次请求作废，动作未执行'
        self.session.playback = SlowPlayback(lambda: None)
        with self.assertRaisesRegex(ActError, '请求作废'):
            self.act_with(RequestTicket(check))
        self.assertEqual(self.bridge.steps, [])

    def test_valid_request_runs_and_keeps_its_executed_range_after_it_starts(self):
        ticket = RequestTicket(lambda: None)
        self.act_with(ticket)
        self.assertTrue(ticket.started)
        self.assertEqual(self.bridge.steps[0]['inputs'], ['10,R'])
        ticket.check = lambda: '已结束'            # later steps of a started request are not cut off
        ticket.before_input()

    def test_controller_steps_without_a_ticket_are_unaffected(self):
        self.session.act(['3,R'], '主控外的动作', None, None)
        self.assertEqual(self.bridge.steps[0]['inputs'], ['3,R'])


if __name__ == '__main__':
    unittest.main()
