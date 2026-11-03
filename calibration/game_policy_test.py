"""The two adapters consume the same present facts and tool categories."""
from contextlib import ExitStack
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.config import RunConfig
from orchestrator.game_policy import CONTROL_TOOLS, GAME_ACTIONS, denial, is_game_tool


class AdapterPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_adapters_use_current_policy_at_actual_server_entry(self):
        from orchestrator.agent import ClaudeRunner
        from orchestrator.codex_agent import CodexRunner
        from orchestrator.game_session import GameSession
        from orchestrator.metrics import Metrics
        from orchestrator.server import build_server
        for adapter, engine in ((ClaudeRunner, 'claude'), (CodexRunner, 'codex')):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder, \
                 patch('orchestrator.config.REPO_ROOT', Path(folder)), ExitStack() as cleanup:
                root = Path(folder)
                cfg = RunConfig(agent='test', save='test', model='stub', engine=engine,
                                mode='explore', knowledge_profile='explore', sim_assist=True,
                                cutscene_mode='agent')
                metrics = Metrics(root / 'metrics.json', 'test', 'stub', 'high', 100)
                session = GameSession(cfg, root, metrics)
                try:
                    server = build_server(session)
                    runner = (adapter(cfg, root, session, metrics, 'fixture', game_server=server)
                              if engine == 'codex' else adapter(cfg, root, session, metrics, 'fixture'))
                    cleanup.callback(runner.close_events)
                    runner.turn_active, runner.turn_id = True, 'turn'
                    snapshots = []
                    session.client = SimpleNamespace(call=lambda *a, **kw: snapshots.append(a) or {'state': {'frame': 9}})
                    prediction = {'info': {'frame': 9}, 'results': [{'stop': 'done', 'frames': [[10, 1, 2]]}]}
                    async def invoke(ident):
                        args = {'candidates': [['1,R']], 'request_id': ident}
                        if engine == 'codex':
                            result = await runner._request('item/tool/call', {
                                'tool': 'celeste_simulate', 'callId': ident, 'arguments': args})
                            return result['contentItems'][0]['text']
                        return (await server.call_tool('simulate', args)).content[0].text
                    with patch('orchestrator.simulation.available', return_value=True), \
                         patch('orchestrator.sim_assist.run_predictions', return_value=prediction) as worker:
                        successful = json.loads(await invoke('running'))
                        self.assertEqual(successful['energy']['balance'], 57)
                        for flag in ('paused', 'pausing', 'prepare_only', 'chat_only',
                                     'goal_finishing', 'model_waiting', 'stop_requested'):
                            setattr(runner, flag, True)
                            text = await invoke(flag)
                            self.assertNotIn('Error executing tool', text)
                            self.assertNotIn('"receipt"', text)
                            setattr(runner, flag, False)
                        cfg.sim_assist = False
                        self.assertIn('未启用', await invoke('disabled'))
                        worker.assert_called_once()
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(session.sim_energy.snapshot()['balance'], 57)
                finally:
                    session._frames_log.close()
                    session._decisions_log.close()

    async def test_control_effort_is_available_in_chat_but_manual_choice_is_respected(self):
        from orchestrator.agent import ClaudeRunner
        from orchestrator.codex_agent import CodexRunner
        from orchestrator.metrics import Metrics
        from orchestrator.server import build_server
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder, \
                 patch('orchestrator.config.REPO_ROOT', Path(folder)), ExitStack() as cleanup:
                root = Path(folder)
                cfg = RunConfig(agent='test', save='test', model='stub', engine=engine)
                metrics = Metrics(root / 'metrics.json', 'test', 'stub', 'high', 100)
                if engine == 'claude':
                    session = SimpleNamespace(cfg=cfg, metrics=metrics, workspace=None, done=False, run_dir=root)
                    server = build_server(session)
                    runner = ClaudeRunner(cfg, root, session, metrics, 'fixture', chat_only=True)
                else:
                    runner = CodexRunner(cfg, root, None, metrics, 'fixture', chat_only=True)
                cleanup.callback(runner.close_events)
                # Replace only external model capability lookup; retain shared effort policy.
                runner._validate_effort = AsyncMock()
                runner.turn_active, runner.turn_id = True, 'turn'
                async def request():
                    args = {'level': 'low', 'reason': 'fixture'}
                    if engine == 'claude':
                        return (await server.call_tool('set_effort', args)).content[0].text
                    result = await runner._request('item/tool/call', {
                        'tool': 'celeste_set_effort', 'callId': 'effort', 'arguments': args})
                    return result['contentItems'][0]['text']
                self.assertIn('登记', await request())
                self.assertEqual(metrics.state.effort_pending, 'low')
                runner.manual_effort = 'high'
                self.assertIn('固定', await request())


class GamePolicyTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(done=False, goals=SimpleNamespace(stop_motion=threading.Event()))
        self.controller = SimpleNamespace(
            cfg=RunConfig(agent='test', save='test', model='stub', mode='explore', live=True,
                          sim_assist=True, cutscene_mode='agent', allow_detour=True),
            session=self.session, prepare_only=False, chat_only=False, goal_finishing=False,
            stop_requested=False, model_waiting=False, paused=False, pausing=False)

    def test_game_actions_require_running_while_controls_remain_available(self):
        for name in GAME_ACTIONS:
            self.assertIsNone(denial(self.controller, name))
            for flag in ('prepare_only', 'chat_only', 'goal_finishing', 'stop_requested',
                         'model_waiting', 'paused', 'pausing'):
                with self.subTest(tool=name, flag=flag):
                    setattr(self.controller, flag, True)
                    self.assertIsNotNone(denial(self.controller, name))
                    for control in CONTROL_TOOLS:
                        self.assertIsNone(denial(self.controller, control))
                    setattr(self.controller, flag, False)

    def test_report_is_available_paused_but_needs_a_live_session(self):
        self.controller.paused = True
        self.assertIsNone(denial(self.controller, 'stop_run'))
        for flag in ('chat_only', 'prepare_only', 'goal_finishing', 'stop_requested'):
            setattr(self.controller, flag, True)
            self.assertIsNotNone(denial(self.controller, 'stop_run'))
            setattr(self.controller, flag, False)
        self.controller.session = None
        self.assertIsNotNone(denial(self.controller, 'stop_run'))
        self.assertIsNone(denial(self.controller, 'narrate'))

    def test_stop_goal_and_done_are_authoritative_before_hud_catches_up(self):
        self.session.goals.stop_motion.set()
        self.assertIsNotNone(denial(self.controller, 'act'))
        self.session.goals.stop_motion.clear()
        self.session.done = True
        self.assertIsNotNone(denial(self.controller, 'act'))

    def test_registered_optional_tools_follow_current_configuration(self):
        self.controller.cfg.sim_assist = False
        self.assertIn('未启用', denial(self.controller, 'simulate'))
        self.controller.cfg.allow_detour = False
        self.assertIn('未启用', denial(self.controller, 'travel'))

    def test_transport_names_share_policy_without_treating_workspace_as_game(self):
        for prefix in ('', 'celeste_', 'mcp__celeste__'):
            self.assertTrue(is_game_tool(prefix + 'act'))
            self.controller.paused = True
            self.assertIn('暂停', denial(self.controller, prefix + 'act'))
        for tool in ('Bash', 'read', 'apply_patch'):
            self.assertFalse(is_game_tool(tool))


if __name__ == '__main__':
    unittest.main()
