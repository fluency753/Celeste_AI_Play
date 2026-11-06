"""Bounded Git evidence from an isolated copy of the required metadata."""
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

from .session_export_projection import clean_text
from .session_export_workspace import read_regular


MIB = 1024 * 1024
ROW_LIMIT = 200
PATH_LIMIT = 500


def _plain_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Git 元数据目录含链接或重解析点')


def _copy_objects(source, target):
    objects = source / 'objects'
    _plain_directory(objects)
    remaining = 64 * MIB
    count = 0
    for folder in objects.iterdir():
        if folder.name != 'pack' and not re.fullmatch(r'[0-9a-f]{2}', folder.name):
            continue
        _plain_directory(folder)
        destination = target / 'objects' / folder.name
        destination.mkdir()
        pattern = r'pack-(?:[0-9a-f]{40}|[0-9a-f]{64})\.(?:idx|pack)' if folder.name == 'pack' else r'(?:[0-9a-f]{38}|[0-9a-f]{62})'
        for entry in folder.iterdir():
            if not re.fullmatch(pattern, entry.name):
                continue
            size = entry.lstat().st_size
            if count >= 8192 or size > remaining:
                raise ValueError('Git 对象快照超出 64 MiB 或 8192 文件的上限')
            raw, _, truncated = read_regular(entry, source, size)
            if truncated:
                raise ValueError('Git 对象在采样时变大，请重试导出')
            (destination / entry.name).write_bytes(raw)
            remaining -= len(raw)
            count += 1


def _in_window(wall, since, until):
    return (since is None or wall >= since) and (until is None or wall < until)


def _reflog(source, head, since, until):
    _plain_directory(source / 'logs')
    raw, _, truncated = read_regular(source / 'logs' / 'HEAD', source, 2 * MIB)
    if truncated or not raw.endswith(b'\n'):
        raise ValueError('HEAD reflog 超出 2 MiB、为空或含不完整尾行')
    events = []
    hash_pattern = rb'[0-9a-f]{%d}' % len(head)
    pattern = rb'(' + hash_pattern + rb') (' + hash_pattern + rb') [^\t]*> ([0-9]{1,12}) [+-][0-9]{4}\t'
    for line in raw.splitlines():
        match = re.match(pattern, line)
        if not match:
            raise ValueError('HEAD reflog 存在不可识别记录')
        old, new, wall = match.groups()
        events.append({'old_head': old.decode(), 'new_head': new.decode(), 'wall': int(wall)})
    if events[-1]['new_head'] != head:
        raise ValueError('HEAD reflog 与采集到的 HEAD 不一致')
    window = [event for event in events if _in_window(event['wall'], since, until)]
    base = end = None
    continuous = all(left['wall'] <= right['wall'] and left['new_head'] == right['old_head']
                     for left, right in zip(events, events[1:]))
    if continuous:
        if since is not None:
            base = next((event['new_head'] for event in reversed(events) if event['wall'] < since), None)
        end = head if until is None else next((event['new_head'] for event in reversed(events) if event['wall'] < until), None)
    return window[:ROW_LIMIT], len(window) > ROW_LIMIT, base, end, continuous


def _paths(output, *, status=False):
    values = output.decode('utf-8', errors='replace').split('\0')[:-1]
    if status:
        pairs = [(entry[:2], entry[3:]) for entry in values]
    else:
        pairs = list(zip(values[::2], values[1::2]))
    rows = [{'status': code, 'path': clean_text(path, 1024)} for code, path in pairs[:PATH_LIMIT]]
    return rows, len(pairs) > PATH_LIMIT


