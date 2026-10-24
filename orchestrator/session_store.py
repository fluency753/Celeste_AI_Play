"""Idle HUD storage is not an agent conversation or a completed game run."""
import json
from pathlib import Path


def idle_anchor(root: Path) -> Path:
    directory = root / 'local' / 'hud-idle'
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in {
        'run_state.json': {'session_kind': 'idle', 'session_id': None, 'save': '', 'save_slot': None},
        'metrics.json': {'agent': '', 'model': '', 'status': 'starting', 'started_wall': 0, 'thought': '',
                         'chapter': '', 'room': '', 'splits': [], 'decisions': 0, 'deaths': 0},
    }.items():
        path = directory / name
        try:
            with path.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False)
        except FileExistsError:
            pass
    return directory
