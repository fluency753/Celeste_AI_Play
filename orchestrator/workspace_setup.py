"""Publish shared, file-only game helpers for new and resumed workspaces."""
from pathlib import Path
import os
import shutil
import tempfile


_HELPERS = ('show.py', 'tas.py', 'collision_matrix.py')


def install_game_helpers(workspace: Path) -> None:
    """Refresh code/docs only; never build a raster, connect to the game or alter observations."""
    root = Path(__file__).resolve().parent
    destination = Path(workspace) / 'game'
    destination.mkdir(parents=True, exist_ok=True)
    sources = [(root / 'workspace_files' / name, name) for name in _HELPERS]
    sources.append((root.parent / 'kb' / 'collision_matrix.md', 'collision_matrix.md'))
    for source, name in sources:
        fd, temporary = tempfile.mkstemp(prefix=f'.{name}.', suffix='.tmp', dir=destination)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            shutil.copymode(source, temporary)
            Path(temporary).replace(destination / name)
        finally:
            Path(temporary).unlink(missing_ok=True)
