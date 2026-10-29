"""Resume notes must respect each executor's actual instruction-update channel."""
import itertools
from pathlib import Path
import tempfile
import unittest

from orchestrator.config import RunConfig
from orchestrator.prompting import play_prompt
from orchestrator.run import opening_note
from orchestrator.segments import Piece


class ResumePromptTests(unittest.TestCase):
    def test_resume_and_extended_plan_use_only_the_required_rule_channel(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previous = {'dir': Path(folder.name), 'metrics': {}, 'state': {}}
        for engine, mode, runtime, extended, fresh in itertools.product(
                ('codex', 'claude'), ('explore', 'speedrun'), ('container', 'host'),
                (False, True), (False, True)):
            with self.subTest(engine=engine, mode=mode, runtime=runtime, extended=extended, fresh=fresh):
                cfg = RunConfig(agent='test', save='test', model='test', engine=engine, mode=mode, runtime=runtime)
                cfg.chapter, cfg.chapters, cfg.plan = 2, [2], [Piece(2, 0, None)]
                cfg._extended_plan = [Piece(2, 0, None)] if extended else None
                note = opening_note(cfg, previous, None if fresh else 'existing-session', '保留用户留言。')
                rules = play_prompt(cfg)
                needs_snapshot = engine == 'claude' and runtime == 'host' and not fresh
                self.assertEqual(rules in note, needs_snapshot)
                self.assertEqual('提示词快照' in note, needs_snapshot)
                self.assertEqual('后面附上的当前规则' in note, needs_snapshot and extended)
                self.assertEqual(note.count('保留用户留言。'), 1)
                self.assertIn('observe', note)
                if engine == 'codex':
                    self.assertLess(len(note), 700)
                    self.assertNotIn('# 从房间数据建立路线', note)
                if extended:
                    self.assertIn('旧址', note)
                    self.assertNotIn('主控中断', note)

    def test_new_run_has_no_resume_note(self):
        self.assertIsNone(opening_note(RunConfig(agent='test', save='test', model='test'), None, None))

    def test_live_exit_instructions_have_one_shared_owner(self):
        for mode, engine in itertools.product(('explore', 'speedrun'), ('codex', 'claude')):
            cfg = RunConfig(agent='test', save='test', model='test', engine=engine, mode=mode, runtime='container', live=True)
            prompt = play_prompt(cfg)
            self.assertEqual(prompt.count('只有用户明确结束整次试玩才设 finish_run=true'), 1)
            self.assertIn('返回地图不算通关', prompt)


if __name__ == '__main__':
    unittest.main()
