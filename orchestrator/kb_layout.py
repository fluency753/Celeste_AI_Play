"""One catalog for source ownership, mounted paths, old names and study policy."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from .session_store import rename_directory

VERSION = 1
MANIFEST = '.knowledge.json'
LAYOUT_MARKER = '.knowledge-layout'
CELESTE_SOURCE = 'reference/celeste-source'
LEGACY_CELESTE_SOURCE = CELESTE_SOURCE + '.txt'
MODES = ('explore', 'speedrun')
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
    children: tuple['Document', ...] = ()


def enabled(root):
    return (root/'shared').is_dir() or (root/'speedrun').is_dir()


def canonical_path(name):
    return CELESTE_SOURCE if name == LEGACY_CELESTE_SOURCE else name


def canonical_required(values):
    result = {canonical_path(name): value for name, value in values.items() if name == LEGACY_CELESTE_SOURCE}
    result.update((name, value) for name, value in values.items() if name != LEGACY_CELESTE_SOURCE)
    return result


def local_source(root):
    return root.parent/'local'/'kb'/Path(CELESTE_SOURCE).name


def _relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\\:') or value.startswith('/'):
        raise ValueError('目录集合路径须为正斜杠分隔的相对路径')
    if any(not part or part.startswith('.') or part.endswith(('.', ' ')) for part in value.split('/')):
        raise ValueError('目录集合路径含空白或隐藏路径组件')
    return PurePosixPath(value)


def _collections(root):
    specs = {}
    for manifest in (root/'.collections.json', root.parent/'local'/'kb'/'collections.json'):
        if not manifest.is_file():
            continue
        data = json.loads(manifest.read_text(encoding='utf-8-sig'))
        if not isinstance(data, list):
            raise ValueError(f'{manifest} 须包含目录集合数组')
        for row in data:
            if (not isinstance(row, dict) or set(row) - {'path', 'source', 'modes', 'required'}
                    or not isinstance(row.get('modes'), list) or not row['modes']
                    or any(mode not in MODES for mode in row['modes'])
                    or type(row.get('required', False)) is not bool):
                raise ValueError(f'{manifest} 的目录集合设置无效')
            target, source = _relative(row.get('path')), _relative(row.get('source'))
            if source.parts[:2] == ('local', 'kb') and len(source.parts) > 2:
                base = root.parent/'local'/'kb'
                parts = source.parts[2:]
            elif (len(source.parts) >= 2 and source.parts[0] == 'kb'
                  and source.parts[1] in ('shared', 'speedrun', 'explore', 'reference')):
                base = root
                parts = source.parts[1:]
                allowed = MODES if parts[0] == 'shared' else ('speedrun',) if parts[0] == 'reference' else (parts[0],)
                if any(mode not in allowed for mode in row['modes']):
                    raise ValueError(f'{source} 不适用于指定模式')
            else:
                raise ValueError(f'{source} 须位于 kb/ 或 local/kb/ 的子目录')
            if _linked(base):
                raise ValueError(f'目录集合不能引用链接：{base}')
            folder = base
            for part in parts:
                folder /= part
                if _linked(folder):
                    raise ValueError(f'目录集合不能引用链接：{folder}')
            if not folder.resolve().is_relative_to(root.parent.resolve()) or not folder.resolve().is_relative_to(base.resolve()):
                raise ValueError(f'目录集合不能越出来源目录：{folder}')
            name = target.as_posix()
            if name.casefold() == LEGACY_CELESTE_SOURCE.casefold() or target.parts[0].casefold() == 'speedrun':
                raise ValueError(f'目录集合路径冲突：{name}')
            if any(key != name and _overlap(key, name) for key in specs):
                raise ValueError(f'目录集合路径重叠：{name}')
            specs[name] = (folder, row['modes'], row.get('required', False))
    return specs


def _linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _overlap(left, right):
    left, right = left.casefold(), right.casefold()
    return left == right or left.startswith(right + '/') or right.startswith(left + '/')


def _collection_files(folder):
    root = folder.resolve()
    for current, subdirs, filenames in os.walk(folder, followlinks=False):
        directory = Path(current)
        subdirs[:] = sorted(name for name in subdirs if not name.startswith('.') and not _linked(directory/name))
        for name in sorted(filenames):
            path = directory/name
            if (not name.startswith('.') and not _linked(path) and visible_file(path, folder)
                    and path.resolve().is_relative_to(root)):
                yield path


def collection_exists(root, mode, name):
    spec = _collections(root).get(name)
    return bool(spec and mode in spec[1] and next(_collection_files(spec[0]), None))


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
                if canonical_path(path) != CELESTE_SOURCE and not path.startswith(CELESTE_SOURCE + '/'):
                    docs.append(Document(path, source, path in reference_required))
    for target, (folder, modes, mandatory) in _collections(root).items():
        if mode not in modes or not folder.is_dir():
            continue
        files = tuple(_collection_files(folder))
        if not files:
            continue
        members = set(files)
        previous = {doc.source: doc for doc in docs if doc.source in members and not doc.children}
        children = tuple(previous.get(file) or Document(target + '/' + file.relative_to(folder).as_posix(), file)
                         for file in files)
        if any(not doc.path.startswith(target + '/') for doc in children):
            raise ValueError(f'集合 {target} 须保留现有知识库下的文件路径')
        docs = [doc for doc in docs if doc.source not in previous]
        if any(_overlap(name, target) for doc in docs for name in
               (doc.path, *doc.aliases, *(a for child in doc.children for a in child.aliases))):
            raise ValueError(f'目录集合在 {target} 有路径冲突')
        if any(_overlap(alias, doc.path) for child in children for alias in child.aliases for doc in docs):
            raise ValueError(f'目录集合 {target} 的文件别名有路径冲突')
        if len({doc.path.casefold() for doc in children}) != len(children):
            raise ValueError(f'目录集合在 {target} 有大小写冲突')
        from .kb_access import study_target
        member_names = {doc.path for doc in children}
        children = tuple(replace(doc, required=mandatory and study_target(doc.path, member_names) == doc.path)
                         for doc in children)
        docs.append(Document(target, folder, mandatory, children=children))
    return docs


def lookup(root, mode, path):
    name = canonical_path(path)
    return next((doc for doc in catalog(root, mode) if name == doc.path or name in doc.aliases), None)


def policy_documents(root, mode):
    return {name: doc for row in catalog(root, mode) for doc in (row, *row.children)
            for name in (doc.path, *doc.aliases)}


def edit_path(root, source, overlay, doc=None):
    if not source.is_relative_to(root):
        return source
    canonical = overlay/source.relative_to(root)
    if doc is None:
        doc = next((d for mode in MODES for row in catalog(root, mode) for d in row.children or (row,) if d.source == source), None)
    candidates = [canonical, *(overlay/name for name in doc.old_sources)] if doc else [canonical]
    existing = list(dict.fromkeys(path for path in candidates if path.is_file()))
    if len(existing) > 1:
        raise ValueError(f'知识库新旧路径存在多份修改，请先合并为一份：{source.relative_to(root)}')
    return existing[0] if existing else canonical


def selected(cfg, root):
    from .kb_access import apply_required
    mode = 'explore' if cfg.knowledge_profile == 'explore' else 'speedrun'
    docs = catalog(root, mode)
    files = {}
    required = set()
    aliases = {}
    identities = {}
    groups = {}
    overlay = cfg.kb_overlay_dir
    for row in docs:
        if row.children:
            groups[row.path] = [doc.path for doc in row.children]
        for doc in row.children or (row,):
            source = edit_path(root, doc.source, overlay, doc) if overlay else doc.source
            files[doc.path] = source if source.is_file() else doc.source
            identities[doc.path] = row.path
            if doc.required:
                required.add(doc.path)
            aliases.update((alias, doc.path) for alias in doc.aliases)
    permitted = policy_documents(root, 'speedrun') if cfg.kb_allow else {}
    for name in cfg.kb_allow:
        if name.startswith('speedrun/'):
            doc = permitted.get(name[len('speedrun/'):])
            if doc and not doc.children and doc.source.is_relative_to(root):
                source = edit_path(root, doc.source, overlay, doc) if overlay else doc.source
                files[name] = source if source.is_file() else doc.source
                identities[name] = doc.path
    if cfg.kb_extra_dir:
        if not cfg.kb_extra_dir.is_dir():
            raise RuntimeError(f'找不到本机追加资料的目录：{cfg.kb_extra_dir}')
        for source in cfg.kb_extra_dir.rglob('*'):
            if visible_file(source, cfg.kb_extra_dir):
                name = source.relative_to(cfg.kb_extra_dir).as_posix()
                if any(_overlap(name, group) for group in groups):
                    raise ValueError(f'本机追加资料与知识库目录集合路径冲突：{name}')
                name = aliases.get(name, name)
                files[name] = source
                if '/' not in name and source.suffix == '.md':
                    required.add(name)
    deny = {canonical_path(aliases.get(name, name)) for name in cfg.kb_deny}
    files = {name: source for name, source in files.items() if name not in deny and identities.get(name, name) not in deny}
    overrides = {aliases.get(name, name): enabled for name, enabled in canonical_required(getattr(cfg, 'kb_required', {})).items()}
    groups = {name: visible for name, members in groups.items()
              if (visible := [child for child in members if child in files])}
    for name, members in groups.items():
        if name in overrides:
            overrides.update((member, overrides[name]) for member in members)
    return files, sorted(apply_required(required, overrides, files)), {a: p for a, p in aliases.items() if p in files}, groups


def view_key(cfg, root):
    files, required, aliases, groups = selected(cfg, root)
    identity = [VERSION, cfg.engine, cfg.knowledge_profile, cfg.profile, required, aliases,
                [(name, str(path), path.stat().st_size, path.stat().st_mtime_ns) for name, path in sorted(files.items())]]
    identity += _variant_identity(cfg)
    if groups:
        identity.append(groups)
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def _variant_identity(cfg):
    """模拟器段落的选择（sim_mode.kb_variant）随执行器默认时不进标识，免得已有视图无故换目录。"""
    from .sim_mode import controller_owned, kb_variant
    variant = kb_variant(cfg)
    return [] if variant == ('controller' if controller_owned(cfg) else 'direct') else ['sim:' + variant]


def binding_key(cfg):
    identity = (cfg.engine, cfg.knowledge_profile, cfg.profile, tuple(sorted(cfg.kb_deny)),
            tuple(sorted(cfg.kb_allow)), cfg.kb_extra, cfg.kb_overlay, str(cfg.workspace_dir), *_variant_identity(cfg))
    required = getattr(cfg, 'kb_required', {})
    return (*identity, tuple(sorted(required.items()))) if required else identity


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
    from .prompt_environment import render_tool_references, select_sim_blocks
    from .sim_mode import kb_variant
    files, required, aliases, groups = selected(cfg, root)
    sources = _collections(root)
    external_group_files = {name for group, members in groups.items()
                            if not sources[group][0].is_relative_to(root) for name in members}
    content = {}
    for name, source in files.items():
        data = source.read_bytes()
        if source.suffix == '.md' and name not in external_group_files:
            text = select_sim_blocks(data.decode('utf-8'), kb_variant(cfg))
            data = render_tool_references(text, cfg, legacy=False).encode('utf-8')
        content[name] = data
    for alias, name in aliases.items():
        content[alias] = content[name]
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(content.items())}
    identity = _study_digest(cfg.engine, required, hashes, groups)
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
                                         'engine': cfg.engine, 'hashes': hashes, **({'groups': groups} if groups else {})},
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
    groups = data.get('groups', {})
    if (not isinstance(groups, dict) or any(not isinstance(members, list)
            or any(not isinstance(name, str) or not name.startswith(group + '/') or name not in hashes for name in members)
            for group, members in groups.items())):
        raise ValueError('知识库目录集合清单无效')
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
    actual = _study_digest(data['engine'], data['required'], hashes, data.get('groups', {}))
    if actual != data.get('identity'):
        raise ValueError('知识库视图内容与生成清单不一致，请重新生成')
    return actual


def _study_digest(engine, required, hashes, groups):
    fields = [VERSION, engine, required, hashes]
    if groups:
        fields.append(groups)
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
