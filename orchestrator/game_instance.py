"""AI 专用游戏副本：独立的游戏目录，有自己的存档、设置、Mod 环境与桥接连接，个人的《蔚蓝》可以同时游玩。

副本是从个人安装复制出来的一份完整游戏目录（默认 local/instances/<名字>，不入库），不带个人的 Mods 与 Saves：
- Mods 里只有 CelesteAIRuntime 与 CelesteAIBridge（从源安装复制，之后用 tools/deploy_bridge.ps1 -CelesteDir 副本目录 更新）；
- Saves 从空开始，只把源安装的 settings.celeste 与 modsettings-Everest.celeste 作为初始设置复制一份；
  需要已有进度时用 copy_saves 复制指定的存档槽（之后只写副本，不回写个人存档）；
- steam_appid.txt 默认写 480（Valve 公开的 Spacewar 测试程序）：Steam 版《蔚蓝》启动时必须连上正在运行的 Steam，
  写 480 时 Steam 把副本当成 Spacewar，你自己的《蔚蓝》仍能从 Steam 正常启动，AI 也不会给账号解锁《蔚蓝》成就；
- 目录里的 celeste-ai-instance.json 标记它是副本（名字、窗口标题、来源）。

游戏位置（控制台“游戏位置”或 CELESTE_DIR）指向副本时进入副本模式：主控只接管桥接报告的模组路径在副本里的游戏，
把它的窗口标题改成标记里的标题；找游戏窗口（还原最小化、采集尺寸）只认副本目录里的 Celeste.exe；OBS 按窗口标题采集，
标题不符的窗口（个人游戏）不会被录进去。输入不需要另外隔离：游戏窗口没有焦点时，Monocle 的 MInput 把键盘与手柄都清空。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import REPO_ROOT

MARKER = 'celeste-ai-instance.json'
DEFAULT_TITLE = 'Celeste AI Play'
DEFAULT_APPID = '480'
INSTANCES_ROOT = REPO_ROOT / 'local' / 'instances'
AI_MODS = ('CelesteAIRuntime', 'CelesteAIBridge')
SETTING_TEMPLATES = ('settings.celeste', 'modsettings-Everest.celeste')
# 不复制的顶层目录与文件：个人的 Mod 与存档、备份与日志、外部工具（副本只需要能运行的游戏本体）
SKIP_DIRS = {'mods', 'saves', 'backups', 'loghistory', 'crashlogs', 'celemod_yaml_cache',
             'celemod_blacklist_profiles', 'celestestudio', 'consistencytracker', 'external-tools',
             'celesteai-backups', 'moddump'}
SKIP_FILES = {'log.txt', 'celeste.tas', 'imgui.ini', 'miniinstaller-log.txt', 'steam_appid.txt',
              'error_log.txt', MARKER}


def _is_game_dir(path: Path) -> bool:
    return (path / 'Celeste.exe').is_file() or (path / 'Celeste.dll').is_file()


def _same(a, b) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def inside(path, root) -> bool:
    """path 在 root 里（Windows 不分大小写）。"""
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:      # 不同盘符
        return False


def info(game_dir) -> dict | None:
    """game_dir 是副本时返回它的标记（另加 dir）；不是副本或标记无效时返回 None。"""
    if not game_dir:
        return None
    path = Path(game_dir) / MARKER
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    title = data.get('window_title')
    if data.get('format') != 1 or not isinstance(title, str) or not title.strip() or len(title) > 80:
        return None
    return {**data, 'dir': str(Path(game_dir))}


def current() -> dict | None:
    """明确配置的游戏位置（环境变量、控制台、部署向导）是副本时返回它的标记。
    只看明确配置的位置：自动查找会去找正在运行的游戏窗口，而找窗口本身要先知道是不是副本模式。"""
    from .gamepaths import configured_dir
    return info(configured_dir())


# ---------------------------------------------------------------- 创建与更新

def _copy_game(source: Path, target: Path, log=print) -> int:
    """复制游戏本体（跳过 SKIP_DIRS / SKIP_FILES，只看顶层），返回复制的文件数。"""
    count = 0

    def ignore(directory, names):
        if not _same(directory, source):
            return []
        return [n for n in names if (n.lower() in SKIP_DIRS and (Path(directory) / n).is_dir())
                or (n.lower() in SKIP_FILES and (Path(directory) / n).is_file())]

    def copy(src, dst):
        nonlocal count
        count += 1
        return shutil.copy2(src, dst)

    shutil.copytree(source, target, ignore=ignore, copy_function=copy, dirs_exist_ok=True, symlinks=False)
    log(f'[instance] 已复制游戏本体：{count} 个文件')
    return count


def _write_marker(target: Path, data: dict) -> None:
    from .session_store import write_json
    write_json(target / MARKER, data, indent=2)


def create(name: str = 'main', source=None, target=None, *, title: str = DEFAULT_TITLE,
           steam_appid: str = DEFAULT_APPID, log=print) -> dict:
    """从 source（默认现在用的游戏目录）复制出副本。目标目录必须不存在或为空；不改动源安装的任何文件。"""
    if not isinstance(name, str) or not name or not all(c.isalnum() or c in '-_' for c in name) or len(name) > 40:
        raise ValueError('副本名字只能用字母、数字、- 和 _（最多 40 个字符）')
    if ':' in title or not title.strip() or len(title) > 80:
        raise ValueError('窗口标题不能为空、不能含冒号，最多 80 个字符')
    if not str(steam_appid).isdigit():
        raise ValueError('steam_appid 必须是数字')
    if source is None:
        from .gamepaths import celeste_dir
        source = celeste_dir()
    source = Path(source).resolve()
    if not _is_game_dir(source):
        raise ValueError(f'{source} 里没有 Celeste.exe，不是《蔚蓝》的安装目录')
    if info(source):
        raise ValueError(f'{source} 本身就是 AI 副本；请从个人安装复制')
    target = Path(target).resolve() if target else (INSTANCES_ROOT / name).resolve()
    if inside(target, source):
        raise ValueError('副本目录不能放在源安装里面')
    if target.exists() and any(target.iterdir()):
        raise ValueError(f'{target} 已存在且不为空；更新已有副本请用 update')
    log(f'[instance] 从 {source} 复制到 {target}（不复制 Mods、Saves、日志与备份）')
    _copy_game(source, target, log)
    mods, saves = target / 'Mods', target / 'Saves'
    mods.mkdir(exist_ok=True)
    saves.mkdir(exist_ok=True)
    missing = []
    for module in AI_MODS:
        installed = source / 'Mods' / module
        if (installed / 'everest.yaml').is_file():
            shutil.copytree(installed, mods / module, dirs_exist_ok=True)
        else:
            missing.append(module)
    for name_ in SETTING_TEMPLATES:     # 只是初始设置（画面、按键、语言、Everest 选项），不是存档
        if (source / 'Saves' / name_).is_file():
            shutil.copy2(source / 'Saves' / name_, saves / name_)
    (target / 'steam_appid.txt').write_text(str(steam_appid), encoding='ascii')
    data = {'format': 1, 'name': name, 'window_title': title, 'source': str(source),
            'steam_appid': str(steam_appid), 'created': time.strftime('%Y-%m-%d %H:%M:%S')}
    _write_marker(target, data)
    if missing:
        log(f'[instance] 源安装里没有 {"、".join(missing)}：请运行 .\\tools\\deploy_bridge.ps1 -CelesteDir "{target}" 安装桥接')
    log(f'[instance] 副本已创建：{target}')
    return {**data, 'dir': str(target), 'missing_mods': missing}


def update(game_dir=None, source=None, log=print) -> dict:
    """用源安装（默认标记里记下的来源）刷新副本的游戏本体；不动副本的 Mods、Saves、标记与 steam_appid.txt。"""
    marker = info(game_dir) if game_dir else current()
    if marker is None:
        raise ValueError('不是 AI 副本目录（缺少 celeste-ai-instance.json）')
    target = Path(marker['dir'])
    source = Path(source or marker.get('source') or '')
    if not _is_game_dir(source) or info(source):
        raise ValueError(f'来源 {source} 不是个人的《蔚蓝》安装目录')
    if game_processes(target):
        raise RuntimeError('AI 副本的游戏正在运行，请先关闭它再更新')
    _copy_game(source, target, log)
    marker = {k: v for k, v in marker.items() if k != 'dir'}
    marker.update(source=str(source.resolve()), updated=time.strftime('%Y-%m-%d %H:%M:%S'))
    _write_marker(target, marker)
    return {**marker, 'dir': str(target)}


def _slot_files(saves: Path, slot: int) -> list[Path]:
    """一个存档槽的全部文件：<槽>.celeste 与 Everest 的 <槽>-modsave-*、<槽>-modsession-*、<槽>-modsavedata。"""
    if not saves.is_dir():
        return []
    return sorted(p for p in saves.iterdir() if p.is_file() and p.suffix == '.celeste'
                  and (p.name == f'{slot}.celeste' or p.name.startswith(f'{slot}-')))


def _save_name(path: Path) -> str:
    import re
    try:
        found = re.search(r'<Name>([^<]*)</Name>', path.read_text(encoding='utf-8-sig', errors='replace'))
    except OSError:
        return ''
    return found.group(1) if found else ''


def copy_saves(slots, game_dir=None, source=None, log=print) -> list[dict]:
    """把源安装（默认标记里记下的来源）的这些存档槽原样复制进副本；只读源安装。
    之后副本的进度只写副本，不回写个人存档。副本里已有同号存档槽时拒绝（不覆盖副本的进度）。"""
    marker = info(game_dir) if game_dir else current()
    if marker is None:
        raise ValueError('不是 AI 副本目录（缺少 celeste-ai-instance.json）')
    target = Path(marker['dir'])
    source = Path(source or marker.get('source') or '')
    if not _is_game_dir(source) or info(source):
        raise ValueError(f'来源 {source} 不是个人的《蔚蓝》安装目录')
    slots = list(dict.fromkeys(slots))
    if not slots or not all(type(s) is int and s >= 0 for s in slots):
        raise ValueError('存档槽号必须是不小于 0 的整数')
    if game_processes(target) or game_processes(source):
        raise RuntimeError('个人的《蔚蓝》或 AI 副本正在运行：请先关闭游戏再复制存档（运行中的游戏随时可能写存档）')
    plan = []
    for slot in slots:
        files = _slot_files(source / 'Saves', slot)
        if not any(p.name == f'{slot}.celeste' for p in files):
            raise ValueError(f'源安装没有存档槽 {slot}（{source / "Saves" / f"{slot}.celeste"}）')
        if existing := _slot_files(target / 'Saves', slot):
            raise ValueError(f'副本里已经有存档槽 {slot}（{existing[0].name} 等），不覆盖副本的进度')
        plan.append((slot, files))
    (target / 'Saves').mkdir(exist_ok=True)
    copied = []
    for slot, files in plan:
        for path in files:
            shutil.copy2(path, target / 'Saves' / path.name)
        name = _save_name(target / 'Saves' / f'{slot}.celeste')
        log(f'[instance] 已复制存档槽 {slot}（{name or "未命名"}，{len(files)} 个文件）')
        copied.append({'slot': slot, 'name': name, 'files': len(files)})
    return copied


def select(game_dir) -> None:
    """把控制台的游戏位置（configs/local.console.json 的 celeste_dir）设为这个目录。"""
    from .session_store import write_json
    path = REPO_ROOT / 'configs' / 'local.console.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data['celeste_dir'] = str(Path(game_dir))
    write_json(path, data, indent=2)


# ---------------------------------------------------------------- 进程、Steam 与启动

def game_processes(game_dir) -> list[int]:
    """game_dir 里的 Celeste.exe 正在运行的进程号（只在 Windows 上查；别的系统返回空）。"""
    if sys.platform != 'win32':
        return []
    from .gamewindow import process_path
    import ctypes
    from ctypes import wintypes as wt
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    enum = kernel32.K32EnumProcesses
    enum.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.POINTER(wt.DWORD)]
    size = 4096
    while True:
        pids = (wt.DWORD * size)()
        used = wt.DWORD()
        if not enum(pids, ctypes.sizeof(pids), ctypes.byref(used)):
            raise ctypes.WinError(ctypes.get_last_error())
        if used.value < ctypes.sizeof(pids):
            break
        size *= 2
    found = []
    for pid in pids[:used.value // ctypes.sizeof(wt.DWORD)]:
        path = process_path(pid)
        if path and os.path.basename(path).lower() == 'celeste.exe' and inside(path, game_dir):
            found.append(int(pid))
    return found


def _detached(args, cwd=None):
    """独立于启动它的进程（控制台或主控宿主退出、所在作业被关掉时，游戏与 Steam 不跟着被结束）。"""
    flags = (getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    try:
        return subprocess.Popen(args, cwd=cwd, close_fds=True, creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(args, cwd=cwd, close_fds=True, creationflags=flags)


def _steam_registry():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
        exe = winreg.QueryValueEx(key, 'SteamExe')[0]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
        user = winreg.QueryValueEx(key, 'ActiveUser')[0]
    return exe, user


def _steam_user() -> int:
    try:
        return int(_steam_registry()[1] or 0)
    except (OSError, ValueError):
        return 0


def steam_running() -> bool:
    """steam.exe 在运行（tasklist 也看得到以管理员身份运行的）。"""
    if sys.platform != 'win32':
        return False
    import csv
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq steam.exe', '/FO', 'CSV', '/NH'], capture_output=True,
                         text=True, errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    return any(len(row) > 1 and row[0].lower() == 'steam.exe' for row in csv.reader(out.splitlines()))


def steam_ready() -> bool:
    """Steam 在运行。登录后 Steam 通常在注册表 ActiveProcess 记下登录的用户；但实测 Steam 重新打开后它可能一直是 0，
    《蔚蓝》照样能连上 Steam，所以 steam.exe 进程在就算（没登录时游戏启动后会退出，launch 会报告）。"""
    return _steam_user() != 0 or steam_running()


def ensure_steam(wait_s: float = 180, log=print) -> None:
    """Steam 版《蔚蓝》启动时必须连上正在运行的 Steam：没开就静默启动并等它登录（新启动的 Steam 会记下登录的用户）。"""
    if steam_ready():
        return
    try:
        exe = _steam_registry()[0]
    except OSError as exc:
        raise RuntimeError('找不到 Steam：Steam 版《蔚蓝》需要 Steam 在运行') from exc
    log('[instance] Steam 没在运行，正在静默启动并等待登录…')
    _detached([exe, '-silent'])
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _steam_user():
            return
        time.sleep(2)
    raise RuntimeError(f'Steam 在 {wait_s:g} 秒内没有登录完成；登录后再启动 AI 副本')


def apply_title(pid: int, title: str, wait_s: float = 0) -> bool:
    """把进程 pid 的游戏窗口标题改成 title；wait_s > 0 时等窗口出现。返回是否已是这个标题。"""
    from .gamewindow import find_game_window, set_window_title, window_title
    deadline = time.monotonic() + wait_s
    while True:
        hwnd = find_game_window(pid=pid)
        if hwnd is not None:
            if window_title(hwnd) != title:
                set_window_title(hwnd, title)
            if window_title(hwnd) == title:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def launch(game_dir=None, *, background: bool = False, log=print) -> dict:
    """启动副本的游戏（需要时先启动 Steam），窗口出现后改标题。background=True 时立即返回，其余在后台线程里做。"""
    marker = info(game_dir) if game_dir else current()
    if marker is None:
        raise ValueError('现在的游戏位置不是 AI 副本；先创建副本并把游戏位置设为它')
    if sys.platform != 'win32':
        raise RuntimeError('启动 AI 副本只支持 Windows')
    target = Path(marker['dir'])
    running = game_processes(target)
    if running:
        return {'started': False, 'running': running, 'dir': str(target)}

    def work():
        ensure_steam(log=log)
        process = _detached([str(target / 'Celeste.exe')], cwd=str(target))
        deadline = time.monotonic() + 180
        while True:     # 桥接 1.11.0 起模组加载时就改好标题；这里核对，旧桥接时补改
            if process.poll() is not None:
                raise RuntimeError(f'AI 副本的游戏启动后退出了（返回码 {process.returncode}）：可能 Steam 还没登录完成，'
                                   f'登录后再启动；详见 {target / "log.txt"}')
            titled = apply_title(process.pid, marker['window_title'])
            if titled or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        log(f'[instance] AI 副本已启动（进程 {process.pid}）' + ('' if titled else '；窗口标题没能改好，主控接管时会再改'))
        return process.pid

    def background_work():
        try:
            work()
        except (RuntimeError, OSError) as exc:
            log(f'[instance] 启动 AI 副本失败：{exc}')

    if background:
        threading.Thread(target=background_work, name='celeste-instance-launch', daemon=True).start()
        return {'started': True, 'dir': str(target)}
    return {'started': True, 'pid': work(), 'dir': str(target)}


# ---------------------------------------------------------------- 主控接管前的身份核验

def check_identity(hello: dict, marker: dict | None = None) -> dict | None:
    """副本模式下核对桥接端口上的游戏就是副本（模组路径在副本目录里），并把它的窗口标题改好。
    不是副本模式时什么都不做，返回 None。"""
    marker = marker if marker is not None else current()
    if marker is None:
        return None
    runtime = hello.get('runtime_path')
    if not isinstance(runtime, str) or not runtime:
        raise RuntimeError('桥接没有报告模组路径，无法确认连接的是 AI 副本；请更新副本里的桥接。未开始录制或进档。')
    if not inside(runtime, marker['dir']):
        raise RuntimeError(f'桥接端口上的游戏不是 AI 副本（模组路径 {runtime}，副本 {marker["dir"]}）。'
                           '可能是个人的《蔚蓝》也加载了 AI 模组、先占用了端口：请在个人游戏的 Mod 配置里停用 '
                           'CelesteAIRuntime 与 CelesteAIBridge，或先关闭个人游戏再启动副本。未开始录制或进档。')
    pid = hello.get('process_id')
    if sys.platform == 'win32' and type(pid) is int:
        apply_title(pid, marker['window_title'])
    return marker
