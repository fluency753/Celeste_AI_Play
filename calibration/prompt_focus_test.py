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
        for engine in ('codex', 'claude'):
            for sim in (False, True):
                for limited in (False, True):
                    cfg = replace(self.cfg, engine=engine, mode='speedrun', knowledge_profile='full',
                                  sim=sim, pause_limit=limited)
                    # Codex：主控预测须限额；Claude：/sim 直接调用，记账与否都算开放
                    expected = sim and (limited or engine == 'claude')
                    self.assertEqual(current_settings(cfg)['prediction']['preset_simulator'], expected)

    def test_limited_simulation_prompt_is_the_same_controller_version_for_both_executors(self):
        for engine, runtime in (('codex', 'container'), ('claude', 'container'), ('claude', 'host')):
            with self.subTest(engine=engine, runtime=runtime):
                cfg = replace(self.cfg, engine=engine, runtime=runtime, mode='speedrun',
                              knowledge_profile='full', sim=True, pause_limit=True)
                sources = [part.source for part in prompt_parts(cfg)]
                self.assertIn('sim_zh.md', sources)
                self.assertIn('pause_zh.md', sources)
                self.assertNotIn('sim_direct_zh.md', sources)
                self.assertIn('host_zh.md' if runtime == 'host' else 'container_sim_zh.md', sources)
        # Claude 本机运行方式不限额：没有程序通道，不能直接调用 /sim
        cfg = replace(self.cfg, engine='claude', runtime='host', mode='speedrun', knowledge_profile='full', sim=True)
        sources = [part.source for part in prompt_parts(cfg)]
        self.assertNotIn('sim_zh.md', sources)
        self.assertNotIn('sim_direct_zh.md', sources)
        self.assertIn('system_nosim_zh.md', sources)

    def test_claude_container_prompt_keeps_direct_simulator_when_unlimited(self):
        cfg = replace(self.cfg, engine='claude', runtime='container', mode='speedrun',
                      knowledge_profile='full', sim=True, pause_limit=False, playback=True)
        parts = prompt_parts(cfg)
        sources = [part.source for part in parts]
        text = ''.join(part.sep + part.text for part in parts)
        for name in ('system_sim_direct_zh.md', 'container_sim_direct_zh.md', 'sim_direct_zh.md',
                     'playback_direct_zh.md', 'free_zh.md', 'retry_zh.md', 'interface_claude_zh.md'):
            self.assertIn(name, sources)
        self.assertIn('/sim/README.md', text)
        self.assertIn('sim_state.json', text)
        self.assertIn('房间具体信息以精确碰撞箱数据和实体数据等为准，截图的视觉识别仅作为参考', text)
        self.assertNotIn('simulate', text)
        # 限额：模拟改走主控，完整接口说明（执行器相关）保留
        limited = [part.source for part in prompt_parts(replace(cfg, pause_limit=True))]
        self.assertIn('sim_zh.md', limited)
        self.assertIn('interface_claude_zh.md', limited)
        self.assertNotIn('sim_direct_zh.md', limited)

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
