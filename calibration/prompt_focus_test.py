"""Keep task boundaries visible while leaving interface details in their owner."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.config import RunConfig, REPO_ROOT
from orchestrator.prompting import play_prompt, prompt_parts
from orchestrator.segments import Piece
from orchestrator.startup_prompt import current_settings, runtime_prompt, task_brief
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
        for fact in ('example', '槽位 14', 'middle-room', 'end-room', 'A 面', 'B 面', 'C 面',
                     '计划完成后继续在许可范围内活动'):
            self.assertIn(fact, text)
        self.assertNotIn('"act_frames"', text)
        self.assertNotIn('"presentation"', text)
        self.assertEqual(current_settings(cfg)['task']['targets'][1]['side'], 'B')
        guide = play_prompt(cfg)
        self.assertNotIn('最后一截完成后本次运行结束', guide)
        self.assertIn('计划完成后仍在许可范围内继续活动', guide)
        visited = task_brief(replace(cfg, play_scope='visited'))
        self.assertIn('以主控保存的许可集合及最新观测为准', visited)
        self.assertIn('区分 A/B/C 面', visited)
        self.assertNotIn('计划目标（依次）', visited)
        detour = task_brief(replace(self.cfg, allow_detour=True))
        self.assertIn('仅作获准的绕行', detour)

    def test_boot_omits_interface_reminder_and_tas_tutorial_duplicates(self):
        text = play_prompt(self.cfg)
        for removed in ('让观众看见尝试和进步', '首选用 inputs', 'stop_on', 'freeze frames',
                        '游戏工具为', '思考累计满', '笔记维护提醒', '已知上下文窗口占用',
                        'input_format', 'celeste_tas', '比如找到好路线', '按房间或机制整理'):
            self.assertNotIn(removed, text)
        labels = [p.label for p in prompt_parts(self.cfg)]
        self.assertNotIn('剧情模式说明（有剧情工具时自动加）', labels)
        self.assertEqual(text.count('当前为 agent 剧情模式'), 1)
        self.assertIn('主控才确认整章完成', text)
        self.assertIn('组织方式由你决定', text)
        auto = play_prompt(replace(self.cfg, cutscene_mode='auto'))
        self.assertNotIn('当前为 agent 剧情模式', auto)
        self.assertIn('当前 auto 模式', auto)
        reference = (REPO_ROOT/'kb/explore/help/actions.md').read_text(encoding='utf-8')
        for fact in ('input_cursor.next', 'remaining_inputs', '原格式', '松键', 'LRUDJKXCZVG'):
            self.assertIn(fact, reference)

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
            self.assertNotIn('每次 celeste_act 至少', play_prompt(session.cfg))

    def test_tas_precondition_stays_available_when_optional_reference_is_unreadable(self):
        from types import SimpleNamespace
        for cfg in (replace(self.cfg, engine='claude', runtime='host'),
                    replace(self.cfg, kb_deny=['help/actions.md'])):
            server = build_server(SimpleNamespace(cfg=cfg, metrics=None, workspace=None))
            tools = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
            self.assertIn('原格式显式执行一帧空输入松键', tools['act'])
            self.assertNotIn('input_cursor.next', play_prompt(cfg))


if __name__ == '__main__':
    unittest.main()
