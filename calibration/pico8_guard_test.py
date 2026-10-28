"""PICO-8 refusal through production session and tool formatting; no game connection."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from calibration.collision_contract_test import FakeBridge, observation
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server
from tools.menu_navigation import checked_step, MenuActionBlocked


class PicoBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.steps = 0

    def call(self, command, **args):
        if command != 'step':
            return super().call(command, **args)
        self.steps += 1
        self.obs = observation(self.obs['frame'] + 1)
        self.obs['controllable'] = False  # A block must never trigger implicit waiting.
        return {'obs': copy.deepcopy(self.obs), 'result': {
            'consumed': 1, 'planned': 2, 'stop': 'unsupported_scene_blocked', 'events': [],
            'block': {'rule_id': 'unsupported.pico8', 'action': 'enter_pico8',
                      'reason': '这是内置的 PICO-8 小游戏，暂不支持。'}}}


class Pico8GuardTests(unittest.TestCase):
    def test_connect_rejects_missing_guard_before_activate(self):
        for hello in ({}, {'pico8_entry_guard': False}):
            with self.subTest(hello=hello), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = RunConfig(agent='isolated', save='test', model='stub', runtime='host')
                session = GameSession(cfg, root, Metrics(root/'metrics.json', 'test', 'stub', 'low', 60))
                client = Mock()
                client.call.return_value = hello
                try:
                    with patch('orchestrator.game_session.BridgeClient', return_value=client), \
                         self.assertRaisesRegex(ValueError, 'PICO-8'):
                        session.connect()
                    self.assertEqual([c.args[0] for c in client.call.call_args_list], ['hello'])
                finally:
                    session.close(release=False)

    def test_tool_reports_reason_and_executed_scope_without_auto_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = RunConfig(agent='isolated', save='test', model='stub', engine='codex', runtime='host',
                            sim=False, chapter=3, cutscene_mode='agent', collision_image='off')
            cfg.min_frames_per_act = 1
            session = GameSession(cfg, root, Metrics(root/'metrics.json', 'test', 'stub', 'low', 60))
            game = PicoBridge()
            session.client = game
            session.last_obs = observation()
            session.entry_guard_supported = session.chapter_reset_guard_supported = True
            session.action_policy_supported = 1
            try:
                server = build_server(session)
                acted = asyncio.run(server.call_tool('act', {'inputs': ['2,T'], 'thought': '尝试街机入口'}))
                text = '\n'.join(b.text for b in acted.content if hasattr(b, 'text'))
                for phrase in ('内置的 PICO-8 小游戏', '暂不支持', '未进入小游戏', '后续按键没有执行', '继续本体关卡'):
                    self.assertIn(phrase, text)
                self.assertNotIn('暂停菜单仍开着', text)
                self.assertEqual(game.steps, 1)
                self.assertEqual(session.last_obs['scene'], 'Level')
                self.assertEqual(session.metrics.state.frames_played, 1)
            finally:
                session.close(release=False)

    def test_menu_macro_does_not_retry_unsupported_entry(self):
        game = PicoBridge()
        with self.assertRaisesRegex(MenuActionBlocked, 'PICO-8.*暂不支持'):
            checked_step(game, ['2,T'])
        self.assertEqual(game.steps, 1)


if __name__ == '__main__':
    unittest.main()
