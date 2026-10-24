"""Persistent session JSON and the HUD empty startup state."""
import json
from pathlib import Path


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


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
