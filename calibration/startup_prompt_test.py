"""Actual startup envelopes reference published guides without inlining stable rules."""
import asyncio
from dataclasses import replace
import itertools
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
        self.assertEqual(current_settings(updated)['act_frames'], [55, 150])
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
