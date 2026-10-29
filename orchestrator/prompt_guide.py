"""Versioned workspace instructions; building a preview never publishes files."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

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


def _reject_link(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        raise ValueError(f'操作指南路径不能是符号链接或重解析点：{path}')


def _check_chain(path):
    for component in reversed((path, *path.parents)):
        _reject_link(component)


def _verify(directory, documents):
    _check_chain(directory)
    if not directory.is_dir():
        raise ValueError(f'操作指南目录冲突：{directory}')
    if {p.name for p in directory.iterdir()} != set(documents):
        raise ValueError(f'操作指南内容冲突，拒绝覆盖：{directory}')
    for name, expected in documents.items():
        path = directory / name
        _reject_link(path)
        if not path.is_file() or path.read_bytes() != expected.encode('utf-8'):
            raise ValueError(f'操作指南内容冲突，拒绝覆盖：{path}')


def publish_guide(cfg):
    """Publish before starting a model; failures propagate instead of exposing a false path."""
    if not workspace_capabilities(cfg).workspace_read:
        return None
    guide = guide_document(cfg)
    workspace = Path(cfg.workspace_dir).absolute()
    base = workspace / 'game' / 'instructions'
    destination = base / guide['version']
    _check_chain(destination)
    base.mkdir(parents=True, exist_ok=True)
    _check_chain(base)
    if destination.exists():
        _verify(destination, guide['documents'])
        return guide
    temporary = Path(tempfile.mkdtemp(prefix='.publish-', dir=base))
    try:
        for name, content in guide['documents'].items():
            _check_chain(temporary)
            with (temporary / name).open('x', encoding='utf-8', newline='\n') as output:
                output.write(content)
        _check_chain(destination)
        try:
            os.rename(temporary, destination)
        except FileExistsError:
            _verify(destination, guide['documents'])
        _verify(destination, guide['documents'])
    finally:
        if temporary.exists():
            _check_chain(temporary)
            if temporary.parent != base or not temporary.name.startswith('.publish-'):
                raise ValueError('拒绝清理非本次发布目录')
            shutil.rmtree(temporary)
    return guide