def git_snapshot(root, head, since, until, *, workspace_root=None):
    boundary = Path(workspace_root if workspace_root is not None else root).resolve()
    snapshot = {'since_wall': since, 'until_wall': until, 'commits': None, 'commits_truncated': False,
                'head_events': None, 'head_events_truncated': False, 'window_start_head': None, 'window_end_head': None,
                'window_changes': None, 'window_changes_truncated': False, 'worktree_status': None,
                'worktree_status_wall': None, 'worktree_status_truncated': False, 'status_unavailable_reason': None,
                'warnings': []}
    warnings = snapshot['warnings']
    if head is None:
        snapshot['status_unavailable_reason'] = '未读取到独立 Git 目录中的有效 HEAD'
        return snapshot

    source = root / '.git'
    try:
        events, truncated, base, end, continuous = _reflog(source, head, since, until)
        snapshot.update(head_events=events, head_events_truncated=truncated, window_start_head=base, window_end_head=end)
        if not continuous:
            warnings.append('HEAD reflog 时间或引用链不连续，无法核实窗口端点')
        elif since is None:
            warnings.append('未指定时间窗起点，未计算端点间净提交路径变化')
        elif base is None or end is None:
            warnings.append('缺少窗口端点之前的 HEAD 记录，无法计算净提交路径变化')
        elif base == end:
            snapshot['window_changes'] = []
    except (OSError, ValueError) as exc:
        warnings.append(f'HEAD reflog 不可用：{exc}')

    try:
        executable = shutil.which('git.exe' if os.name == 'nt' else 'git')
        if not executable or Path(executable).resolve().is_relative_to(boundary):
            raise ValueError('未找到工作区之外的 Git 可执行文件')
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0',
                   GIT_NO_LAZY_FETCH='1', GIT_NO_REPLACE_OBJECTS='1', GIT_ALLOW_PROTOCOL='', GIT_TERMINAL_PROMPT='0')
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root.is_relative_to(boundary):
            raise ValueError('Git 临时目录必须位于源工作区之外')
        with tempfile.TemporaryDirectory(prefix='session-export-git-', dir=temporary_root) as scratch:
            private = Path(scratch).resolve()
            if private.is_relative_to(boundary):
                raise ValueError('Git 临时目录必须位于源工作区之外')
            git_dir = private / 'git'
            (git_dir / 'objects').mkdir(parents=True)
            (git_dir / 'refs').mkdir()
            (git_dir / 'HEAD').write_text(head + '\n', encoding='ascii')
            config = '[core]\n\trepositoryformatversion = 0\n' if len(head) == 40 else (
                '[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n')
            (git_dir / 'config').write_text(config, encoding='ascii')
            _copy_objects(source, git_dir)

            def run(*args):
                with tempfile.TemporaryFile(dir=private) as stdout:
                    process = subprocess.run(
                        [str(Path(executable).resolve()), '--no-pager', f'--git-dir={git_dir}', f'--work-tree={root}',
                         '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                         '-c', f'core.attributesFile={os.devnull}', '-c', f'core.excludesFile={os.devnull}', *args],
                        cwd=private, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL,
                        timeout=8, check=False)
                    if process.returncode:
                        raise ValueError(f'Git {args[0]} 退出码 {process.returncode}')
                    stdout.seek(0)
                    output = stdout.read(2 * MIB + 1)
                    if len(output) > 2 * MIB:
                        raise ValueError(f'Git {args[0]} 输出超过 2 MiB')
                    return output

            try:
                bounds = []
                if since is not None:
                    bounds.append(f'--since-as-filter=@{math.ceil(since)}')
                if until is not None:
                    bounds.append(f'--until=@{math.ceil(until) - 1}')
                raw = run('log', f'--max-count={ROW_LIMIT + 1}', '--format=%H%x00%ct%x00%s', '-z', *bounds, head, '--')
                fields = raw.decode('utf-8', errors='replace').split('\0')[:-1]
                commits = [{'commit': commit, 'wall': int(wall), 'subject': clean_text(subject, 240)}
                           for commit, wall, subject in zip(fields[::3], fields[1::3], fields[2::3])
                           if _in_window(int(wall), since, until)]
                snapshot.update(commits=commits[:ROW_LIMIT], commits_truncated=len(commits) > ROW_LIMIT)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                warnings.append(f'HEAD 可达提交不可用：{exc}')

            base, end = snapshot['window_start_head'], snapshot['window_end_head']
            if base and end and base != end:
                try:
                    changes, truncated = _paths(run('diff', '--name-status', '-z', '--no-renames', '--no-ext-diff',
                                                     '--no-textconv', '--ignore-submodules=all', base, end, '--'))
                    snapshot.update(window_changes=changes, window_changes_truncated=truncated)
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    warnings.append(f'净提交路径变化不可用：{exc}')

            try:
                index, _, truncated = read_regular(source / 'index', source, 16 * MIB)
                if truncated:
                    raise ValueError('Git index 超出 16 MiB 上限')
                (git_dir / 'index').write_bytes(index)
                exclude = source / 'info' / 'exclude'
                if os.path.lexists(exclude):
                    _plain_directory(exclude.parent)
                    raw, _, truncated = read_regular(exclude, source, MIB)
                    if truncated:
                        raise ValueError('Git info/exclude 超出 1 MiB 上限')
                    (git_dir / 'info').mkdir()
                    (git_dir / 'info' / 'exclude').write_bytes(raw)
                status, truncated = _paths(run('status', '--porcelain=v1', '-z', '--no-renames', '--untracked-files=all',
                                                '--ignore-submodules=all'), status=True)
                snapshot.update(worktree_status=status, worktree_status_truncated=truncated, worktree_status_wall=time.time())
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                snapshot['status_unavailable_reason'] = f'无法采集当前 Git 状态：{exc}'
    except (OSError, ValueError) as exc:
        snapshot['status_unavailable_reason'] = f'无法建立隔离 Git 快照：{exc}'
        warnings.append(snapshot['status_unavailable_reason'])
    return snapshot
