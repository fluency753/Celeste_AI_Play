"""The setup check looks at the same game directory the controller uses."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import gamepaths
from tools import setup_wizard as wizard


class SetupGameDirTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.personal, self.copy = self.root / 'steam/Celeste', self.root / 'instances/main'
        for game in (self.personal, self.copy):
            game.mkdir(parents=True)
            (game / 'Celeste.exe').write_bytes(b'')
        self.enterContext(patch.object(gamepaths, 'CONSOLE_PREFS', self.root / 'local.console.json'))
        self.enterContext(patch.object(gamepaths, 'DEPLOY_ANSWERS', self.root / 'local.deploy.json'))
        self.enterContext(patch.dict('os.environ', {}, clear=False))
        import os
        os.environ.pop('CELESTE_DIR', None)
        self.enterContext(patch.object(wizard, 'find_celeste_candidates', return_value=[self.personal]))

    def check(self, answers=None):
        out = io.StringIO()
        a = wizard.Answers(answers or {})
        with patch.object(wizard, 'CHECK_ONLY', True), contextlib.redirect_stdout(out):
            wizard.step_game(a)
        return a, out.getvalue()

    def test_check_uses_the_console_game_location_without_a_wizard_answer(self):
        (self.root / 'local.console.json').write_text(json.dumps({'celeste_dir': str(self.copy)}), encoding='utf-8')
        a, out = self.check()
        self.assertEqual(a['celeste_dir'], str(self.copy))      # not the Steam copy found automatically
        self.assertIn('控制台里填的位置', out)
        self.assertFalse((self.root / 'local.deploy.json').exists())

    def test_console_location_wins_over_an_old_wizard_answer(self):
        (self.root / 'local.console.json').write_text(json.dumps({'celeste_dir': str(self.copy)}), encoding='utf-8')
        a, _ = self.check({'celeste_dir': str(self.personal)})
        self.assertEqual(a['celeste_dir'], str(self.copy))

    def test_without_configuration_falls_back_to_the_found_game(self):
        a, _ = self.check()
        self.assertEqual(a['celeste_dir'], str(self.personal))


if __name__ == '__main__':
    unittest.main()
