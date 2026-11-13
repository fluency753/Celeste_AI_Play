"""Versioned workspace instructions; building a preview never publishes files."""
from bisect import bisect_right
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from .prompting import TASK_SECTION_LABELS, prompt_parts
from .execution import workspace_capabilities
from .config import REPO_ROOT
from .session_store import rename_directory


GUIDE_READ_ROOT = REPO_ROOT / 'local' / 'guide-reading'


VERSION_CHARS = 12


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def guide_document(cfg, *, include_task=True):
    """Return the complete rendered guide and its immutable publication identity."""
    sections = []
    occurrences = {}
    # Reading ledgers predate task classification. Include all generated task
    # identities so changing whole-chapter/segment form does not revoke history.
    omitted = {label + ':1' for label in TASK_SECTION_LABELS.values()} if not include_task else set()
    for part in prompt_parts(cfg):
        source = part.source or part.label
        occurrences[source] = occurrences.get(source, 0) + 1
        section_id = f'{source}:{occurrences[source]}'
        if part.is_task and not include_task:
            omitted.add(section_id)
            continue
        content = part.sep + part.text
        digest = _digest(content)
        sections.append({'id': section_id, 'label': part.label,
                         'source': part.source, 'sha256': digest, 'text': content})
    body = ''.join(section['text'] for section in sections)
    starts, position = [], 0
    for line in body.splitlines(keepends=True):
        starts.append(position)
        position += len(line)
    position = 0
    for section in sections:
        content = section['text']
        first = bisect_right(starts, position + len(content) - len(content.lstrip()))
        last = bisect_right(starts, position + len(content.rstrip()) - 1)
        section.update(offset=first, limit=last - first + 1 if content.strip() else 0)
        position += len(content)
    manifest = {'format': 'celeste-prompt-guide-v2', 'sections': [
        {key: value for key, value in section.items() if key != 'text'} for section in sections]}
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    # 目录名取清单哈希的前 VERSION_CHARS 位：足以区分版本，提示词里不出现 64 位长串；完整哈希留在 manifest.json
    version = _digest(manifest_text)[:VERSION_CHARS]
    prefix = f'/workspace/game/instructions/{version}'
    path = prefix + '/guide.md'
    index = ['# 试玩操作指南索引', '', '正文：同目录的 guide.md',
             '当前任务、配置、实时工具结果与用户最新指令优先于本指南中的启动快照。',
             '首次读正文，续跑按主控给出的范围补读。遗忘时可用本索引定位。', '',
             '各节依次列出标识、名称、offset 和 limit（读 guide.md 时用）。']
    for section in sections:
        index.append(f"- {section['id']} | {section['label']} | {section['offset']} | {section['limit']}")
    documents = {'guide.md': body, 'manifest.json': manifest_text, 'index.md': '\n'.join(index) + '\n'}
    return {'version': version, 'path': path, 'index_path': prefix + '/index.md', 'include_task': include_task,
            'omitted_task_sections': sorted(omitted),
            'sections': sections, 'documents': documents, 'total_lines': len(starts)}


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
    rename_directory(destination, stale)
    return stale


def publish_guide(cfg, *, include_task=True):
    """Publish before starting a model; failures propagate instead of exposing a false path.

    A version directory whose published files were changed is moved aside and
    published again, so an agent edit never prevents a later start or reconnect.
    """
    if not workspace_capabilities(cfg).workspace_read:
        return None
    guide = guide_document(cfg, include_task=include_task)
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
                rename_directory(temporary, destination)
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


