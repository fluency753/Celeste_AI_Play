"""Regressions from independent reminder lifecycle and runtime capability review."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calibration.note_reminders_test import runner_fixture, codex_usage, claude_usage
from orchestrator.note_reminders import NoteReminders


class NoteReminderReviewTests(unittest.TestCase):
    def test_withdrawn_unreadable_warning_can_be_offered_after_observability_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notes.md'
            path.write_bytes(b'original')
            notes = NoteReminders(Path(directory), lambda: 60)
            notes.usage(700, 1000)
            self.assertTrue(notes.peek())
            with patch.object(notes, '_fingerprint', return_value=None):
                self.assertEqual(notes.peek(), ())
            notes.usage(700, 1000)
            self.assertFalse(notes.updated)
            self.assertIn('本监测周期尚未', notes.take())
            self.assertEqual(notes.peek(), ())

    def test_already_delivered_warning_does_not_repeat_after_temporary_unreadability(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'notes.md').write_bytes(b'original')
            notes = NoteReminders(Path(directory), lambda: 60)
            notes.usage(700, 1000)
            self.assertTrue(notes.take())
            with patch.object(notes, '_fingerprint', return_value=None):
                self.assertEqual(notes.peek(), ())
            notes.usage(900, 1000)
            self.assertEqual(notes.peek(), ())

    def test_codex_does_not_use_unapplied_claude_autocompact_as_context_window(self):
        with runner_fixture() as (runner, session, path):
            runner.cfg.autocompact = '1000'
            runner.cfg.provider_info['autocompact'] = '1000'
            codex_usage(runner, 900, window=None)
            self.assertIsNone(runner._notes_window())
            self.assertEqual(runner.note_reminders.peek(), ())
            codex_usage(runner, 900, window=1000)
            self.assertTrue(runner.note_reminders.peek())

    def test_claude_effective_provider_budget_matches_actual_cli_arguments(self):
        with runner_fixture('claude') as (runner, session, path):
            runner.cfg.autocompact = None
            runner.cfg.provider_info['autocompact'] = '1000'
            command = runner._common_args()
            self.assertEqual(command[command.index('--autocompact') + 1], '1000')
            self.assertEqual(runner._notes_window(), 1000)
            claude_usage(runner, 700)
            self.assertTrue(runner.note_reminders.peek())
            runner.cfg.autocompact = '2000'
            self.assertEqual(runner._notes_window(), 2000)
            command = runner._common_args()
            self.assertEqual(command[command.index('--autocompact') + 1], '2000')
            runner.metrics.update(context_window=4000)
            self.assertEqual(runner._notes_window(), 4000)


if __name__ == '__main__':
    unittest.main()
