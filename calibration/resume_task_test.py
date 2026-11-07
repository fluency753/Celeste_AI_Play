"""Restore instructions omit task targets without changing execution config."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.prompt_guide import GuideReading, guide_document
from orchestrator.session_controls import SessionControls
from orchestrator.startup_prompt import current_settings, runtime_prompt
from orchestrator.segments import Piece


class RestoreTaskFixture:
    def setUp(self):
        from orchestrator import profiles
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(profiles.using(profiles.LEGACY))
        self.enterContext(patch.object(profiles, 'selected', return_value=profiles.LEGACY))
        self.enterContext(patch('orchestrator.personal_prompts.STORE', self.root/'personal.json'))
        self.enterContext(patch('orchestrator.prompting.OVERRIDES', self.root/'prompts'))
        self.enterContext(patch.object(RunConfig, 'workspace_dir', property(lambda cfg: self.root/'workspace')))
        self.cfg = RunConfig(agent='restore-test', save='test', model='test', engine='codex',
                             mode='explore', chapter=3, chapters=[3], plan=[Piece(3)], sim=False)


class RestoreTaskTests(RestoreTaskFixture, unittest.TestCase):
    def test_restore_rejects_a_guide_that_would_reintroduce_the_task(self):
        initial = guide_document(self.cfg)
        with self.assertRaises(ValueError):
            runtime_prompt(self.cfg, guide=initial, include_task=False)

    def test_restored_targets_are_omitted_from_envelope_and_guide_only(self):
        for engine, mode, plan in [('codex', 'explore', [Piece(3)]),
                                   ('claude', 'explore', [Piece(3, 1, 2)]),
                                   ('claude', 'speedrun', [Piece(3, side='B'), Piece(4)])]:
            with self.subTest(engine=engine, mode=mode):
                cfg = replace(self.cfg, engine=engine, mode=mode, plan=plan)
                initial = guide_document(cfg)
                restored = guide_document(cfg, include_task=False)
                task_ids = {section['id'] for section in initial['sections']} - {section['id'] for section in restored['sections']}
                self.assertTrue(task_ids)
                self.assertTrue(task_ids.issubset(set(restored['omitted_task_sections'])))
                self.assertIn('targets', current_settings(cfg)['task'])
                settings = current_settings(cfg, include_task=False)
                self.assertNotIn('targets', settings['task'])
                original = current_settings(cfg)
                original['task'].pop('targets')
                self.assertEqual(settings, original)
                self.assertEqual(cfg.plan, plan)
                content = runtime_prompt(cfg, guide=restored)
                for section in initial['sections']:
                    if section['id'] in task_ids:
                        self.assertNotIn(section['text'].strip(), restored['documents']['guide.md'])
                reader = GuideReading(cfg, restored)
                reader.delivered = {section['id']: section['sha256'] for section in initial['sections']}
                self.assertEqual(reader.removed_sections(), set())
                self.assertNotIn('天空度假村', content)
                self.assertNotIn('天空度假村', restored['documents']['guide.md'])

    def test_restoring_after_whole_segment_change_does_not_revoke_prior_task(self):
        for old_plan, new_plan in [([Piece(3)], [Piece(3, 1, 2)]), ([Piece(3, 1, 2)], [Piece(3)])]:
            old = guide_document(replace(self.cfg, plan=old_plan))
            restored = guide_document(replace(self.cfg, plan=new_plan), include_task=False)
            reader = GuideReading(self.cfg, restored)
            reader.delivered = {section['id']: section['sha256'] for section in old['sections']}
            self.assertEqual(reader.removed_sections(), set())

    def test_registered_goal_is_not_repeated_on_restore_but_new_goal_notification_survives(self):
        controller = SessionControls()
        controller.resume_notice = ''
        controller.session = SimpleNamespace(goals=SimpleNamespace(snapshot=lambda: {
            'active': True, 'text': 'PRIVATE_TASK_MARKER', 'conditions': []}))
        controller.announce_goal = False
        self.assertNotIn('PRIVATE_TASK_MARKER', controller._turn_message('resume', [], opening=True))
        update = {'text': 'new user goal: PRIVATE_TASK_MARKER'}
        self.assertIn(update['text'], controller._turn_message('resume', [update], opening=True))
        controller.announce_goal = True
        self.assertIn('PRIVATE_TASK_MARKER', controller._turn_message('initial', [], opening=True))

    def test_claude_restore_command_and_inline_rules_have_no_task_reinjection(self):
        cfg = replace(self.cfg, engine='claude', runtime='host')
        directory = self.root/'claude'
        directory.mkdir()
        runner = ClaudeRunner(cfg, directory, SimpleNamespace(done=False),
                              Metrics(directory/'metrics.json', 'test', 'test', 'medium', 60),
                              'unused', session_id='old-session', resume=True, include_task=False,
                              announce_goal=False)
        self.addCleanup(runner.close_events)
        with patch('orchestrator.agent.find_claude_exe', return_value='fixture-claude'):
            command = runner._command()
        self.assertIn('--resume', command)
        prompt = (directory/'system_prompt.md').read_text(encoding='utf-8')
        self.assertNotIn('天空度假村', prompt)
        from orchestrator.prompting import current_rules_note
        self.assertNotIn('天空度假村', current_rules_note(cfg, guide=runner._startup_guide))
        self.assertEqual(cfg.plan, [Piece(3)])


class ExecutorRestorePolicyTests(RestoreTaskFixture, unittest.TestCase):
    def test_only_codex_omits_targets_when_restoring(self):
        from orchestrator.run import restore_task_flags
        previous = {'dir': self.root}
        for engine in ('codex', 'claude'):
            cfg = replace(self.cfg, engine=engine)
            self.assertEqual(restore_task_flags(cfg, None), {'include_task': True, 'announce_goal': True})
            restored = restore_task_flags(cfg, previous)
            self.assertEqual(restored, {'include_task': engine == 'claude', 'announce_goal': engine == 'claude'})
            self.assertTrue(restore_task_flags(cfg, previous, 'new goal')['announce_goal'])

    def test_claude_restore_keeps_task_in_inline_rules_and_chat(self):
        cfg = replace(self.cfg, engine='claude', runtime='host')
        for chat_only in (False, True):
            directory = self.root/('claude-chat' if chat_only else 'claude-play')
            directory.mkdir()
            runner = ClaudeRunner(cfg, directory, SimpleNamespace(done=False),
                                  Metrics(directory/'metrics.json', 'test', 'test', 'medium', 60),
                                  'unused', session_id='old-session', resume=True, chat_only=chat_only)
            self.addCleanup(runner.close_events)
            self.assertTrue(runner.include_task)
            self.assertTrue(runner.announce_goal)
            with patch('orchestrator.agent.find_claude_exe', return_value='fixture-claude'):
                runner._command()
            self.assertIn('天空度假村', (directory/'system_prompt.md').read_text(encoding='utf-8'))
            from orchestrator.prompting import current_rules_note
            note = current_rules_note(cfg, guide=runner._startup_guide)
            self.assertIn('天空度假村', note)
            self.assertNotIn('任务目标从已有记录恢复', note)


class CodexRestoreInputTests(RestoreTaskFixture, unittest.IsolatedAsyncioTestCase):
    async def test_native_resume_receives_filtered_base_instructions_and_guide(self):
        directory = self.root/'codex'
        directory.mkdir()
        session = SimpleNamespace(done=False, done_reason='')
        runner = CodexRunner(self.cfg, directory, session,
                             Metrics(directory/'metrics.json', 'test', 'test', 'medium', 60), 'unused',
                             session_id='old-session', resume=True, include_task=False, announce_goal=False)
        self.addCleanup(runner.close_events)
        captured = {}

        async def request(method, params):
            if method == 'thread/resume':
                captured.update(params)
                raise RuntimeError('captured native restore request')
            return {}

        runner.rpc.start = AsyncMock()
        runner.rpc.close = AsyncMock()
        runner.rpc.request = AsyncMock(side_effect=request)
        with self.assertRaisesRegex(RuntimeError, 'captured native restore request'):
            await runner.run()
        self.assertEqual(captured['threadId'], 'old-session')
        self.assertNotIn('天空度假村', captured['baseInstructions'])
        self.assertNotIn('dynamicTools', captured)
        self.assertEqual(captured['permissions'], 'celeste-write')
        for path in (self.root/'workspace/game/instructions').glob('*/guide.md'):
            self.assertNotIn('天空度假村', path.read_text(encoding='utf-8'))
        runner.close_events()


if __name__ == '__main__':
    unittest.main()