class GuideReading:
    """Controller-owned evidence of guide sections delivered to one native session."""

    def __init__(self, cfg, guide, session_id=None):
        self.cfg = cfg
        self.guide = guide
        self.sections = [s for s in guide.get('sections', ()) if s['limit']]
        self.session_id = session_id
        self.lines = set()
        self.delivered = {}
        if session_id and guide.get('path'):
            try:
                saved = json.loads(self.state_path.read_text(encoding='utf-8'))
                if isinstance(saved, dict):
                    self.delivered = saved
            except (OSError, ValueError):
                pass  # No reliable baseline: ask for the full guide once.

    @property
    def state_path(self):
        key = json.dumps([self.cfg.engine, self.cfg.provider, str(self.cfg.workspace_dir), self.session_id])
        return GUIDE_READ_ROOT / (_digest(key) + '.json')

    def read_plan(self):
        from .prompt_environment import tool_name

        guide = self.guide
        path = guide['path']
        reader = tool_name(self.cfg, 'Read')
        prefix = f'工作区操作指南：{path}\n'
        suffix = ('压缩或遗忘后可查同目录的 index.md 定位相关内容。'
                  '仅聊天、准备资料或取名时按需查阅。指南不替代准备阶段的必读清单。')
        if not self.delivered:
            return prefix + f'开始正式试玩前，先用 {reader} 一次读取正文；如结果截断，从返回的下一行继续。\n' + suffix
        pending = [s for s in self.sections if self.delivered.get(s['id']) != s['sha256']]
        if pending:
            ranges = '\n'.join(f"- offset={s['offset']}，limit={s['limit']}（{s['label']}）" for s in pending)
            prefix += (f'开始正式试玩前，用 {reader} 补读以下范围。每次调用的 file_path 都是上面的 guide.md，括号内是小节名称。'
                       '范围内的行已读齐时，忽略指向范围外的翻页提示。\n'
                       + ranges + '\n')
        else:
            prefix += '主控已确认当前各节均已送达，无需重读。\n'
        removed = sorted(self.removed_sections())
        if removed:
            prefix += '当前指南已移除以下章节，旧内容不再适用：' + '、'.join(removed) + '。\n'
        return prefix + suffix

    def removed_sections(self):
        return self.delivered.keys() - {s['id'] for s in self.sections} - set(self.guide.get('omitted_task_sections', ()))

    def confirm_scope(self):
        """Retire removed sections only after a turn has seen the current plan."""
        if removed := self.removed_sections():
            for key in removed:
                self.delivered.pop(key)
            self._save()

    def compacted(self):
        """上下文压缩后，读过的正文不保证还在上下文里：清掉送达记录。智能体按提醒重读后会重新记上；
        在那之前重接会话，阅读安排会要求整篇重读，而不是“无需重读”。"""
        self.lines = set()
        if self.delivered:
            self.delivered = {}
            if self.session_id:
                self._save()

    def note_result(self, info):
        guide = self.guide
        if not self.session_id or not guide.get('path') or not isinstance(info, dict) or info.get('filePath') != guide['path']:
            return
        first, count, total = (info.get(key) for key in ('startLine', 'numLines', 'totalLines'))
        if (any(type(value) is not int for value in (first, count, total)) or total != guide['total_lines']
                or first < 1 or count < 1 or first + count - 1 > total):
            return
        published = self.cfg.workspace_dir / 'game' / 'instructions' / guide['version'] / 'guide.md'
        try:
            _check_chain(published.parent)
            if not _same_bytes(published, guide['documents']['guide.md'].encode('utf-8')):
                return
        except (OSError, ValueError):
            return
        self.lines.update(range(first, first + count))
        complete = {s['id']: s['sha256'] for s in self.sections if self.delivered.get(s['id']) != s['sha256']
                    and self.lines.issuperset(range(s['offset'], s['offset'] + s['limit']))}
        if not complete:
            return
        self.delivered.update(complete)
        self._save()

    def _save(self):
        path = self.state_path
        temporary = path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self.delivered, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        except OSError as exc:
            print(f'[guide] 无法保存指南阅读进度，下次可能需要重读：{exc}', flush=True)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def startup_context(cfg, *, previous=None, include_task=True):
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
    guide = publish_guide(cfg, include_task=include_task)
    return {**(guide or {}), 'personal': personal, 'include_task': include_task}
