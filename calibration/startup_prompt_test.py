"""Actual startup envelopes reference published guides without inlining stable rules."""
import asyncio
from dataclasses import replace
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.config import RunConfig
from orchestrator.codex_agent import CodexRunner, codex_prompt
from orchestrator.metrics import Metrics
from orchestrator.prompt_guide import guide_document
from orchestrator.prompting import play_prompt
from orchestrator.startup_prompt import current_settings, runtime_prompt


class StartupPromptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name, value in [('orchestrator.personal_prompts.STORE', self.root/'personal.json'),
                            ('orchestrator.prompting.OVERRIDES', self.root/'prompts')]:
            context = patch(name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_runtime_template_editor_override_preview_and_reset(self):
        from orchestrator import personal_prompts
        from orchestrator.launch_options import config_for
        from orchestrator.prompting import current_rules_note
        from overlay import console_api
        name = 'runtime_zh.md'
        doc = console_api.prompt_read(name)
        self.assertEqual({p['name'] for p in doc['placeholders']},
                         {'{current_settings}', '{guide_index_path}', '{game_tools}', '{agent_environment}'})
        self.assertFalse(doc['live_editable'])
        options = {'engine': 'codex', 'mode': 'explore', 'agent': 'test',
                   'save': {'name': '{game_tools}', 'slot': 14},
                   'sim': False, 'sim_assist': False}
        guide = guide_document(config_for(options))
        personal_prompts.save('all', 'PERSONAL {current_settings}', personal_prompts.read('all')['version'])
        template = '自定义启动\n{current_settings}\n{guide_index_path}\n{game_tools}\n{agent_environment}\n'
        with patch.object(console_api, 'orchestrator_status', return_value={'running': False}):
            saved = console_api.prompt_write(name, template, doc['version'])
            self.assertTrue(saved['overridden'])
            self.assertFalse(saved['missing_placeholders'])
            for engine, mode, runtime in itertools.product(('codex', 'claude'), ('explore', 'speedrun'),
                                                         ('host', 'container')):
                with self.subTest(engine=engine, mode=mode, runtime=runtime):
                    selected = {**options, 'engine': engine, 'mode': mode, 'runtime': runtime}
                    cfg = config_for(selected)
                    preview = console_api.prompt_preview(selected)
                    actual = codex_prompt(cfg) if engine == 'codex' else runtime_prompt(cfg)
                    self.assertEqual(preview['text'], actual)
                    self.assertEqual(actual.count('PERSONAL {current_settings}'), 1)
                    if engine == 'claude':
                        self.assertIn(runtime_prompt(cfg), current_rules_note(cfg))
                    self.assertFalse(any(p['source'] == name for p in preview['guide_parts']))
                    if engine == 'claude' and runtime == 'host':
                        self.assertNotIn('自定义启动', actual)
                        self.assertTrue(actual.startswith(play_prompt(cfg)))
                        continue
                    part = preview['parts'][0]
                    self.assertEqual(part['source'], name)
                    self.assertTrue(part['overridden'])
                    lines = part['text'].splitlines()
                    self.assertEqual(lines[0], '自定义启动')
                    self.assertEqual(json.loads(lines[1]), current_settings(cfg))
                    self.assertEqual(lines[2], guide_document(cfg)['index_path'])
                    self.assertEqual(preview['parts'][1]['label'], '本机个人追加要求')
                    self.assertTrue(actual.startswith(part['text'] + '\n\n# 本机个人追加要求'))
            self.assertEqual(guide_document(config_for(options))['version'], guide['version'])
            reset = console_api.prompt_reset(name)
            self.assertFalse(reset['overridden'])
            preview = console_api.prompt_preview(options)
            self.assertEqual(preview['parts'][0]['source'], name)
            self.assertFalse(preview['parts'][0]['overridden'])
            self.assertTrue(preview['text'].startswith('你是通过工具游玩 Celeste 的玩家。'))
        with patch.object(console_api, 'orchestrator_status', return_value={'running': True}):
            with self.assertRaisesRegex(ValueError, '主控在运行'):
                console_api.prompt_write(name, template, reset['version'])
            with self.assertRaisesRegex(ValueError, '主控在运行'):
                console_api.prompt_reset(name)

    def test_workspace_capabilities_choose_delivery_and_preview_has_actual_prompt(self):
        from orchestrator.launch_options import config_for
        from overlay.console_api import prompt_preview
        for engine, runtime, mode in itertools.product(('codex', 'claude'), ('host', 'container'), ('explore', 'speedrun')):
            with self.subTest(engine=engine, runtime=runtime, mode=mode):
                options = {'agent': 'test', 'save': {'name': 'test', 'slot': 14},
                           'engine': engine, 'runtime': runtime, 'mode': mode, 'sim': False, 'sim_assist': False}
                cfg = config_for(options)
                preview = prompt_preview(options)
                actual = codex_prompt(cfg) if engine == 'codex' else runtime_prompt(cfg)
                self.assertEqual(preview['text'], actual)
                if engine == 'claude' and runtime == 'host':
                    self.assertEqual(actual, play_prompt(cfg))
                    self.assertFalse(preview['guide_parts'])
                else:
                    self.assertIn(guide_document(cfg)['index_path'], actual)
                    self.assertNotIn('# 从房间数据建立路线', actual)
                    self.assertLess(len(actual), 2500)
                    self.assertEqual(''.join(p['sep'] + p['text'] for p in preview['guide_parts']), play_prompt(cfg))

    def test_configuration_changes_update_envelope_without_requiring_all_sections(self):
        cfg = RunConfig(agent='test', save='test', model='test', engine='codex', mode='explore')
        old = guide_document(cfg)
        updated = replace(cfg, min_frames_per_act=55, action_permissions={
            'restart_chapter': False, 'return_to_map': True, 'save_quit': False})
        new = guide_document(updated)
        self.assertEqual(current_settings(updated)['act_frames'], [55, 600])
        self.assertTrue(current_settings(updated)['permissions']['return_to_map'])
        old_sections = {s['id']: s['sha256'] for s in old['sections']}
        unchanged = [s for s in new['sections'] if old_sections.get(s['id']) == s['sha256']]
        self.assertTrue(unchanged)
        self.assertLess(len(unchanged), len(new['sections']))
        self.assertIn('实时权限与目标以主控反馈为准', runtime_prompt(updated))
        self.assertIn('工作区的已读记录不能代替新会话自己阅读', runtime_prompt(updated))

    def test_failed_guide_publication_stops_runner_before_model_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent='test', save='test', model='test', engine='codex')
            runner = CodexRunner(cfg, root, None, Metrics(root/'metrics.json', 'test', 'test', 'high', 60), 'unused')
            runner.rpc.start = AsyncMock()
            with patch('orchestrator.prompt_guide.publish_guide', side_effect=OSError('guide unavailable')):
                with self.assertRaisesRegex(OSError, 'guide unavailable'):
                    asyncio.run(runner.run())
            runner.rpc.start.assert_not_called()
            self.assertFalse((root/'system_prompt.md').exists())

    def test_published_snapshot_is_used_even_if_templates_change_before_model_start(self):
        from orchestrator import prompting
        from orchestrator.prompt_guide import publish_guide
        from orchestrator.agent import ClaudeRunner
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent='test', save='test', model='test', engine='codex')
            with patch('orchestrator.config.REPO_ROOT', root):
                guide = publish_guide(cfg)
            with patch('orchestrator.prompt_guide.guide_document', side_effect=AssertionError('must not rerender')):
                self.assertIn(guide['index_path'], codex_prompt(cfg, guide=guide))
                self.assertIn(guide['index_path'], runtime_prompt(cfg, guide=guide))
            cfg.engine = 'claude'
            runner = ClaudeRunner(cfg, root, None, Metrics(root/'metrics.json', 'test', 'test', 'high', 60), 'unused')
            try:
                with patch('orchestrator.prompt_guide.publish_guide', return_value=guide), \
                     patch('orchestrator.prompt_guide.guide_document', side_effect=AssertionError('must not rerender')):
                    command = runner._command()
                    self.assertIn(guide['index_path'], command[command.index('--append-system-prompt') + 1])
            finally:
                runner.close_events()


if __name__ == '__main__':
    unittest.main()
