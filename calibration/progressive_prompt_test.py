"""Small first-read surface, discoverable references, and real executor naming."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.config import REPO_ROOT, RunConfig
from orchestrator.kb_access import documents
from orchestrator.kb_study import KbStudy, required_files
from orchestrator.prompt_environment import render_tool_references
from orchestrator.prompting import fill, play_prompt
from orchestrator.server import build_server
from orchestrator.startup_prompt import runtime_prompt


class ProgressivePromptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for target, value in [('orchestrator.prompting.OVERRIDES', self.root/'prompts'),
                              ('orchestrator.personal_prompts.STORE', self.root/'personal.json')]:
            context = patch(target, value)
            context.start()
            self.addCleanup(context.stop)
        self.cfg = RunConfig(agent='test', save='test', model='test', engine='codex', mode='explore',
                             knowledge_profile='explore', sim=False)

    def test_real_preparation_gate_requires_only_core_and_retains_optional_navigation(self):
        study = KbStudy(self.cfg.kb_source, 10)
        self.assertEqual(set(study.files), {'README.md', 'basics.md', 'data.md', 'tools.md'})
        self.assertFalse(study.ready())
        total = 0
        for name in study.files:
            text = (study.kb_dir/name).read_text(encoding='utf-8')
            total += len(text)
            study.note_lines(name, range(1, len(text.splitlines())+1))
        self.assertTrue(study.complete())
        self.assertLess(total, 4000)
        rows = {row['path']: row for row in documents('explore')}
        for name in ('observation.md', 'actions.md', 'story.md', 'workspace.md', 'programs.md'):
            self.assertIn('help/'+name, rows)
            self.assertFalse(rows['help/'+name]['required'])
            self.assertGreater(rows['help/'+name]['size'], 0)
        data = (study.kb_dir/'data.md').read_text(encoding='utf-8')
        for name in ('help/observation.md', 'help/actions.md', 'help/story.md', 'help/workspace.md'):
            self.assertIn(name, data)

    def test_environment_renders_names_once_and_preserves_paths_and_personal_text(self):
        source = 'Read Write Edit Bash observe act celeste_act /workspace/Read.py last_act.json'
        rendered = render_tool_references(source, self.cfg)
        self.assertEqual(rendered, 'read apply_patch apply_patch 原生 Shell celeste_observe celeste_act celeste_act /workspace/Read.py last_act.json')
        self.assertEqual(render_tool_references(rendered, self.cfg), rendered)
        self.assertEqual(render_tool_references(source, replace(self.cfg, engine='claude')), source)
        grouped = 'Read/Write/Bash observe/act/cutscene /workspace/Read/Write.txt'
        self.assertEqual(render_tool_references(grouped, self.cfg),
                         'read/apply_patch/原生 Shell celeste_observe/celeste_act/celeste_cutscene /workspace/Read/Write.txt')
        from orchestrator import personal_prompts
        personal_prompts.save('all', 'PERSONAL Read act Bash', personal_prompts.read('all')['version'])
        for engine in ('codex', 'claude'):
            cfg = replace(self.cfg, engine=engine)
            startup = runtime_prompt(cfg)
            self.assertTrue(startup.endswith('PERSONAL Read act Bash'))
            self.assertNotIn('原生 Codex 工具映射', startup)
            guide = play_prompt(cfg)
            if engine == 'codex':
                self.assertIn('celeste_observe', guide)
                self.assertIn('celeste_act', guide)
                self.assertIn('文件用 read/glob/grep', startup)
                self.assertIn('timeout_ms（毫秒）', fill('{program_timeout_rule}', cfg))
            else:
                self.assertIn('Read/Glob/Grep', startup)
                self.assertIn('Bash 的 timeout 参数', fill('{program_timeout_rule}', cfg))

    def test_tool_descriptions_keep_semantics_and_fall_back_when_reference_unavailable(self):
        def descriptions(cfg):
            server = build_server(SimpleNamespace(cfg=cfg, metrics=None, workspace=None))
            return {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
        for mode, profile, prefix in [('explore', 'explore', '/kb/help/'),
                                      ('speedrun', 'full', '/kb/explore/help/')]:
            cfg = replace(self.cfg, mode=mode, knowledge_profile=profile)
            current = descriptions(cfg)
            self.assertIn(prefix+'actions.md', current['act'])
            self.assertIn(prefix+'observation.md', current['observe'])
            self.assertIn('剩余输入不执行', current['act'])
            self.assertIn('结束本轮', current['stop_run'])
            self.assertIn('结束本轮', current['set_effort'])
            rel = ('help/' if mode == 'explore' else 'explore/help/')
            denied = descriptions(replace(cfg, kb_deny=[rel+'actions.md', rel+'observation.md']))
            self.assertNotIn(prefix+'actions.md', denied['act'])
            self.assertIn('expect', denied['act'])
            self.assertNotIn(prefix+'observation.md', denied['observe'])
        host = descriptions(replace(self.cfg, engine='claude', runtime='host'))
        self.assertNotIn('/workspace', host['act'])
        self.assertNotIn('/kb/', host['act'])
        self.assertIn('plan_file 当前无文件能力，不可用', host['act'])

    def test_forbidden_optional_program_reference_is_not_advertised(self):
        cfg = replace(self.cfg, kb_deny=['help/programs.md', 'help/workspace.md'])
        self.assertNotIn('/kb/help/programs.md', play_prompt(cfg))
        self.assertIn('当前未提供程序参考资料', play_prompt(cfg))

    def test_merged_feedback_matches_executor_and_formal_display_mode(self):
        for engine in ('codex', 'claude'):
            for formal in (False, True):
                cfg = replace(self.cfg, engine=engine, formal_thought=formal, live=True)
                rule = fill('{public_feedback_rule}', cfg)
                text = play_prompt(cfg)
                self.assertIn(rule, text)
                if formal:
                    self.assertIn('写下临时思考', rule)
                    self.assertNotIn('更新 HUD', rule)
                    self.assertIn('只显示每次', text)
                else:
                    self.assertIn('更新 HUD', rule)
                self.assertEqual('commentary' in rule, engine == 'codex')

    def test_old_reader_notice_does_not_double_prefix_explicit_local_names(self):
        from orchestrator.codex_agent import CodexRunner
        from orchestrator.metrics import Metrics
        path = self.root/'prompts/button_prepare_zh.md'
        path.parent.mkdir()
        path.write_text('使用 celeste_read 工具和 Read 工具读取必读清单。', encoding='utf-8')
        study = KbStudy(self.cfg.kb_source, 10)
        runner = CodexRunner(self.cfg, self.root/'run', None,
                             Metrics(self.root/'metrics.json', 'test', 'test', 'high', 60),
                             'unused', study, prepare_only=True)
        runner.registered_tools = {'celeste_read'}
        text = runner._first_prompt()
        self.assertIn('使用 celeste_read 工具和 celeste_read 工具', text)
        self.assertNotIn('celeste_celeste_read', text)

    def test_legacy_runtime_override_requires_non_destructive_environment_migration(self):
        from overlay import console_api
        path = self.root/'prompts/runtime_zh.md'
        path.parent.mkdir()
        old = '保留本机修改\n{current_settings}\n{game_tools}'
        path.write_text(old, encoding='utf-8')
        current = console_api.prompt_read(path.name)
        with self.assertRaisesRegex(ValueError, '缺少.*agent_environment'):
            runtime_prompt(self.cfg)
        from unittest.mock import AsyncMock
        from orchestrator.codex_agent import CodexRunner
        from orchestrator.metrics import Metrics
        runner = CodexRunner(self.cfg, self.root/'run', None,
                             Metrics(self.root/'metrics.json', 'test', 'test', 'high', 60), 'unused')
        runner.rpc.start = AsyncMock()
        try:
            with patch('orchestrator.config.REPO_ROOT', self.root), \
                 self.assertRaisesRegex(ValueError, '缺少.*agent_environment'):
                asyncio.run(runner.run())
            runner.rpc.start.assert_not_called()
        finally:
            runner.close_events()
        with patch.object(console_api, 'orchestrator_status', return_value={'running': False}):
            with self.assertRaisesRegex(ValueError, '缺少.*agent_environment'):
                console_api.prompt_write(path.name, old+'\n继续修改', current['version'])
            self.assertEqual(path.read_text(encoding='utf-8'), old)
            console_api.prompt_write(path.name, old+'\n{agent_environment}', current['version'])
        text = runtime_prompt(self.cfg)
        self.assertIn('保留本机修改', text)
        self.assertIn('文件用 read/glob/grep', text)


if __name__ == '__main__':
    unittest.main()
