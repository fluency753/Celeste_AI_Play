"""Optional bounded workspace evidence."""
import hashlib
import os
from pathlib import Path
import re
import stat

from .session_export_projection import clean_text

DIRECTORY_LIMIT = 2048
REPOSITORY_LIMIT = 8


def read_regular(path, root, limit):
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or getattr(initial, 'st_file_attributes', 0) & 0x400:
        raise ValueError('non-regular file or reparse point skipped')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('not a regular file')
        if os.name == 'nt':
            import ctypes
            import ctypes.wintypes as wt
            import msvcrt
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
            if not 0 < length < len(buffer):
                raise OSError('cannot verify the opened file path')
            actual = Path(buffer.value.removeprefix('\\\\?\\')).resolve()
        elif Path('/proc/self/fd').is_dir():
            actual = Path(f'/proc/self/fd/{descriptor}').resolve()
        else:
            actual = path.resolve()
        if not actual.is_relative_to(root):
            raise ValueError('opened file escapes workspace')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        return raw[:limit], info.st_size, len(raw) > limit
    finally:
        os.close(descriptor)


def workspace_snapshot(path, since_wall=None, until_wall=None):
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError('工作区目录不存在')
    inventory, notes, warnings = [], [], []
    total = 0
    extensions = {'.py', '.cpp', '.c', '.h', '.hpp', '.cs', '.sh', '.md', '.txt', '.json', '.toml'}
    excluded = {'.git', '.venv', '__pycache__', 'node_modules', 'build', 'dist', '.ssh', '.codex', 'agent-home', 'Saves'}

    def capture_note(source):
        nonlocal total
        relative = source.relative_to(root).as_posix()
        try:
            raw, size, truncated = read_regular(source, root, min(128 * 1024, 512 * 1024 - total))
            total += len(raw)
            text = clean_text(raw.decode('utf-8-sig', errors='replace'), 128 * 1024)
            notes.append({'path': relative, 'source_bytes': size, 'captured_bytes': len(raw), 'truncated': truncated,
                          'captured_sha256': hashlib.sha256(raw).hexdigest(), 'text': text})
        except (OSError, ValueError) as exc:
            warnings.append(f'{relative}: {exc}')

    def unlinked(source):
        try:
            return not source.is_symlink() and not getattr(source.lstat(), 'st_file_attributes', 0) & 0x400
        except OSError as exc:
            warnings.append(f'{source}: {exc}')
            return False
    root_note = root / 'notes.md'
    if os.path.lexists(root_note):
        capture_note(root_note)
    repositories = [root]
    repository_limit_warned = False
    for visited, (directory, dirs, files) in enumerate(os.walk(root, followlinks=False)):
        if visited >= DIRECTORY_LIMIT:
            warnings.append(f'目录访问达到{DIRECTORY_LIMIT}项上限，文件和 Git 仓库清单可能不完整')
            break
        current = Path(directory)
        candidate = current / '.git'
        if current != root and os.path.lexists(candidate):
            if not unlinked(candidate) or not candidate.is_dir():
                warnings.append(f'{candidate.relative_to(root).as_posix()}: 跳过 Git 文件指针、链接或重解析点')
            elif len(repositories) < REPOSITORY_LIMIT:
                repositories.append(current)
            elif not repository_limit_warned:
                warnings.append(f'Git 仓库达到{REPOSITORY_LIMIT}项上限，仓库摘要可能不完整')
                repository_limit_warned = True
        dirs[:] = sorted(name for name in dirs if name not in excluded and not name.startswith('.')
                         and unlinked(Path(directory) / name))
        for name in sorted(files):
            source = Path(directory) / name
            if source.suffix.lower() not in extensions or re.search(r'(?i)credential|secret|password|token|\.key$|\.env', name):
                continue
            if not unlinked(source):
                continue
            if len(inventory) >= 2000:
                warnings.append('文件清单达到2000项上限，未继续扫描')
                dirs[:] = []
                break
            try:
                info = source.stat()
            except OSError as exc:
                warnings.append(f'{source}: {exc}')
                continue
            relative = source.relative_to(root).as_posix()
            inventory.append({'path': relative, 'size': info.st_size, 'mtime_wall': info.st_mtime})
            if source != root_note and source.suffix.lower() in ('.md', '.txt') and re.search(r'(?i)note|memory|journal', name) and len(notes) < 20 and total < 512 * 1024:
                capture_note(source)
        if len(inventory) >= 2000:
            break
    snapshots = [{'path': directory.relative_to(root).as_posix(),
                  **_git_evidence(directory, since_wall, until_wall, workspace_root=root)} for directory in repositories]
    return {'root': str(root), 'meaning': '笔记与清单来自导出时工作区；不证明游玩当时内容或Agent读取/理解，mtime不用于历史归因',
            'inventory': inventory, 'memory_documents': notes, 'git': {k: v for k, v in snapshots[0].items() if k != 'path'},
            'git_repositories': snapshots, 'warnings': warnings}


def _git_evidence(root, since_wall, until_wall, *, workspace_root):
    from .session_export_git import git_snapshot

    warnings = []
    git = {'head': None, 'ref': None}
    git_dir = root / '.git'
    try:
        info = git_dir.lstat()
        plain_directory = stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
    except FileNotFoundError:
        plain_directory = False
    except OSError as exc:
        warnings.append(f'Git 目录: {exc}')
        plain_directory = False
    if plain_directory:
        try:
            raw, _, _ = read_regular(git_dir / 'HEAD', root, 4096)
            head = raw.decode('utf-8').strip()
            if head.startswith('ref: '):
                ref = head[5:]
                if re.fullmatch(r'refs/[A-Za-z0-9_./-]+', ref) and '..' not in Path(ref).parts:
                    git['ref'] = ref
                    candidate = git_dir / ref
                    if candidate.exists():
                        raw, _, _ = read_regular(candidate, root, 4096)
                        head = raw.decode().strip()
                    elif (git_dir / 'packed-refs').exists():
                        raw, _, truncated = read_regular(git_dir / 'packed-refs', root, 1024 * 1024)
                        head = next((line.split(' ', 1)[0] for line in raw.decode().splitlines() if line.endswith(' ' + ref)), '')
                        if truncated:
                            warnings.append('packed-refs 已按1MiB截断')
            if re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', head):
                git['head'] = head.lower()
        except (OSError, ValueError, UnicodeError) as exc:
            warnings.append(f'Git HEAD: {exc}')
    git.update(git_snapshot(root, git['head'], since_wall, until_wall, workspace_root=workspace_root))
    git['warnings'] = warnings + git['warnings']
    return git
