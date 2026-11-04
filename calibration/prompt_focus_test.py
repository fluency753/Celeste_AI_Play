"""Keep task boundaries visible while leaving interface details in their owner."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.prompting import prompt_parts
from orchestrator.segments import Piece
from orchestrator.startup_prompt import current_settings, runtime_prompt
from orchestrator.server import build_server


class PromptFocusTests(unittest.TestCase):
    def setUp(self):
        from orchestrator import profiles
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(profiles.using(profiles.LEGACY))
        self.enterContext(patch.object(profiles, 'selected', return_value=profiles.LEGACY))
        self.enterContext(patch('orchestrator.prompting.OVERRIDES', self.root/'prompts'))
        self.enterContext(patch('orchestrator.personal_prompts.STORE', self.root/'personal.json'))
        self.cfg = RunConfig(agent='focus-test', save='example', save_slot=14, model='test',
                             engine='codex', mode='explore', knowledge_profile='explore',
                             cutscene_mode='agent', chapter=1, sim=False)

    def test_brief_preserves_sides_checkpoints_and_stop_boundaries(self):
        table = {1: [{'index': 0, 'level': 'start', 'name': '开头'},
                     {'index': 1, 'level': 'middle-room', 'name': '中点'},
                     {'index': 2, 'level': 'end-room', 'name': '终点'}]}
        cfg = replace(self.cfg, plan=[Piece(1, 1, 2), Piece(4, side='B'), Piece(9, side='C')],
                      checkpoints=table, completion_policy='continue')
        text = runtime_prompt(cfg)
        for fact in ('example', '14', 'middle-room', 'end-room'):
            self.assertIn(fact, text)
        self.assertNotIn('"act_frames"', text)
        self.assertNotIn('"presentation"', text)
        self.assertEqual(current_settings(cfg)['task']['targets'][1]['side'], 'B')

    def test_runtime_settings_report_preset_simulator_only_when_enabled_and_bounded(self):
        for sim in (False, True):
            for limited in (False, True):
                cfg = replace(self.cfg, mode='speedrun', knowledge_profile='full',
                              sim=sim, pause_limit=limited)
                self.assertEqual(current_settings(cfg)['prediction']['preset_simulator'], sim and limited)

    def test_host_claude_can_request_main_controller_simulation_without_program_tutorial(self):
        cfg = replace(self.cfg, engine='claude', runtime='host', mode='speedrun',
                      knowledge_profile='full', sim=True, pause_limit=True)
        parts = prompt_parts(cfg)
        sources = [part.source for part in parts]
        self.assertIn('sim_zh.md', sources)
        self.assertIn('host_zh.md', sources)
        self.assertNotIn('container_sim_zh.md', sources)

    def test_actual_tool_and_observation_use_configured_frames_without_guide_copy(self):
        from calibration.workspace_observation_test import game_session
        with game_session() as (session, _):
            session.cfg.mode, session.cfg.knowledge_profile = 'explore', 'explore'
            session.cfg.min_frames_per_act, session.cfg.explore_act_frames = 17, 83
            server = build_server(session)
            tools = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
            self.assertIn('至少 17', tools['act'])
            self.assertIn('最多 83', tools['act'])
            result = asyncio.run(server.call_tool('observe', {}))
            text = '\n'.join(item.text for item in result.content if hasattr(item, 'text'))
            self.assertFalse(result.is_error)
            self.assertIn('17～83', text)

if __name__ == '__main__':
    unittest.main()
