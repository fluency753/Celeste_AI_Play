"""Workspace helper publication uses the same files for fresh/resumed sessions and preparation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.workspace_setup import install_game_helpers


class WorkspaceHelperTests(unittest.TestCase):
    def test_new_and_resumed_sessions_refresh_helpers_without_touching_snapshots(self):
        source = Path(__file__).resolve().parents[1] / 'orchestrator' / 'workspace_files'
        with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
            root = Path(folder)
            cfg = RunConfig(agent='isolated', save='test', model='stub', engine='codex', runtime='host',
                            sim=False, chapter=3, cutscene_mode='agent')
            for resumed in (False, True):
                run = root / ('resume' if resumed else 'first')
                run.mkdir()
                session = GameSession(cfg, run, Metrics(run / 'metrics.json', 'isolated', 'stub', 'high', 60))
                try:
                    game = session.workspace / 'game'
                    for name in ('show.py', 'tas.py', 'collision_matrix.py'):
                        self.assertEqual((game / name).read_bytes(), (source / name).read_bytes())
                    self.assertTrue((game / 'collision_matrix.md').is_file())
                    self.assertFalse((game / 'collision_matrix.json').exists())
                    if resumed:
                        self.assertEqual((game / 'latest_obs.json').read_bytes(), b'{"frame":1}')
                    (game / 'latest_obs.json').write_bytes(b'{"frame":1}')
                    (game / 'collision_matrix.py').write_bytes(b'# stale helper')
                finally:
                    session.close(False)

    def test_failed_publication_preserves_previous_complete_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            install_game_helpers(workspace)
            game = workspace / 'game'
            original = (game / 'show.py').read_bytes()
            def failed_copy(source, destination):
                Path(destination).write_bytes(b'# incomplete')
                raise OSError('fixture write failure')
            with patch('orchestrator.workspace_setup.shutil.copyfile', failed_copy):
                with self.assertRaisesRegex(OSError, 'fixture write failure'):
                    install_game_helpers(workspace)
            self.assertEqual((game / 'show.py').read_bytes(), original)
            self.assertFalse(list(game.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
