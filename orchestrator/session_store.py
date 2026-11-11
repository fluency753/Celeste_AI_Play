"""Persistent session JSON and the HUD empty startup state."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time


if os.name == 'nt':
    from .windows_state_file import replace_shared, shared_reader
else:
    shared_reader = None

_write_lock = threading.RLock()


@contextmanager
def locked_write(lock_path):
    """Serialize a read/modify/write cycle across threads and processes."""
    with _write_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+b') as stream:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def load_json(path, *, encoding='utf-8'):
    with open(path, encoding=encoding, opener=shared_reader) as stream:
        return json.load(stream)


def rename_directory(source, target):
    """Publish a completed directory, tolerating short Windows handle conflicts."""
    for delay in (0, .01, .02, .04, .08, .16, .32):
        if delay:
            time.sleep(delay)
        try:
            os.rename(source, target)
            if delay:
                print(f'[publish] 目录发布重试成功：{source} -> {target}', flush=True)
            return
        except OSError as exc:
            if (os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32)
                    or os.path.lexists(target) or delay == .32):
                raise
            if not delay:
                print(f'[publish] Windows 目录发布异常，将有限重试：{exc}', flush=True)


def replace_file(source, target):
    """Publish a complete file snapshot with bounded Windows sharing recovery."""
    windows = os.name == 'nt'
    native = windows
    for delay in (0, .01, .02, .04, .08, .16):
        if delay:
            time.sleep(delay)
        try:
            source.replace(target)
            return
        except OSError as exc:
            if not windows or getattr(exc, 'winerror', None) not in (5, 32):
                raise
            if native:
                try:
                    replace_shared(source, target)
                    return
                except OSError as shared_error:
                    code = getattr(shared_error, 'winerror', None)
                    if code in (1, 50, 87):  # Filesystem does not support the extended rename.
                        native = False
                    elif code not in (5, 32):
                        raise
            if delay == .16:
                raise


def write_json(path, data, *, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=indent)
        replace_file(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


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
