"""Provider-independent play range and optional simulator availability."""
from pathlib import Path

from .config import CHAPTER_SIDS, REPO_ROOT


def sim_available(root: Path = REPO_ROOT) -> bool:
    return (root / 'sim' / 'dist' / 'celeste-sim').is_file()


def chapter_sequence(chapter: int, scope: str) -> list[int]:
    if type(chapter) is not int or chapter not in CHAPTER_SIDS:
        raise ValueError('请选择有效的官方章节')
    if scope not in {'chapter', 'campaign'}:
        raise ValueError('试玩范围须为单章或顺序游玩')
    return [chapter] if scope == 'chapter' else [i for i in sorted(CHAPTER_SIDS) if i >= chapter]
