"""Completion reports cannot bypass an updated chapter plan.

Use real session, navigation, goal persistence and public tools. DetourGame only
replaces the external game's menu, input and observation boundary.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from calibration.detour_test import DetourGame
from calibration.formal_run_test import _observation
from orchestrator.config import CHAPTER_SIDS, RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.segments import Piece, piece_label
from orchestrator.server import build_server
from calibration.isolation import generated_dirs_fixture

setUpModule, tearDownModule = generated_dirs_fixture()  # never write real workspaces/ or local/kb_views/


class ReportGame(DetourGame):
    def observation(self):
        obs = super().observation()
        if obs.get('player') is not None:
            obs['player'] = {**_observation()['player'], **obs['player']}
        return obs


class GoalReportPlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def session(self, *, chapter=2, policy='stop', scope='chapter', plan=None, side='A'):
        directory = self.root / str(len(list(self.root.iterdir())))
        directory.mkdir()
        cfg = RunConfig(agent='plan-report-test', save='test', save_slot=7, model='test',
                        engine='codex', runtime='host', mode='explore', knowledge_profile='explore',
                        chapter=chapter, chapters=[chapter], plan=plan or [Piece(chapter)],
                        completion_policy=policy, play_scope=scope, cutscene_mode='agent',
                        collision_image='off', sim=False, provider_info={'images': False})
        cfg.thought_lead_s, cfg.min_frames_per_act = 0, 1
        if scope == 'visited':
            cfg.visited_maps = [{'sid': CHAPTER_SIDS[chapter], 'side': side}]
        (directory/'run_state.json').write_text('{}', encoding='utf-8')
        metrics = Metrics(directory/'metrics.json', cfg.agent, cfg.model, 'medium', 600)
        session = GameSession(cfg, directory, metrics)
        self.addCleanup(session.close, release=False)
        game = ReportGame(start=(CHAPTER_SIDS[chapter], side), save=cfg.save)
        with patch('orchestrator.game_session.BridgeClient', return_value=game):
            session.connect()
        return session, game, build_server(session)

    async def act(self, server, inputs):
        result = await server.call_tool('act', {'inputs': inputs, 'thought': 'isolated plan regression'})
        self.assertFalse(result.is_error)

    async def complete_chapter(self, session, game, server):
        await self.act(server, ['40,R'])
        result = await server.call_tool('cutscene', {'action': 'skip'})
        self.assertFalse(result.is_error)
        await self.act(server, ['1,C', '40'])
        self.assertEqual(game.state, 'select')

    async def enter_third(self, session, game, server):
        game.panel_target = (CHAPTER_SIDS[3], 'A')
        await self.act(server, ['1,C', '5'])
        await self.act(server, ['1,C', '5'])
        self.assertEqual(session.piece, Piece(3))
        self.assertFalse(session.chapter_finished)

    def snapshot(self, session, game):
        path = session.run_dir/'goal.json'
        state = session.metrics.state
        return copy.deepcopy({
            'done': session.done, 'reason': session.done_reason, 'user_stopped': session.user_stopped,
            'goal': session.goals.snapshot(), 'goal_bytes': path.read_bytes() if path.exists() else None,
            'current': session.piece, 'queue': session.chapter_queue, 'switching': session.chapter_finished,
            'splits': state.splits, 'deaths': state.deaths, 'piece_done': state.piece_done,
            'stop_motion': session.goals.stop_motion.is_set(),
            'commands': game.commands, 'steps': game.steps, 'game_state': game.state})

    async def reject_old_report(self, session, game, server, *, expect_remaining=True):
        before = self.snapshot(session, game)
        with self.assertRaises(ToolError) as caught:
            await server.call_tool('finish_goal', {
                'goal': piece_label(Piece(2), {}), 'evidence': 'Chapter 2 completed; requesting finish.'})
        if expect_remaining:
            self.assertIn(piece_label(Piece(3), session._table), str(caught.exception))
        self.assertEqual(self.snapshot(session, game), before)
        self.assertFalse(session.done)
        self.assertNotIn('completion_report', session.goals.snapshot())

    async def test_updated_plan_rejects_old_completion_during_handoff_and_in_final_chapter(self):
        session, game, server = self.session()
        session.set_chapter_queue([3])
        await self.complete_chapter(session, game, server)
        self.assertTrue(session.chapter_finished)
        self.assertEqual(session.chapter_queue, [Piece(3)])
        self.assertEqual([row['chapter'] for row in session.metrics.state.splits], [CHAPTER_SIDS[2]])
        await self.reject_old_report(session, game, server)

        await self.enter_third(session, game, server)
        self.assertEqual(session.chapter_queue, [])  # Checking only the queue would miss this case.
        saved = json.loads((session.run_dir/'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['navigation_remaining'], [[3, 0, None]])
        await self.reject_old_report(session, game, server)
        await self.reject_old_report(session, game, server)  # Repetition still cannot close the run.

        await self.complete_chapter(session, game, server)
        self.assertTrue(session.done)
        self.assertEqual(session.done_reason, '章节完成')
        self.assertEqual([row['chapter'] for row in session.metrics.state.splits],
                         [CHAPTER_SIDS[2], CHAPTER_SIDS[3]])
        self.assertNotIn('completion_report', session.goals.snapshot())

    async def test_explicit_registered_text_goal_can_end_early_but_wrong_and_repeated_reports_cannot(self):
        session, game, server = self.session(chapter=3)
        session.set_goal({'text': 'inspect the current room then stop'})
        before = self.snapshot(session, game)
        with self.assertRaises(ToolError):
            await server.call_tool('finish_goal', {'goal': 'obsolete task', 'evidence': 'done'})
        self.assertEqual(self.snapshot(session, game), before)
        result = await server.call_tool('finish_goal', {
            'goal': 'inspect the current room then stop', 'evidence': 'Inspection finished.'})
        self.assertFalse(result.is_error)
        self.assertTrue(session.done)
        self.assertEqual(session.goals.snapshot()['completion_report']['goal'], 'inspect the current room then stop')
        after = self.snapshot(session, game)
        with self.assertRaises(ToolError):
            await server.call_tool('finish_goal', {
                'goal': 'inspect the current room then stop', 'evidence': 'duplicate'})
        self.assertEqual(self.snapshot(session, game), after)

    async def test_automatic_and_cleared_goals_do_not_accept_agent_completion(self):
        session, game, server = self.session(chapter=3)
        session.set_goal({'conditions': [{'field': 'frame', 'op': 'delta_gte', 'value': 1000}]})
        await self.reject_old_report(session, game, server, expect_remaining=False)
        session.goals.clear()
        await self.reject_old_report(session, game, server)

    async def test_partial_and_b_side_plans_also_require_controller_verification(self):
        for plan, side in [([Piece(3, 0, 1)], 'A'), ([Piece(3, side='B')], 'B')]:
            with self.subTest(side=side):
                session, game, server = self.session(chapter=3, plan=plan, side=side)
                await self.reject_old_report(session, game, server)

    async def test_free_exploration_report_compatibility(self):
        for scope, policy in [('visited', 'continue'), ('chapter', 'continue')]:
            with self.subTest(scope=scope):
                session, game, server = self.session(chapter=3, scope=scope, policy=policy)
                before = list(game.commands)
                result = await server.call_tool('finish_goal', {'goal': 'inspect a side area', 'evidence': 'Inspection finished.'})
                self.assertFalse(result.is_error)
                self.assertTrue(session.done)
                self.assertEqual(game.commands, before)


if __name__ == '__main__':
    unittest.main()
