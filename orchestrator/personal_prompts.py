"""Local additive instructions, independent of shared prompt template overrides."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from .config import REPO_ROOT


STORE = REPO_ROOT / 'local' / 'personal_prompts.json'
MAX_TEXT_CHARS = 12000
MAX_FILE_BYTES = 512000
_LABELS = {
    'all': '通用（全部执行器与模式）',
    'codex:explore': 'GPT（Codex）· 探索',
    'codex:speedrun': 'GPT（Codex）· 速通',
    'claude:explore': 'Claude 执行器 · 探索',
    'claude:speedrun': 'Claude 执行器 · 速通',
}
_thread_lock = threading.RLock()


def catalog():
    return [{'scope': scope, 'label': label} for scope, label in _LABELS.items()]


def _scope(scope):
    if not isinstance(scope, str) or scope not in _LABELS:
        raise ValueError('未知个人提示词适用范围')
    return scope


def _text(text):
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or '\x00' in text:
        raise ValueError(f'个人追加要求须为文本，最多 {MAX_TEXT_CHARS} 个字符，不能含空字符')
    return text.replace('\r\n', '\n')


def _version(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _record(scope, text):
    return {'scope': scope, 'label': _LABELS[scope], 'text': text, 'version': _version(text)}


def _check_path(path):
    for item in reversed((path, *path.parents)):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError(f'个人提示词路径不能是符号链接或重解析点：{item}')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('个人提示词配置存在重复字段')
        result[key] = value
    return result


def _load(path):
    _check_path(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('个人提示词配置文件过大')
    try:
        document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('个人提示词配置不是有效的 UTF-8 JSON；未覆盖原文件') from exc
    if (not isinstance(document, dict) or set(document) != {'version', 'scopes'}
            or type(document['version']) is not int or document['version'] != 1
            or not isinstance(document['scopes'], dict)):
        raise ValueError('个人提示词配置格式无效；未覆盖原文件')
    return {_scope(scope): _text(text) for scope, text in document['scopes'].items()}


@contextmanager
def _write_lock(path):
    """Serialize separate HUD processes as well as requests within one process."""
    with _thread_lock:
        _check_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + '.lock')
        _check_path(lock_path)
        with lock_path.open('a+b') as stream:
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'\0')
                stream.flush()
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


def read(scope):
    scope = _scope(scope)
    return _record(scope, _load(Path(STORE).absolute()).get(scope, ''))


def save(scope, text, version):
    scope, text = _scope(scope), _text(text)
    path = Path(STORE).absolute()
    with _write_lock(path):
        values = _load(path)
        old = values.get(scope, '')
        if not isinstance(version, str) or version != _version(old):
            raise ValueError('此范围的个人提示词已被修改，请重新读取后保存')
        if old == text:
            return _record(scope, text)
        if text:
            values[scope] = text
        else:
            values.pop(scope, None)
        payload = json.dumps({'version': 1, 'scopes': values}, ensure_ascii=False, indent=2) + '\n'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                    prefix='.personal-prompts-', suffix='.tmp', dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _check_path(path)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return _record(scope, text)


def snapshot(cfg):
    """Read the effective scopes once for a coherent startup text and version."""
    from .modes import mode_of
    specific = _scope(f"{getattr(cfg, 'engine', 'claude')}:{mode_of(cfg)}")
    values = _load(Path(STORE).absolute())
    entries = [_record(scope, values[scope]) for scope in ('all', specific)
               if values.get(scope, '').strip()]
    content = [{'scope': item['scope'], 'text': item['text']} for item in entries]
    version = hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()[:16]
    text = ('# 本机个人追加要求\n\n以下要求补充共享说明；仍须遵守当前用户指令、任务范围、操作权限与实际工具限制。\n\n'
            + '\n\n'.join(f"## {item['label']}\n\n{item['text']}" for item in entries)) if entries else ''
    return {'entries': entries, 'text': text, 'version': version}


def selected(cfg):
    return snapshot(cfg)['entries']


def fingerprint(cfg):
    return snapshot(cfg)['version']


def instructions(cfg):
    return snapshot(cfg)['text']
