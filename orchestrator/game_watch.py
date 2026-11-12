"""游戏进程与 Steam 客户端：开跑前核对游戏与 Steam 的连接，运行中记录游戏进程退出。

游戏启动时加载的 steamclient 只连着当时的 Steam 客户端。Steam 在游戏启动之后重启（例如自动更新）或退出，
游戏之后调用 Steam 时可能被 Steam 自己的处理器写下 dump 再直接结束进程：游戏日志没有异常栈、
Windows 也没有崩溃报告，主控只看到桥接连接断开。

- require_live_steam：开跑前发现这种情况就拒绝开始，请用户先重启游戏。检查本身失败（权限、找不到进程等）
  只输出提示，不阻止开跑。
- GameExitWatch：运行中持有游戏进程的只读句柄（只能等待与查询，不能影响游戏），每秒看一次；游戏退出时
  把退出码、退出时间、Steam 当时的状态、日志与 dump 位置追加到运行目录的 game_exits.jsonl，并输出一行。

只在 Windows 上生效；其他系统（及测试）可传入替身 system。
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import threading

STEAM_CLIENT_MODULES = frozenset({'steamclient64.dll', 'steamclient.dll'})
STEAM_PROCESS = 'steam.exe'
EXIT_LOG = 'game_exits.jsonl'
POLL_SECONDS = 1.0
DUMP_SLACK_S = 5.0          # File times and process times are rounded differently.
MAX_DUMPS = 10


def _iso(wall: float | None) -> str | None:
    return None if wall is None else datetime.fromtimestamp(wall).isoformat(timespec='milliseconds')


def game_dir_from(hello: dict) -> Path | None:
    """<游戏目录>/Mods/CelesteAIRuntime/bin/CelesteAIRuntime.dll → 游戏目录。"""
    runtime = hello.get('runtime_path') if isinstance(hello, dict) else None
    if not isinstance(runtime, str) or not runtime:
        return None
    try:
        parents = Path(runtime).resolve().parents
    except (OSError, RuntimeError):
        return None
    return parents[3] if len(parents) > 3 else None


def _inside(path: str | None, directory: Path) -> bool:
    """Compare as written and fully resolved: 8.3 short names (C:\\Users\\ADMINI~1) and links can differ."""
    if not path:
        return False
    for form in (os.path.abspath, os.path.realpath):
        try:
            if Path(os.path.normcase(form(path))).is_relative_to(Path(os.path.normcase(form(directory)))):
                return True
        except (OSError, ValueError):
            continue
    return False


class WindowsProcesses:
    """The few read-only Win32 calls used here."""

    SYNCHRONIZE = 0x00100000
    QUERY_LIMITED = 0x1000
    QUERY_INFORMATION = 0x0400
    VM_READ = 0x0010
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k.GetExitCodeProcess.restype = w.BOOL
        k.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        k.GetProcessTimes.restype = w.BOOL
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.K32EnumProcessModulesEx.argtypes = [w.HANDLE, ctypes.POINTER(w.HMODULE), w.DWORD,
                                              ctypes.POINTER(w.DWORD), w.DWORD]
        k.K32EnumProcessModulesEx.restype = w.BOOL
        k.K32GetModuleBaseNameW.argtypes = [w.HANDLE, w.HMODULE, w.LPWSTR, w.DWORD]
        k.K32GetModuleBaseNameW.restype = w.DWORD

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                        ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD),
                        ('cntThreads', w.DWORD), ('th32ParentProcessID', w.DWORD),
                        ('pcPriClassBase', ctypes.c_long), ('dwFlags', w.DWORD),
                        ('szExeFile', ctypes.c_wchar * 260)]
        self.entry_type = PROCESSENTRY32W
        k.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32FirstW.restype = w.BOOL
        k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.restype = w.BOOL
        self.k = k

    def open(self, pid: int, *, wait: bool = False):
        rights = self.QUERY_LIMITED | (self.SYNCHRONIZE if wait else 0)
        return self.k.OpenProcess(rights, False, pid) or None

    def close(self, handle) -> None:
        if handle:
            self.k.CloseHandle(handle)

    def wait(self, handle, timeout_s: float) -> bool:
        """True once the process has exited."""
        result = self.k.WaitForSingleObject(handle, max(0, int(timeout_s * 1000)))
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise self.ctypes.WinError(self.ctypes.get_last_error())

    def exit_code(self, handle) -> int | None:
        code = self.w.DWORD()
        return code.value if self.k.GetExitCodeProcess(handle, self.ctypes.byref(code)) else None

    def times(self, handle) -> tuple[float | None, float | None]:
        """(创建, 退出) 的 Unix 时间；还在运行时退出为 None。"""
        stamps = [self.w.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(self.ctypes.byref(t) for t in stamps)):
            return None, None

        def unix(stamp):
            value = stamp.dwLowDateTime | (stamp.dwHighDateTime << 32)
            return value / 10_000_000 - 11_644_473_600 if value else None
        return unix(stamps[0]), unix(stamps[1])

    def executable(self, handle) -> str | None:
        size = self.w.DWORD(32768)
        buffer = self.ctypes.create_unicode_buffer(size.value)
        if not self.k.QueryFullProcessImageNameW(handle, 0, buffer, self.ctypes.byref(size)):
            return None
        return buffer.value

    def modules(self, pid: int) -> set[str] | None:
        """进程加载的模块文件名（小写）；无权读取时为 None。"""
        handle = self.k.OpenProcess(self.QUERY_INFORMATION | self.VM_READ, False, pid)
        if not handle:
            return None
        try:
            count = 1024
            while True:
                array = (self.w.HMODULE * count)()
                needed = self.w.DWORD()
                if not self.k.K32EnumProcessModulesEx(handle, array, self.ctypes.sizeof(array),
                                                      self.ctypes.byref(needed), 3):   # LIST_MODULES_ALL
                    return None
                total = needed.value // self.ctypes.sizeof(self.w.HMODULE)
                if total <= count:
                    break
                count = total + 64
            names = set()
            buffer = self.ctypes.create_unicode_buffer(260)
            for module in array[:total]:
                if module and self.k.K32GetModuleBaseNameW(handle, module, buffer, 260):
                    names.add(buffer.value.lower())
            return names
        finally:
            self.k.CloseHandle(handle)

    def pids(self, name: str) -> list[int]:
        snapshot = self.k.CreateToolhelp32Snapshot(0x2, 0)           # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            entry = self.entry_type()
            entry.dwSize = self.ctypes.sizeof(entry)
            found, more = [], self.k.Process32FirstW(snapshot, self.ctypes.byref(entry))
            while more:
                if entry.szExeFile.lower() == name.lower():
                    found.append(int(entry.th32ProcessID))
                more = self.k.Process32NextW(snapshot, self.ctypes.byref(entry))
            return found
        finally:
            self.k.CloseHandle(snapshot)


def _system(system):
    if system is not None:
        return system
    if os.name != 'nt':
        return None
    return WindowsProcesses()


def _steam_processes(system) -> list[dict]:
    rows = []
    for pid in system.pids(STEAM_PROCESS):
        handle = system.open(pid)
        if not handle:
            rows.append({'pid': pid, 'started': None, 'executable': None})
            continue
        try:
            rows.append({'pid': pid, 'started': system.times(handle)[0], 'executable': system.executable(handle)})
        finally:
            system.close(handle)
    return rows


def _relation(game_started: float | None, steam: list[dict]) -> str:
    """Steam 客户端相对游戏启动的先后：steam_before_game／steam_after_game／steam_not_running／unknown。"""
    if not steam:
        return 'steam_not_running'
    starts = [row['started'] for row in steam]
    if game_started is None or any(start is None for start in starts):
        return 'unknown'
    return 'steam_before_game' if any(start <= game_started for start in starts) else 'steam_after_game'


def steam_check(pid, game_dir: Path | None, system=None) -> dict:
    """只读核对游戏进程与 Steam 客户端。checked=False 表示没能核对（原因见 reason），不代表有问题。"""
    system = _system(system)
    if system is None:
        return {'checked': False, 'reason': '仅在 Windows 上核对'}
    if type(pid) is not int or pid <= 0 or game_dir is None:
        return {'checked': False, 'reason': '桥接没有报告游戏进程或游戏目录'}
    handle = system.open(pid)
    if not handle:
        return {'checked': False, 'reason': f'无法打开游戏进程 {pid}'}
    try:
        executable = system.executable(handle)
        started = system.times(handle)[0]
    finally:
        system.close(handle)
    if not _inside(executable, game_dir):
        # dotnet 宿主等情况无法确认是这个游戏：不下结论。
        return {'checked': False, 'reason': f'进程 {pid} 的程序 {executable} 不在游戏目录 {game_dir} 里'}
    modules = system.modules(pid)
    steam = _steam_processes(system)
    loaded = None if modules is None else bool(modules & STEAM_CLIENT_MODULES)
    relation = _relation(started, steam)
    return {'checked': True, 'pid': pid, 'executable': executable, 'game_dir': str(game_dir), 'game_started': started,
            'steam_client_loaded': loaded, 'steam': steam, 'relation': relation,
            'stale': loaded is True and relation in ('steam_after_game', 'steam_not_running')}


def require_live_steam(hello: dict, log=print, system=None) -> dict | None:
    """开跑前：游戏加载了 Steam 客户端、而 Steam 在游戏启动后重启过或已退出时拒绝开始。"""
    try:
        facts = steam_check(hello.get('process_id'), game_dir_from(hello), system)
    except Exception as exc:  # noqa: BLE001 - a failed check never blocks a run
        log(f'[run] 游戏与 Steam 的连接没有核对完（{type(exc).__name__}: {exc}），照常开始')
        return None
    if not facts.get('checked'):
        return facts
    if facts['stale']:
        what = ('Steam 客户端在游戏启动之后重启过' if facts['relation'] == 'steam_after_game'
                else 'Steam 客户端在游戏启动之后已退出')
        raise RuntimeError(f'{what}，游戏仍连着原来的 Steam 客户端；之后游戏调用 Steam 时可能被直接结束，'
                           '没有游戏日志也没有 Windows 崩溃报告。请先从游戏菜单退出游戏，再从控制台启动 AI 副本'
                           '（会先确认 Steam 已启动）后开跑。未开始录制或进档。')
    if facts['steam_client_loaded'] is None and facts['relation'] in ('steam_after_game', 'steam_not_running'):
        log('[run] Steam 客户端在游戏启动后重启过或已退出，但读不到游戏加载的模块，无法确认游戏是否连着 Steam；照常开始。'
            '若随后游戏无故退出，请先重启游戏')
    return facts


class GameExitWatch:
    """运行中等游戏进程退出并留下记录；只持有等待／查询权限的句柄，不影响游戏。"""

    def __init__(self, run_dir: Path, log=print, system=None, poll_s: float = POLL_SECONDS):
        self.run_dir = Path(run_dir)
        self.log = log
        self.system = system
        self.poll_s = poll_s
        self.records: list[dict] = []
        self.pid = None
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def attach(self, hello: dict) -> bool:
        """在桥接 hello 之后调用；同一进程重复调用无副作用，换了进程则改看新进程。"""
        pid = hello.get('process_id') if isinstance(hello, dict) else None
        if type(pid) is not int or pid <= 0:
            return False
        with self._lock:
            if pid == self.pid and self._thread is not None and self._thread.is_alive():
                return True
        self.close()
        try:
            system = _system(self.system)
            if system is None:
                return False
            facts = steam_check(pid, game_dir_from(hello), system)
            if not facts.get('checked'):
                return False
            handle = system.open(pid, wait=True)
            if not handle:
                return False
            if system.times(handle)[0] != facts['game_started']:     # PID was reused in between.
                system.close(handle)
                return False
        except Exception as exc:  # noqa: BLE001 - watching is diagnostics only
            self.log(f'[game] 无法记录游戏进程退出（{type(exc).__name__}: {exc}）')
            return False
        stop = threading.Event()
        thread = threading.Thread(target=self._run, args=(system, handle, stop, facts),
                                  name=f'game-exit-watch-{pid}', daemon=True)
        with self._lock:
            self.pid, self._stop, self._thread = pid, stop, thread
        thread.start()
        return True

    def close(self) -> None:
        """停止等待；停止前再看一次，刚退出的进程也会被记下。"""
        with self._lock:
            stop, thread, self._stop, self._thread, self.pid = self._stop, self._thread, None, None, None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(5.0, self.poll_s * 3))

    def _run(self, system, handle, stop, facts) -> None:
        try:
            while True:
                stopping = stop.is_set()
                if system.wait(handle, 0 if stopping else self.poll_s):
                    self._record(system, handle, facts)
                    return
                if stopping:
                    return
        except Exception as exc:  # noqa: BLE001
            self.log(f'[game] 游戏进程退出记录中断（{type(exc).__name__}: {exc}）')
        finally:
            system.close(handle)

    def _record(self, system, handle, facts) -> None:
        code = system.exit_code(handle)
        started, exited = system.times(handle)
        started = started if started is not None else facts.get('game_started')
        try:
            steam = _steam_processes(system)
        except Exception:  # noqa: BLE001
            steam = None
        game_dir = Path(facts['game_dir'])
        record = {
            'pid': facts['pid'], 'executable': facts.get('executable'),
            'started_at': _iso(started), 'exited_at': _iso(exited), 'recorded_at': _iso(datetime.now().timestamp()),
            'exit_code': code, 'exit_code_hex': None if code is None else f'0x{code:08X}',
            'steam_client_loaded': facts.get('steam_client_loaded'),
            'steam_at_start': facts.get('relation'),
            'steam_at_exit': None if steam is None else _relation(started, steam),
            'game_log': _file_fact(game_dir / 'log.txt', started),
            'error_log': _file_fact(game_dir / 'errorLog.txt', started),
            'steam_dumps': _dumps(steam, started),
        }
        try:
            with open(self.run_dir / EXIT_LOG, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError as exc:
            self.log(f'[game] 游戏退出记录写入失败：{exc}')
        steam_text = {'steam_after_game': 'Steam 客户端在游戏启动后重启过',
                      'steam_not_running': 'Steam 客户端已不在运行',
                      'steam_before_game': 'Steam 客户端未重启'}.get(record['steam_at_exit'], 'Steam 状态未知')
        dumps = record['steam_dumps']
        self.log(f"[game] 游戏进程 {facts['pid']} 已退出：退出码 {record['exit_code_hex'] or '未知'}，"
                 f"时间 {record['exited_at'] or '未知'}；{steam_text}"
                 + (f"；游戏启动后 Steam 写了 {len(dumps)} 个 dump" if dumps else '')
                 + f'；详情见 {EXIT_LOG}')
        self.records.append(record)      # Last: a visible record means the file and the log line are done.


def _file_fact(path: Path, since: float | None) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {'path': str(path), 'bytes': stat.st_size, 'modified_at': _iso(stat.st_mtime),
            'modified_since_start': None if since is None else stat.st_mtime >= since - DUMP_SLACK_S}


def _steam_dir(steam: list[dict] | None) -> Path | None:
    for row in steam or ():
        if row.get('executable'):
            return Path(row['executable']).parent
    if os.name != 'nt':
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            return Path(winreg.QueryValueEx(key, 'SteamPath')[0])
    except OSError:
        return None


def _dumps(steam: list[dict] | None, since: float | None) -> list[dict]:
    """游戏启动后 Steam 写下的 dump（只列文件名、大小、时间，不读取内容）。"""
    directory = _steam_dir(steam)
    if directory is None or since is None:
        return []
    found = []
    try:
        for path in (directory / 'dumps').iterdir():
            if not path.is_file() or path.suffix.lower() != '.dmp':
                continue
            stat = path.stat()
            if stat.st_mtime >= since - DUMP_SLACK_S:
                found.append((stat.st_mtime, {'path': str(path), 'bytes': stat.st_size,
                                              'modified_at': _iso(stat.st_mtime)}))
    except OSError:
        return []
    found.sort(key=lambda item: item[0])
    return [row for _, row in found[-MAX_DUMPS:]]
