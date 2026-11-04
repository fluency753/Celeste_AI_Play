"""Desktop HUD startup: resolve the current service and own one window per checkout."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

from orchestrator.session_store import load_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTS = (47790, 47792)
HUD_TITLE = 'Celeste AI HUD'


def find_hud_window(title=None):
    """Find a hidden or visible WinForms HUD, excluding same-title message boxes."""
    title = HUD_TITLE if title is None else title
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    found = []

    @callback_type
    def visit(hwnd, _):
        caption = ctypes.create_unicode_buffer(len(title) + 2)
        user32.GetWindowTextW(hwnd, caption, len(caption))
        if caption.value == title:
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, len(name))
            if name.value.startswith('WindowsForms10.Window.'):
                found.append(hwnd)
                return False
        return True

    if not user32.EnumWindows(visit, 0) and not found:
        raise ctypes.WinError(ctypes.get_last_error())
    return found[0] if found else None


def _object(path):
    try:
        value = load_json(path, encoding='utf-8-sig')
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _state(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/hud/state', timeout=1) as response:
            data = json.loads(response.read(128 * 1024).decode('utf-8-sig'))
        return data if isinstance(data, dict) and 'config' in data and 'metrics' in data else None
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
    except (OSError, ValueError):
        return None


def current_hud_url(root=ROOT):
    """Only use a service after verifying its actual selected run; never restart one."""
    try:
        value = (root / 'runs/latest.txt').read_text(encoding='utf-8-sig').strip()
        anchor = Path(value) if Path(value).is_absolute() else root / value
        anchor = anchor.resolve() if value and anchor.is_dir() else None
    except OSError:
        anchor = None
    from orchestrator.conversations import selected_run
    selected = selected_run(anchor)
    state = _object(anchor / 'run_state.json') if anchor else {}
    host = state.get('controller_host')
    arguments = _object(Path(host) / 'launch.json').get('arguments', []) if isinstance(host, str) and host else []
    ports = []
    if isinstance(arguments, list):
        for index, argument in enumerate(arguments):
            candidate = arguments[index + 1] if argument == '--hud-port' and index + 1 < len(arguments) else \
                argument.split('=', 1)[1] if isinstance(argument, str) and argument.startswith('--hud-port=') else None
            try:
                port = int(candidate)
                if 1 <= port <= 65535:
                    ports.append(port)
            except (TypeError, ValueError):
                pass
    ports += DEFAULT_PORTS

    def matches(data):
        if data is None:
            return False
        actual = data.get('run')
        if selected is None:
            return True
        try:
            return isinstance(actual, str) and Path(actual).resolve() == selected.resolve()
        except (OSError, ValueError):
            return False

    for port in dict.fromkeys(ports):
        if matches(_state(port)):
            return f'http://127.0.0.1:{port}/hud'
    from .console import ensure_service
    default_port = DEFAULT_PORTS[0]
    try:
        ensure_service(default_port)
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc
    if not matches(_state(default_port)):
        raise RuntimeError('默认 HUD 服务显示的是其他运行，未修改该服务。请使用 --url 显式指定 HUD 地址。')
    return f'http://127.0.0.1:{default_port}/hud'


class HudInstance:
    """The owner keeps both handles; a second opener only signals its restore queue."""
    def __init__(self, *, root=ROOT):
        scope = hashlib.sha256(str(root.resolve()).casefold().encode('utf-8')).hexdigest()[:20]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        self.kernel.CreateEventW.restype = wt.HANDLE
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        self.kernel.CreateMutexW.restype = wt.HANDLE
        self.kernel.SetEvent.argtypes = [wt.HANDLE]
        self.kernel.SetEvent.restype = wt.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        self.kernel.WaitForSingleObject.restype = wt.DWORD
        self.kernel.CloseHandle.argtypes = [wt.HANDLE]
        self.event = self.mutex = None
        try:
            self.event = self.kernel.CreateEventW(None, False, False, f'Local\\CelesteAIHud-{scope}-restore')
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.set_last_error(0)
            self.mutex = self.kernel.CreateMutexW(None, False, f'Local\\CelesteAIHud-{scope}')
            already_exists = ctypes.get_last_error() == 183
            if not self.mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            self.is_owner = not already_exists
        except Exception:
            self.close()
            raise

    def request_restore(self):
        if not self.kernel.SetEvent(self.event):
            raise ctypes.WinError(ctypes.get_last_error())

    def wake_requested(self):
        result = self.kernel.WaitForSingleObject(self.event, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def close(self):
        for name in ('mutex', 'event'):
            handle = getattr(self, name)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
