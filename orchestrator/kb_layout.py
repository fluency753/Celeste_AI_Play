"""One catalog for source ownership, mounted paths, old names and study policy."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .session_store import rename_directory

VERSION = 1
MANIFEST = '.knowledge.json'
LAYOUT_MARKER = '.knowledge-layout'
TOOL_REFERENCES = {'observation.md': 'shared/tools/observe.md', 'actions.md': 'shared/tools/act.md',
                   'story.md': 'shared/tools/cutscene.md', 'workspace.md': 'shared/tools/files.md',
                   'programs.md': 'shared/tools/programs.md'}


def visible_file(path, base):
    return path.is_file() and not any(part.startswith('.') for part in path.relative_to(base).parts) and path.suffix != '.zip'
SHARED_ALIASES = {
    'shared/data.md': ('data.md',),
    'shared/tools.md': ('tools.md',),
    'shared/tools/observe.md': ('help/observation.md',),
    'shared/tools/act.md': ('help/actions.md',),
    'shared/tools/cutscene.md': ('help/story.md',),
    'shared/tools/files.md': ('help/workspace.md',),
    'shared/tools/programs.md': ('help/programs.md',),
    'shared/tools/observe/files.md': ('game_data.md',),
    'shared/tools/observe/collision_matrix.md': ('collision_matrix.md',),
}


@dataclass(frozen=True)
class Document:
    path: str
    source: Path
    required: bool = False
    aliases: tuple[str, ...] = ()
    old_sources: tuple[str, ...] = ()


def enabled(root):
    return (root/'shared').is_dir() or (root/'speedrun').is_dir()


def catalog(root, mode):
    from .kb_study import required_files
    for name in ('shared/data.md', 'shared/tools.md', mode+'/README.md'):
        if not (root/name).is_file():
            raise ValueError(f'知识库源码不完整：缺少 {name}')
    docs = []
    for source in sorted((root/'shared').rglob('*.md')):
        if not visible_file(source, root/'shared'):
            continue
        path = source.relative_to(root).as_posix()
        old = SHARED_ALIASES.get(path, ())
        aliases = tuple(dict.fromkeys((*old, *('explore/'+name for name in old))))
        old_sources = tuple('explore/'+name for name in old) if path not in (
            'shared/tools/observe/files.md', 'shared/tools/observe/collision_matrix.md') else old
        docs.append(Document(path, source, path in ('shared/data.md', 'shared/tools.md'), aliases, old_sources))
    base = root/mode
    required = set(required_files(base))
    for source in sorted(base.rglob('*')):
        if not visible_file(source, base):
            continue
        path = source.relative_to(base).as_posix()
        old = path if mode == 'speedrun' else 'explore/'+path
        docs.append(Document(path, source, path in required, (), (old,)))
    # Generated community originals are local data; keep their established location.
    if mode == 'speedrun':
        reference_required = set(required_files(root))
        for source in sorted((root/'reference').rglob('*')):
            if visible_file(source, root/'reference'):
                path = source.relative_to(root).as_posix()
                docs.append(Document(path, source, path in reference_required))
    return docs


def lookup(root, mode, path):
    return next((doc for doc in catalog(root, mode) if path == doc.path or path in doc.aliases), None)


def edit_path(root, source, overlay):
    canonical = overlay/source.relative_to(root)
    doc = next((d for mode in ('explore', 'speedrun') for d in catalog(root, mode) if d.source == source), None)
    candidates = [canonical, *(overlay/name for name in doc.old_sources)] if doc else [canonical]
    existing = list(dict.fromkeys(path for path in candidates if path.is_file()))
    if len(existing) > 1:
        raise ValueError(f'知识库新旧路径存在多份修改，请先合并为一份：{source.relative_to(root)}')
    return existing[0] if existing else canonical


def selected(cfg, root):
    mode = 'explore' if cfg.knowledge_profile == 'explore' else 'speedrun'
    docs = catalog(root, mode)
    files = {}
    required = set()
    aliases = {}
    identities = {}
    overlay = cfg.kb_overlay_dir
    for doc in docs:
        source = edit_path(root, doc.source, overlay) if overlay else doc.source
        files[doc.path] = source if source.is_file() else doc.source
        identities[doc.path] = doc.path
        if doc.required:
            required.add(doc.path)
        aliases.update((alias, doc.path) for alias in doc.aliases)
    for name in cfg.kb_allow:
        if name.startswith('speedrun/'):
            doc = lookup(root, 'speedrun', name[len('speedrun/'):])
            if doc:
                source = edit_path(root, doc.source, overlay) if overlay else doc.source
                files[name] = source if source.is_file() else doc.source
                identities[name] = doc.path
    if cfg.kb_extra_dir:
        if not cfg.kb_extra_dir.is_dir():
            raise RuntimeError(f'找不到本机追加资料的目录：{cfg.kb_extra_dir}')
        for source in cfg.kb_extra_dir.rglob('*'):
            if visible_file(source, cfg.kb_extra_dir):
                name = source.relative_to(cfg.kb_extra_dir).as_posix()
                name = aliases.get(name, name)
                files[name] = source
                if '/' not in name and source.suffix == '.md':
                    required.add(name)
    deny = {aliases.get(name, name) for name in cfg.kb_deny}
    files = {name: source for name, source in files.items() if name not in deny and identities.get(name, name) not in deny}
    return files, sorted(required & files.keys()), {a: p for a, p in aliases.items() if p in files}


def view_key(cfg, root):
    files, required, aliases = selected(cfg, root)
    identity = [VERSION, cfg.engine, cfg.knowledge_profile, cfg.profile, required, aliases,
                [(name, str(path), path.stat().st_size, path.stat().st_mtime_ns) for name, path in sorted(files.items())]]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def binding_key(cfg):
    return (cfg.engine, cfg.knowledge_profile, cfg.profile, tuple(sorted(cfg.kb_deny)),
            tuple(sorted(cfg.kb_allow)), cfg.kb_extra, cfg.kb_overlay, str(cfg.workspace_dir))


def build(cfg, root, view):
    if view.exists():
        if study_identity(view) is None:
            raise ValueError('知识库快照不完整，请重新生成到新目录')
        return view
    view.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.knowledge-', dir=view.parent))
    try:
        _write_view(cfg, root, staging)
        try:
            rename_directory(staging, view)
        except OSError:
            if not os.path.lexists(view):
                raise
            if study_identity(view) is None:
                raise ValueError('并发发布的知识库快照不完整，请重新生成到新目录')
        return view
    finally:
        if staging.exists():
            assert staging.resolve().parent == view.resolve().parent
            shutil.rmtree(staging)


def _write_view(cfg, root, view):
    from .prompt_environment import render_tool_references
    files, required, aliases = selected(cfg, root)
    content = {}
    for name, source in files.items():
        data = source.read_bytes()
        if source.suffix == '.md':
            data = render_tool_references(data.decode('utf-8'), cfg, legacy=False).encode('utf-8')
        content[name] = data
    for alias, name in aliases.items():
        content[alias] = content[name]
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(content.items())}
    identity = hashlib.sha256(json.dumps([VERSION, cfg.engine, required, hashes], sort_keys=True).encode()).hexdigest()
    view.mkdir(parents=True, exist_ok=True)
    (view/LAYOUT_MARKER).write_text(str(VERSION), encoding='ascii')
    for name, data in content.items():
        path = view/name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != data:
            path.write_bytes(data)
    for path in view.rglob('*'):
        if path.is_file() and path.name not in (MANIFEST, LAYOUT_MARKER) and path.relative_to(view).as_posix() not in content:
            path.unlink()
    (view/MANIFEST).write_text(json.dumps({'version': VERSION, 'required': required, 'identity': identity,
                                         'engine': cfg.engine, 'hashes': hashes},
                                        ensure_ascii=False, indent=2), encoding='utf-8')


def read_manifest(directory):
    path = directory/MANIFEST
    if not path.is_file():
        if (directory/LAYOUT_MARKER).exists():
            raise ValueError('知识库生成清单缺失，请重新生成视图')
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    required, hashes = data.get('required'), data.get('hashes')
    if (data.get('version') != VERSION or not isinstance(required, list) or not isinstance(hashes, dict)
            or data.get('engine') not in ('codex', 'claude') or not isinstance(data.get('identity'), str)):
        raise ValueError('知识库生成清单格式或版本不符，请重新生成视图')
    for name in [*required, *hashes]:
        if (not isinstance(name, str) or Path(name).is_absolute() or Path(name).drive or name.startswith(('/', '\\'))
                or '..' in Path(name).parts or not (directory/name).is_file()):
            raise ValueError('知识库生成清单含无效或缺失文件')
    if not set(required) <= hashes.keys():
        raise ValueError('知识库必读清单缺少文件摘要')
    return data


def study_identity(directory):
    data = read_manifest(directory)
    if data is None:
        return None
    actual_files = {path.relative_to(directory).as_posix() for path in directory.rglob('*')
                    if path.is_file() and path.relative_to(directory).as_posix() not in (MANIFEST, LAYOUT_MARKER)}
    if actual_files != data['hashes'].keys():
        raise ValueError('知识库视图文件集合与生成清单不一致，请重新生成')
    hashes = {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in data['hashes']}
    actual = hashlib.sha256(json.dumps([VERSION, data['engine'], data['required'], hashes], sort_keys=True).encode()).hexdigest()
    if actual != data.get('identity'):
        raise ValueError('知识库视图内容与生成清单不一致，请重新生成')
    return actual
