"""Finished movie names; legacy MKV recordings remain readable."""
from pathlib import Path


def finished_movie(directory: Path, pure: bool = False) -> Path | None:
    stem = 'gameplay_pure' if pure else 'gameplay'
    return next((path for suffix in ('.mp4', '.mkv')
                 if (path := Path(directory) / (stem + suffix)).is_file()), None)
