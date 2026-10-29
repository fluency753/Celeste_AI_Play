"""Personal intent uses one startup snapshot and never changes shared guide content."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator import personal_prompts as personal
from orchestrator.config import RunConfig
from orchestrator.codex_agent import CodexRunner, codex_prompt
from orchestrator.metrics import Metrics
from orchestrator.prompt_guide import guide_document, startup_context
from orchestrator.prompting import current_rules_note, play_prompt
from orchestrator.startup_prompt import runtime_prompt


class PersonalPromptDeliveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name, value in [('orchestrator.personal_prompts.STORE', self.root/'local/personal.json'),
                            ('orchestrator.config.REPO_ROOT', self.root)]:
            context = patch(name, value)
            context.start()
            self.addCleanup(context.stop)
        self.cfg = RunConfig(agent='test', save='test', model='test', engine='codex', mode='explore')

    def save(self, scope, text):
        personal.save(scope, text, personal.read(scope)['version'])

    def test_scope_order_and_shared_guide_independence(self):
        old = guide_document(self.cfg)
        self.save('all', 'ALL-MARKER')
        self.save('codex:explore', 'EXPLORE-MARKER')
        self.save('codex:speedrun', 'SPEED-MARKER')
        context = startup_context(self.cfg)
        self.assertEqual(old['documents'], context['documents'])
        self.assertEqual(old['version'], context['version'])
        actual = codex_prompt(self.cfg, guide=context)
        self.assertEqual(actual.count('ALL-MARKER'), 1)
        self.assertEqual(actual.count('EXPLORE-MARKER'), 1)
        self.assertLess(actual.index('ALL-MARKER'), actual.index('EXPLORE-MARKER'))
        self.assertNotIn('SPEED-MARKER', actual)
        self.assertNotIn('ALL-MARKER', ''.join(context['documents'].values()))

    def test_startup_and_resume_use_captured_text_after_disk_changes(self):
        for runtime in ('host', 'container'):
            with self.subTest(runtime=runtime):
                cfg = replace(self.cfg, engine='claude', runtime=runtime)
                self.save('all', 'CAPTURED-MARKER')
                context = startup_context(cfg)
                self.save('all', 'LATER-MARKER')
                with patch.object(personal, 'snapshot', side_effect=AssertionError('must use captured snapshot')):
                    actual = runtime_prompt(cfg, guide=context)
                    resume = current_rules_note(cfg, guide=context)
                for text in (actual, resume):
                    self.assertIn('CAPTURED-MARKER', text)
                    self.assertNotIn('LATER-MARKER', text)
                self.assertEqual(actual.count('CAPTURED-MARKER'), 1)
                self.assertIn(actual, resume)

    def test_empty_defaults_and_preview_match_actual_all_executors(self):
        from orchestrator.launch_options import config_for
        from overlay.console_api import prompt_preview
        for engine in ('codex', 'claude'):
            for runtime in ('container', 'host'):
                options = {'agent':'test', 'save':{'name':'test','slot':14}, 'engine':engine,
                           'runtime':runtime, 'mode':'explore', 'sim':False, 'sim_assist':False}
                cfg = config_for(options)
                empty = prompt_preview(options)
                self.assertFalse(empty['personal_applied'])
                self.assertNotIn('本机个人追加要求', empty['text'])
                self.assertFalse(personal.STORE.exists())
                self.save('all', 'PERSONAL-MARKER')
                preview = prompt_preview(options)
                actual = codex_prompt(cfg) if engine == 'codex' else runtime_prompt(cfg)
                self.assertEqual(preview['text'], actual)
                self.assertEqual(actual.count('PERSONAL-MARKER'), 1)
                self.assertEqual([p['scope'] for p in preview['personal_applied']], ['all'])
                if engine == 'claude' and runtime == 'host':
                    self.assertTrue(actual.startswith(play_prompt(cfg)))
                self.save('all', '')
                personal.STORE.unlink()

    def test_bad_personal_file_stops_before_model_start(self):
        personal.STORE.parent.mkdir(parents=True)
        personal.STORE.write_text('broken', encoding='utf-8')
        root = self.root/'run'
        root.mkdir()
        runner = CodexRunner(self.cfg, root, None,
                             Metrics(root/'metrics.json', 'test', 'test', 'high', 60), 'unused')
        runner.rpc.start = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'JSON'):
            asyncio.run(runner.run())
        runner.rpc.start.assert_not_called()
        self.assertFalse((root/'system_prompt.md').exists())


if __name__ == '__main__':
    unittest.main()
