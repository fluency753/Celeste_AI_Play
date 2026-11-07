"""Workspace permissions follow real tool dispatch, not model-client placement."""
import sys
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestrator.config import RunConfig
from orchestrator import kb_access
from orchestrator.execution import workspace_capabilities
from orchestrator.prompting import prompt_parts, play_prompt


class WorkspaceCapabilityTests(unittest.TestCase):
    def test_fresh_resume_only_requests_notes_when_workspace_is_readable(self):
        from orchestrator.run import opening_note
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for engine, released in itertools.product(('claude', 'codex'), (False, True)):
                with self.subTest(engine=engine, released=released):
                    (root/'summary.json').write_text(json.dumps({'game_released': released}), encoding='utf-8')
                    cfg = RunConfig(agent='test', save='test', model='model', engine=engine, runtime='host', sim=False)
                    note = opening_note(cfg, {'dir': root, 'state': {'engine': engine}, 'metrics': {}}, None)
                    self.assertEqual('/workspace/notes.md' in note, engine == 'codex')
                    self.assertEqual('game/ 文件都在' in note, engine == 'codex')
                    self.assertEqual('游戏一直冻结' in note, not released)
                    self.assertIn('observe', note)

    def test_host_claude_explore_launch_preview_and_prompt_need_only_game_tools(self):
        from orchestrator.run import parse_args
        from overlay.console_api import prompt_preview
        for live, cutscene in itertools.product((False, True), ('auto', 'agent')):
            argv = ['run', '--engine', 'claude', '--agent', 'test', '--save', 'test',
                    '--runtime', 'host', '--mode', 'explore', '--no-sim-assist', '--chapter', '0',
                    '--cutscene-mode', cutscene]
            if live:
                argv.append('--live')
            with self.subTest(live=live, cutscene=cutscene), patch.object(sys, 'argv', argv), \
                 patch('orchestrator.run.check_provider'):
                cfg, _ = parse_args()
            preview = prompt_preview({'engine': 'claude', 'agent': 'test', 'runtime': 'host',
                'save': {'name': 'test', 'slot': 10}, 'mode': 'explore', 'sim_assist': False, 'live': live,
                'cutscene_mode': cutscene})
            self.assertEqual(preview['text'], play_prompt(cfg))
            self.assertIsNone(preview['first_message'])
            for unavailable in ('/workspace', '/kb/', 'Bash', '编写模拟器', '自行写程序'):
                self.assertNotIn(unavailable, preview['text'])
            self.assertIn('缺少基线时默认 observe 重建', preview['text'])
            self.assertIn('短动作验证', preview['text'])
            self.assertIn('当前只有游戏工具', preview['text'])

    def test_host_codex_launch_and_preview_keep_container_program_tools(self):
        from orchestrator.run import parse_args
        from orchestrator.launch_options import config_for
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sim = root / 'sim/dist'
            sim.mkdir(parents=True)
            (sim / 'celeste-sim').touch()
            for mode in ('speedrun', 'explore'):
                with self.subTest(mode=mode), patch('orchestrator.config.REPO_ROOT', root), \
                     patch.object(sys, 'argv', ['run', '--engine', 'codex', '--agent', 'test',
                         '--save', 'test', '--runtime', 'host', '--mode', mode]):
                    launched, _ = parse_args()
                    preview = config_for({'engine': 'codex', 'agent': 'test', 'runtime': 'host',
                        'save': {'name': 'test', 'slot': 10, 'create': False}, 'mode': mode})
                self.assertFalse(launched.sim)       # 本机存在模拟器不自动开放主控预测。
                self.assertFalse(launched.sim_assist)   # 限额短路线预测默认关闭（需自配标准模拟器）
                self.assertEqual((preview.sim, preview.sim_assist), (launched.sim, launched.sim_assist))
                self.assertTrue(workspace_capabilities(launched).needs_container)
                self.assertNotIn('你没有其他工具，也不能执行代码', play_prompt(launched))
                self.assertNotIn('sim_zh.md', [p.source for p in prompt_parts(launched)])

    def test_claude_speedrun_simulator_is_direct_by_default_and_controller_when_limited(self):
        """Claude 的速通预置模拟器不限额时直接调用 /sim：容器运行方式下本机有模拟器时默认开；本机运行方式（没有程序
        通道）不能直接调用；加规划硬限额改用主控限额预测（与 Codex 相同）。"""
        from orchestrator.launch_options import config_for
        from orchestrator.modes import game_tools
        from orchestrator.run import parse_args
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'sim' / 'dist' / 'celeste-sim'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch('orchestrator.config.REPO_ROOT', root), \
                 patch.object(sys, 'argv', ['run', '--engine', 'claude', '--agent', 'test', '--save', 'test',
                                            '--mode', 'speedrun']), \
                 patch('orchestrator.run.check_provider'):
                launched, _ = parse_args()
            self.assertTrue(launched.sim)
            self.assertFalse(launched.pause_limit)
            self.assertNotIn('simulate', game_tools(launched))
            sources = [p.source for p in prompt_parts(launched)]
            self.assertIn('sim_direct_zh.md', sources)
            self.assertIn('free_zh.md', sources)
            self.assertNotIn('sim_zh.md', sources)
            with patch('orchestrator.config.REPO_ROOT', root), \
                 patch.object(sys, 'argv', ['run', '--engine', 'claude', '--agent', 'test', '--save', 'test',
                                            '--runtime', 'host', '--mode', 'speedrun', '--sim']), \
                 patch('orchestrator.run.check_provider'):
                with self.assertRaisesRegex(SystemExit, '容器运行方式'):
                    parse_args()
            # 加规划硬限额就改用主控预测：不需要工作区程序，本机运行方式也可以
            with patch('orchestrator.config.REPO_ROOT', root), \
                 patch.object(sys, 'argv', ['run', '--engine', 'claude', '--agent', 'test', '--save', 'test',
                                            '--runtime', 'host', '--mode', 'speedrun', '--sim', '--limit-planning']), \
                 patch('orchestrator.run.check_provider'):
                limited, _ = parse_args()
            self.assertTrue(limited.sim and limited.pause_limit)
            self.assertIn('simulate', game_tools(limited))
            self.assertIn('sim_zh.md', [p.source for p in prompt_parts(limited)])

    def test_host_claude_can_request_private_simulation_without_workspace_tools(self):
        from orchestrator.launch_options import config_for
        from orchestrator.modes import game_tools
        from orchestrator.run import parse_args
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'sim' / 'dist' / 'celeste-sim'
            binary.parent.mkdir(parents=True)
            binary.touch()
            for mode in ('explore',):          # 速通的主控限额预测只用于 Codex；Claude 速通用 /sim（见上）
                flags = ['--sim', '--limit-planning'] if mode == 'speedrun' else ['--sim-assist']
                settings = {'sim': True, 'limit_planning': True} if mode == 'speedrun' else {'sim_assist': True}
                with self.subTest(mode=mode), patch('orchestrator.config.REPO_ROOT', root), \
                     patch.object(sys, 'argv', ['run', '--engine', 'claude', '--agent', 'test',
                         '--save', 'test', '--runtime', 'host', '--mode', mode, *flags]), \
                     patch('orchestrator.run.check_provider'):
                    launched, _ = parse_args()
                    preview = config_for({'engine': 'claude', 'agent': 'test', 'runtime': 'host',
                        'save': {'name': 'test', 'slot': 10}, 'mode': mode, **settings})
                self.assertFalse(workspace_capabilities(launched).programs)
                self.assertFalse(workspace_capabilities(preview).programs)
                self.assertEqual((preview.sim, preview.sim_assist), (launched.sim, launched.sim_assist))
                self.assertIn('simulate', game_tools(launched))
                self.assertIn('simulate', game_tools(preview))
                if mode == 'speedrun':
                    self.assertIn('sim_zh.md', [p.source for p in prompt_parts(launched)])

    def test_claude_host_tool_registration_and_prompt_agree(self):
        from orchestrator.agent import ClaudeRunner
        from orchestrator.metrics import Metrics
        cfg = RunConfig(agent='test', save='test', model='opus', runtime='host', sim=False)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runner = ClaudeRunner(cfg, root, SimpleNamespace(), Metrics(root/'metrics.json', 'test', 'opus', 'high', 60), '')
            try:
                with patch('orchestrator.agent.find_claude_exe', return_value='claude'):
                    command = runner._command()
                self.assertEqual(command[command.index('--tools') + 1], '')
                self.assertFalse(runner._allowed_tool('Bash'))
                self.assertFalse(runner._allowed_tool('Read'))
                self.assertIn('你没有其他工具，也不能执行代码', play_prompt(cfg))
                runner.chat_only = True
                self.assertIn('没有工作区工具通道', runner._lifecycle_prompt())
                self.assertNotIn('读取笔记', runner._lifecycle_prompt())
                paused = runner._paused_notice()
                self.assertIn('没有工作区工具通道', paused)
                self.assertNotIn('可以读写', paused)
                self.assertNotIn('可以编辑', paused)
                self.assertNotIn('程序可按要求运行', paused)
                with self.assertRaisesRegex(ValueError, 'container.*host'):
                    workspace_capabilities(cfg).validate(programs=True)
            finally:
                runner.close_events()

    def test_program_feedback_is_included_for_host_codex(self):
        cfg = RunConfig(agent='test', save='test', model='model', engine='codex', runtime='host', sim=False, live=True)
        self.assertIn('program_batches_zh.md', [p.source for p in prompt_parts(cfg)])
        cfg.engine = 'claude'
        self.assertNotIn('program_batches_zh.md', [p.source for p in prompt_parts(cfg)])


class WorkspacePreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_codex_run_builds_real_reading_gate_before_session(self):
        from orchestrator.run import run_async
        from orchestrator import kb_access
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kb = root/'kb/explore'
            kb.mkdir(parents=True)
            (kb/'README.md').write_text('actual reading gate\n', encoding='utf-8')
            cfg = RunConfig(agent='test', save='test', model='model', engine='codex', runtime='host',
                            sim=False, study=True, knowledge_profile='explore')
            args = SimpleNamespace(no_agent=False, prepared=None, no_hud=True, no_hud_window=True)
            with patch('orchestrator.config.REPO_ROOT', root), patch.object(kb_access, 'KB_ROOT', root/'kb'), \
                 patch('orchestrator.run._run_session', new_callable=AsyncMock, return_value={}) as session:
                await run_async(cfg, args, root, None)
            study = session.call_args.args[5]
            self.assertIsNotNone(study)
            self.assertEqual(study.files, ['README.md'])
            self.assertFalse(study.complete())
            self.assertEqual(study.container_paths(), ['/kb/README.md'])

    async def test_host_claude_preparation_is_rejected_before_creating_mcp(self):
        from orchestrator.lifecycle import host_runner
        cfg = RunConfig(agent='test', save='test', model='opus', runtime='host', sim=False)
        with patch('orchestrator.runtimes.Runtime.runner') as runner:
            with self.assertRaisesRegex(ValueError, '读取工具'):
                async with host_runner(cfg, None, None, None, prepare_only=True):
                    self.fail('host Claude cannot read the required knowledge files')
            runner.assert_not_called()
