"""Versioned workspace instructions; building a preview never publishes files."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from .prompting import prompt_parts
from .execution import workspace_capabilities


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def guide_document(cfg):
    """Return the complete rendered guide and its immutable publication identity."""
    sections = []
    occurrences = {}
    documents = {}
    for part in prompt_parts(cfg):
        source = part.source or part.label
        occurrences[source] = occurrences.get(source, 0) + 1
        content = part.sep + part.text
        digest = _digest(content)
        sections.append({'id': f'{source}:{occurrences[source]}', 'label': part.label,
                         'source': part.source, 'sha256': digest, 'file': digest + '.md',
                         'text': content})
        documents[digest + '.md'] = content
    manifest = {'format': 'celeste-prompt-guide-v1', 'sections': [
        {key: value for key, value in section.items() if key != 'text'} for section in sections]}
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    version = _digest(manifest_text)
    prefix = f'/workspace/game/instructions/{version}'
    index = ['# 试玩操作指南', '', f'版本：{version}', '',
             '当前任务、配置、实时工具结果与用户最新指令优先于本指南中的启动快照。',
             '首次接手按顺序阅读各节；同一会话已读且哈希未变的章节无需重读。',
             '版本变化时比较各节的标识与哈希，只补读新增或变化的章节；压缩后按遗忘的内容查阅。',
             '下列文件名相对于本索引所在目录；文件名去掉 .md 即该节的内容哈希。', '']
    for section in sections:
        section['path'] = prefix + '/' + section['file']
        index.append(f"- {section['id']} | {section['label']} | {section['file']}")
    documents['manifest.json'] = manifest_text
    documents['index.md'] = '\n'.join(index) + '\n'
    return {'version': version, 'index_path': prefix + '/index.md',
            'sections': sections, 'documents': documents}


def _is_link(path):
    """lstat only: a symbolic link or Windows reparse point (junction) is never followed."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return bool(stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _reject_link(path):
    if _is_link(path):
        raise ValueError(f'操作指南路径不能是符号链接或重解析点：{path}')


def _check_chain(path):
    for component in reversed((path, *path.parents)):
        _reject_link(component)


def _same_bytes(path, expected):
    """True only for a regular, non-link file with exactly the published bytes."""
    if _is_link(path):
        return False
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
             | getattr(os, 'O_BINARY', 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected):
            return False
        return stream.read(len(expected) + 1) == expected


def _intact(directory, documents):
    """Every file the controller published is present and unchanged.

    The directory is agent-writable: entries the controller never published (for
    example the .claude/ folder Claude Code creates in each Bash working directory)
    are ignored instead of blocking startup.
    """
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return False
    if _is_link(directory) or not stat.S_ISDIR(metadata.st_mode):
        return False
    return all(_same_bytes(directory / name, text.encode('utf-8')) for name, text in documents.items())


def _set_aside(base, destination):
    """Rename a tampered version entry within the guide base; links are renamed, never followed or deleted."""
    _check_chain(base)
    stale = base / f'.stale-{destination.name[:16]}-{uuid.uuid4().hex[:12]}'
    os.rename(destination, stale)
    return stale


def publish_guide(cfg):
    """Publish before starting a model; failures propagate instead of exposing a false path.

    A version directory whose published files were changed is moved aside and
    published again, so an agent edit never prevents a later start or reconnect.
    """
    if not workspace_capabilities(cfg).workspace_read:
        return None
    guide = guide_document(cfg)
    documents = guide['documents']
    workspace = Path(cfg.workspace_dir).absolute()
    base = workspace / 'game' / 'instructions'
    destination = base / guide['version']
    _check_chain(base)
    base.mkdir(parents=True, exist_ok=True)
    _check_chain(base)
    if _intact(destination, documents):
        return guide
    for _ in range(3):
        if os.path.lexists(destination):
            stale = _set_aside(base, destination)
            print(f"[guide] 操作指南 {guide['version'][:12]} 的已发布文件被改动或缺失，"
                  f"原目录已移到 {stale.name}，重新发布", flush=True)
        temporary = Path(tempfile.mkdtemp(prefix='.publish-', dir=base))
        try:
            for name, content in documents.items():
                _check_chain(temporary)
                with (temporary / name).open('x', encoding='utf-8', newline='\n') as output:
                    output.write(content)
            _check_chain(base)
            try:
                os.rename(temporary, destination)
            except OSError:
                if not os.path.lexists(destination):
                    raise
                # Another publisher won the rename; its result is verified below.
        finally:
            if temporary.exists():
                _check_chain(temporary)
                if temporary.parent != base or not temporary.name.startswith('.publish-'):
                    raise ValueError('拒绝清理非本次发布目录')
                shutil.rmtree(temporary)
        if _intact(destination, documents):
            return guide
    raise ValueError(f'操作指南发布后仍与内容不一致：{destination}')


def startup_context(cfg, *, previous=None):
    """Capture local additions once, alongside the exact published shared guide.

    previous is this runner's last startup context. On a reconnect an unreadable
    personal prompt file keeps the additions the session already received instead
    of ending the run; a first start still fails before the model starts.
    """
    from .personal_prompts import snapshot
    try:
        personal = snapshot(cfg)
    except (OSError, ValueError) as exc:
        if not isinstance(previous, dict) or 'personal' not in previous:
            raise
        personal = previous['personal']
        print(f'[guide] 本机个人追加要求读取失败，沿用本次运行上次送达的版本：{exc}', flush=True)
    guide = publish_guide(cfg)
    return {**(guide or {}), 'personal': personal}
