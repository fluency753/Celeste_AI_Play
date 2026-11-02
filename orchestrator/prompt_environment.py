"""Render tool references from the actual executor capability contract."""
import re

from .execution import workspace_capabilities


GAME_NAMES = ('observe', 'act', 'narrate', 'set_effort', 'stop_run', 'exit_level',
              'cutscene', 'simulate', 'travel')


def tool_name(cfg, name):
    if getattr(cfg, 'engine', 'claude') != 'codex':
        return name
    if name in GAME_NAMES:
        return 'celeste_' + name
    return {'Read': 'read', 'Glob': 'glob', 'Grep': 'grep',
            'Write': 'apply_patch', 'Edit': 'apply_patch', 'Bash': '原生 Shell'}.get(name, name)


def render_tool_references(text, cfg, *, legacy=True):
    """Resolve standalone capability names, preserving paths and explicit prefixes.

    Shared templates (including local overrides) use capability names. Personal
    additions and tool arguments are never passed through this renderer.
    """
    names = (*GAME_NAMES, 'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep')
    text = re.sub(r'\{tool:(' + '|'.join(names) + r')\}', lambda m: tool_name(cfg, m[1]), text)
    if not legacy:
        return text
    alternatives = '|'.join(names)
    groups = r'(?<![A-Za-z0-9_/.-])(?:' + alternatives + r')(?:/(?:' + alternatives + r'))+(?![A-Za-z0-9_/.-])'
    pattern = r'(?<![A-Za-z0-9_/.:{-])(' + alternatives + r')(?![A-Za-z0-9_/.-])'
    def prose(piece):
        piece = re.sub(groups, lambda m: '/'.join(tool_name(cfg, name) for name in m[0].split('/')), piece)
        return re.sub(pattern, lambda m: tool_name(cfg, m[0]), piece)
    # Old prose overrides remain compatible; JSON/string values and fenced programs are data.
    protected = r'(```[\s\S]*?```|~~~[\s\S]*?~~~|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
    return ''.join(piece if i % 2 else prose(piece) for i, piece in enumerate(re.split(protected, text)))


def environment_instructions(cfg):
    workspace = workspace_capabilities(cfg)
    if not workspace.workspace_read:
        return '当前只有游戏工具，没有文件或程序工具；按观测与工具说明操作。'
    if getattr(cfg, 'engine', 'claude') == 'codex':
        tools = ('必读计量只认专用 read，Shell 读文件仍按程序计量；暂停或只读聊天仍可查阅文件。'
                 '旧会话工具名以恢复通知为准。图像通过当前轮附件送达，核对帧号与标签。')
    else:
        tools = ('必读计量只认专用 Read，Bash 读文件不计准备进度；暂停或只读聊天仍可查阅文件。'
                 '图像通过工具的图像内容送达。')
    return ('工作目录 /workspace，文件与程序均在断网容器内。' + tools
            + '程序及游戏操作遵守当前阶段和权限，不另连桥接或服务，不启动子智能体；不输出内部推理或图像编码。')


def reference_path(cfg, name):
    """Return a mounted optional reference, or None when it is unavailable."""
    if not workspace_capabilities(cfg).workspace_read:
        return None
    from .kb_access import KB_ROOT
    from . import kb_layout
    rel = kb_layout.TOOL_REFERENCES.get(name, 'shared/tools/'+name)
    if getattr(cfg, '_kb_bound_view', None) is not None:
        manifest = kb_layout.read_manifest(cfg.kb_dir)
        return '/kb/'+rel if manifest and rel in manifest['hashes'] else None
    if kb_layout.enabled(KB_ROOT):
        old = kb_layout.SHARED_ALIASES.get(rel, ())
        aliases = (*old, *('explore/'+path for path in old))
        deny = set(getattr(cfg, 'kb_deny', None) or [])
        if not (KB_ROOT/rel).is_file() or deny.intersection((rel, *aliases)):
            return None
        return '/kb/' + rel
    rel = ('help/' if getattr(cfg, 'mode', 'speedrun') == 'explore' else 'explore/help/') + name
    if rel in (getattr(cfg, 'kb_deny', None) or []):
        return None
    if not (cfg.kb_source / rel).is_file():
        return None
    return '/kb/' + rel
