"""Optional bounded current-workspace evidence. No Git/programs are executed."""
import hashlib
import os
from pathlib import Path
import re
import stat

from .session_export_projection import clean_text


def read_regular(path, root, limit):
    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('link/reparse point skipped')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
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


def workspace_snapshot(path):
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError('工作区目录不存在')
    inventory, notes, warnings = [], [], []
    total = 0
    extensions = {'.py', '.cpp', '.c', '.h', '.hpp', '.cs', '.sh', '.md', '.txt', '.json', '.toml'}
    excluded = {'.git', '.venv', '__pycache__', 'node_modules', 'build', 'dist', '.ssh', '.codex', 'agent-home', 'Saves'}
    def unlinked(source):
        try:
            return not source.is_symlink() and not getattr(source.lstat(), 'st_file_attributes', 0) & 0x400
        except OSError as exc:
            warnings.append(f'{source}: {exc}')
            return False
    for directory, dirs, files in os.walk(root, followlinks=False):
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
            if source.suffix.lower() in ('.md', '.txt') and re.search(r'(?i)note|memory|journal', name) and len(notes) < 20 and total < 512 * 1024:
                try:
                    raw, size, truncated = read_regular(source, root, min(128 * 1024, 512 * 1024 - total))
                    total += len(raw)
                    text = clean_text(raw.decode('utf-8-sig', errors='replace'), 128 * 1024)
                    notes.append({'path': relative, 'source_bytes': size, 'captured_bytes': len(raw), 'truncated': truncated,
                                  'captured_sha256': hashlib.sha256(raw).hexdigest(), 'text': text})
                except (OSError, ValueError) as exc:
                    warnings.append(f'{relative}: {exc}')
        if len(inventory) >= 2000:
            break
    git = {'head': None, 'ref': None, 'worktree_status': None,
           'status_unavailable_reason': '未执行工作区 Git、过滤器或外部助手；只读 HEAD/refs，不读取对象数据库'}
    git_dir = root / '.git'
    if git_dir.is_dir() and not git_dir.is_symlink() and not getattr(git_dir.lstat(), 'st_file_attributes', 0) & 0x400:
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
            if re.fullmatch(r'[0-9a-fA-F]{40,64}', head):
                git['head'] = head
        except (OSError, ValueError, UnicodeError) as exc:
            warnings.append(f'Git HEAD: {exc}')
    return {'root': str(root), 'meaning': '导出时当前工作区；不证明游玩当时内容或Agent读取/理解，mtime不用于历史归因',
            'inventory': inventory, 'memory_documents': notes, 'git': git, 'warnings': warnings}
