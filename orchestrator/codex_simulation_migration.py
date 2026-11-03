"""Stopped-thread rollout maintenance; executed with isolated Python in its container.

Only a fixed-width session metadata line changes. SQLite and history are never
rewritten; layouts with a separate dynamic-tool cache are rejected.
"""
from contextlib import closing, contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile

JOURNALS = 'simulation-schema-migrations'
MAX_HEADER = 4 * 1024 * 1024


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _safe_path(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('会话维护路径不允许符号链接')


def active_app_servers():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            continue
        if b'app-server' in args or any(arg.endswith(b'/codex-app-server.py') for arg in args):
            found.append(int(entry.name))
    return found


def _sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.journal-')
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def writer_lock(home, ident):
    import fcntl  # The actual Codex Linux writer uses this same flock namespace.
    directory = home / 'thread-writer-locks'
    _safe_path(directory)
    directory.mkdir(exist_ok=True)
    path = directory / (ident + '.lock')
    _safe_path(path)
    with path.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('原会话仍有写入者，未迁移；先正常停止该会话宿主') from error
        try:
            yield
        finally:
            # Never unlink a lock another writer may already have opened.
            fcntl.flock(lock, fcntl.LOCK_UN)


def _no_tool_cache(home, ident):
    for path in home.glob('state_*.sqlite'):
        _safe_path(path)
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='thread_dynamic_tools'").fetchone()
            if exists and db.execute('SELECT 1 FROM thread_dynamic_tools WHERE thread_id=? LIMIT 1', (ident,)).fetchone():
                raise ValueError('该会话还有独立工具缓存，不支持本次仅 rollout 的定长迁移；未修改')


def _header(path):
    with path.open('rb') as source:
        line = source.readline(MAX_HEADER + 1)
    if not line.endswith(b'\n') or len(line) > MAX_HEADER:
        raise ValueError('会话首行不完整或超出维护上限')
    return line


def _tail_digest(path, offset, count=None):
    result = hashlib.sha256()
    with path.open('rb') as source:
        source.seek(offset)
        while count is None or count:
            block = source.read(1024 * 1024 if count is None else min(count, 1024 * 1024))
            if not block:
                if count:
                    raise ValueError('原会话历史被截断，未覆盖')
                break
            result.update(block)
            if count is not None:
                count -= len(block)
    return result.hexdigest()


def _replace_header(path, expected, replacement):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.simulate-schema-')
    temporary = Path(name)
    try:
        with path.open('rb') as source, os.fdopen(fd, 'wb') as output:
            if source.readline(MAX_HEADER + 1) != expected:
                raise ValueError('会话元数据在维护期间变化，未覆盖')
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            output.write(replacement)
            shutil.copyfileobj(source, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if _header(path) != expected:
            raise ValueError('会话元数据在替换前变化，未覆盖')
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(home, ident, request_id_schema, patch_metadata, *, apply=False):
    home = Path(home)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
        raise ValueError('会话编号无效')
    _safe_path(home)
    paths = list((home / 'sessions').rglob('*' + ident + '*.jsonl'))
    if len(paths) != 1:
        raise ValueError('没有找到唯一的未归档容器会话，未修改')
    path = paths[0]
    _safe_path(path)
    if not path.is_file():
        raise ValueError('会话记录不是普通文件')
    journal = home / JOURNALS / ident / 'journal.json'
    _safe_path(journal)
    line = _header(path)
    updated = patch_metadata(line, ident, request_id_schema)
    report = {'session_id': ident, 'path': str(path), 'status': 'current' if line == updated else 'outdated'}
    if not apply:
        if journal.exists() and json.loads(journal.read_text()).get('state') != 'complete':
            report['status'] = 'pending'
        return report
    if active_app_servers():
        raise RuntimeError('该容器仍有 Codex App Server，未迁移；请先正常停止会话宿主')
    with writer_lock(home, ident):
        _no_tool_cache(home, ident)
        line = _header(path)
        updated = patch_metadata(line, ident, request_id_schema)
        saved = json.loads(journal.read_text()) if journal.exists() else None
        if saved and saved.get('state') != 'complete':
            if saved['session_id'] != ident or saved['path'] != str(path):
                raise ValueError('维护日志的会话身份不匹配，未覆盖')
            updated = base64.b64decode(saved['new_header'], validate=True)
            if digest(line) not in (saved['old_header_hash'], digest(updated)):
                raise ValueError('会话首行与维护日志不匹配，未覆盖')
            if (len(line) != saved['header_bytes'] or len(updated) != len(line)
                    or _tail_digest(path, len(line), saved['history_bytes']) != saved['history_hash']):
                raise ValueError('原会话历史与维护备份不一致，未覆盖')
            if patch_metadata(updated, ident, request_id_schema) != updated:
                raise ValueError('未完成迁移的目标 schema 与当前要求不同，未覆盖')
        elif line == updated:
            return {**report, 'status': 'current'}
        else:
            if saved:
                raise ValueError('完成迁移后的工具登记又发生变化，未覆盖')
            journal.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            backup = journal.parent / 'original.jsonl'
            _safe_path(backup)
            fd, name = tempfile.mkstemp(dir=journal.parent, prefix='.backup-')
            temporary = Path(name)
            try:
                with path.open('rb') as source, os.fdopen(fd, 'wb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(backup)
                _sync_directory(backup.parent)
            finally:
                temporary.unlink(missing_ok=True)
            if (_header(backup) != line or _header(path) != line or backup.stat().st_size != path.stat().st_size
                    or _tail_digest(backup, len(line)) != _tail_digest(path, len(line))):
                raise ValueError('会话备份校验失败，未覆盖')
            saved = {'state': 'prepared', 'session_id': ident, 'path': str(path), 'backup': str(backup),
                     'old_header_hash': digest(line), 'new_header': base64.b64encode(updated).decode(),
                     'header_bytes': len(line), 'history_bytes': backup.stat().st_size - len(line),
                     'history_hash': _tail_digest(backup, len(line))}
            _json(journal, saved)
        current_tail = _tail_digest(path, len(line))
        current_size = path.stat().st_size
        if line != updated:
            _replace_header(path, line, updated)
        if (_header(path) != updated or path.stat().st_size != current_size
                or _tail_digest(path, len(updated)) != current_tail):
            raise RuntimeError('定长迁移校验未完成；保持暂停，使用同一维护命令重试')
        saved['state'] = 'complete'
        _json(journal, saved)
        return {**report, 'status': 'updated', 'backup': saved['backup'], 'journal': str(journal),
                'header_bytes': len(line), 'history_unchanged': True, 'history_hash': current_tail}


async def migrate_run(directory, output, *, apply=False):
    """Host entry: never creates a container, connects to the game, or starts a model."""
    from contextlib import nullcontext
    from .config import REPO_ROOT
    from .codex_session_environment import configuration, tool_specs
    from .codex_container import _run, ensure_runtime
    from .container import container_name, docker, slug
    from .game_process import controller_lease

    directory = Path(directory).resolve()
    if (REPO_ROOT / 'runs').resolve() not in directory.parents:
        raise ValueError('只能维护本项目 runs 下的会话')
    state = json.loads((directory / 'run_state.json').read_text(encoding='utf-8'))
    if state.get('engine') != 'codex' or state.get('session_storage') not in (None, 'container'):
        raise ValueError('本入口只维护容器 Codex 会话，不访问宿主 CODEX_HOME')
    ident = state.get('session_id')
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
        raise ValueError('运行没有有效会话编号')
    cfg = configuration(state)
    name = container_name(cfg.agent + ('-explore' if cfg.knowledge_profile == 'explore' else ''))
    with controller_lease() if apply else nullcontext():
        inspected = docker('inspect', '--format', '{{json .State.Running}}|{{json .Config.Labels}}|{{json .Mounts}}', name)
        running, labels, mounts = inspected.stdout.strip().split('|', 2)
        labels = json.loads(labels)
        mounts = json.loads(mounts)
        if running != 'true' or labels.get('celeste-ai-play.workspace_tag') != cfg.workspace_dir.name:
            raise ValueError('既有容器未运行或工作区归属不一致；未创建或重建任何容器')
        home_mounts = [row for row in mounts if row.get('Destination') == '/home/agent/.codex']
        if (len(home_mounts) != 1 or home_mounts[0].get('Type') != 'volume'
                or home_mounts[0].get('Name') != 'celeste-codex-' + slug(cfg.workspace_dir.name)):
            raise ValueError('既有 Codex 会话卷归属不一致，未维护')
        ensure_runtime(name, require_login=False)
        output = Path(output)
        scratch = output / 'descriptor'
        scratch.mkdir(parents=True, exist_ok=True)
        tools = await tool_specs(cfg, scratch)
        tool = next(item for item in tools if item['name'] == 'celeste_simulate')
        schema = tool['inputSchema']['properties']['request_id']
        patch_source = Path(__file__).with_name('codex_simulation_schema.py').read_text(encoding='utf-8')
        payload = {'session_id': ident, 'schema': schema, 'apply': apply,
                   'patch_source': patch_source, 'maintenance_source': Path(__file__).read_text(encoding='utf-8')}
        bootstrap = """import json,sys
from pathlib import Path
data=json.load(sys.stdin)
patch={};exec(compile(data['patch_source'],'<trusted-schema-patch>','exec'),patch)
maintenance={};exec(compile(data['maintenance_source'],'<trusted-schema-maintenance>','exec'),maintenance)
result=maintenance['migrate'](Path('/home/agent/.codex'),data['session_id'],data['schema'],patch['patch_metadata'],apply=data['apply'])
print(json.dumps(result,ensure_ascii=False))
"""
        result = _run(name, 'python3', '-I', '-c', bootstrap, input=json.dumps(payload), timeout=180)
        if result.returncode:
            raise RuntimeError('容器模拟接口维护未完成，保持暂停：' + result.stderr.strip()[-1200:])
        report = json.loads(result.stdout)
        if report.get('session_id') != ident:
            raise RuntimeError('维护返回的会话身份不一致，未启动模型')
        return {**report, 'container': name, 'run': str(directory)}
