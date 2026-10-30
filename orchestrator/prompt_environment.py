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


def render_tool_references(text, cfg):
    """Resolve standalone capability names, preserving paths and explicit prefixes.

    Shared templates (including local overrides) use capability names. Personal
    additions and tool arguments are never passed through this renderer.
    """
    names = (*GAME_NAMES, 'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep')
    alternatives = '|'.join(names)
    groups = r'(?<![A-Za-z0-9_/.-])(?:' + alternatives + r')(?:/(?:' + alternatives + r'))+(?![A-Za-z0-9_/.-])'
    text = re.sub(groups, lambda match: '/'.join(tool_name(cfg, name) for name in match.group().split('/')), text)
    pattern = r'(?<![A-Za-z0-9_/.-])(' + alternatives + r')(?![A-Za-z0-9_/.-])'
    return re.sub(pattern, lambda match: tool_name(cfg, match.group()), text)


def environment_instructions(cfg):
    workspace = workspace_capabilities(cfg)
    if not workspace.workspace_read:
        return '当前只有游戏工具，没有文件或程序工具；按观测与工具说明操作。'
    if getattr(cfg, 'engine', 'claude') == 'codex':
        tools = ('文件用 read/glob/grep，编辑用 apply_patch，程序用原生 Shell；游戏工具为 celeste_ 前缀。'
                 'Shell 中包括 cat/ls 在内的命令均按程序门禁和次数计量；'
                 'read/glob/grep 不计程序次数，暂停或只读聊天也可用。'
                 '旧会话文件工具名以恢复通知中的实际登记为准。'
                 '公开 commentary 可直接作为解说入口；图像通过当前轮附件送达，按帧号和 screenshot/collision 标签核对。')
    else:
        tools = ('文件用 Read/Glob/Grep，编辑用 Write/Edit，程序用 Bash；游戏工具由 celeste MCP 提供。'
                 '文件读取不计程序次数，暂停或只读聊天仍可查阅；图像通过工具的图像内容送达。')
    return ('工作目录 /workspace，文件与程序均在断网容器内。' + tools
            + '必读资料须用专用文件读取工具逐页读取，Shell 读取不计准备进度。'
              '程序及游戏操作仍受当前阶段与权限限制；不另连桥接或其他服务，不启动子智能体。'
              '只公开简短结论，不输出内部推理或图像编码。')


def reference_path(cfg, name):
    """Return a mounted optional reference, or None when it is unavailable."""
    if not workspace_capabilities(cfg).workspace_read:
        return None
    rel = ('help/' if getattr(cfg, 'mode', 'speedrun') == 'explore' else 'explore/help/') + name
    if rel in (getattr(cfg, 'kb_deny', None) or []):
        return None
    if not (cfg.kb_source / rel).is_file():
        return None
    return '/kb/' + rel
