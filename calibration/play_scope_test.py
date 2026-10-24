import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from orchestrator.play_scope import chapter_sequence
from orchestrator.run import parse_args


class PlayScopeTests(unittest.TestCase):
    def parse(self,root,*extra):
        argv=['run','--engine','codex','--agent','test','--save','selected','--chapter','0',*extra]
        with patch('orchestrator.config.REPO_ROOT',root),patch.object(sys,'argv',argv):return parse_args()[0]

    def test_missing_optional_simulator_does_not_block_game(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(self.parse(Path(folder)).sim_assist)
            with self.assertRaises(SystemExit):self.parse(Path(folder),'--sim-assist')

    def test_scope_is_separate_from_stop_goal_and_new_save_can_use_campaign(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=self.parse(Path(folder),'--scope','campaign','--create-save')
            self.assertEqual(cfg.chapters,chapter_sequence(0,'campaign'))
            self.assertEqual(cfg.chapters[-1],10)
            self.assertEqual(chapter_sequence(1,'chapter'),[1])

    def test_unknown_scope_and_boolean_chapter_are_rejected(self):
        for chapter,scope in [(True,'chapter'),(0,'unknown'),(99,'campaign')]:
            with self.assertRaises(ValueError):chapter_sequence(chapter,scope)

    def test_claude_can_select_explore_without_private_simulator(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = self.parse(Path(folder), '--engine', 'claude', '--knowledge', 'explore', '--no-sim-assist')
            self.assertEqual(cfg.engine, 'claude')
            self.assertEqual(cfg.knowledge_profile, 'explore')
            self.assertFalse(cfg.sim)
