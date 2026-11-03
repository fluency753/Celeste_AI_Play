"""Named sections in the single tool-interface template; old IDs remain aliases."""
import re


SECTION_NAMES = ('observe', 'act', 'narrate', 'set_effort', 'stop_run', 'exit_level',
                 'cutscene', 'simulate', 'travel', 'files', 'programs')
PART_NAMES = {f'tool_{name}_zh.md': name for name in SECTION_NAMES}
_MARKER = re.compile(r'^<!-- (/?)tool:([a-z_]+) -->[ \t]*(?:\r?\n|$)', re.MULTILINE)


def parse_interface(text):
    """Return (legacy wrapper, sections), or None for an old unsectioned template.

    Only the wrapper is expanded by the ordinary slot renderer. Section markers
    never enter model input, and capability filtering remains in the assembler.
    """
    markers = list(_MARKER.finditer(text))
    if re.search(r'<!--[ \t]*/?tool:', _MARKER.sub('', text)):
        raise ValueError('interface_zh.md 工具章节标记须独占一行：<!-- tool:名称 -->')
    if not markers:
        return None
    if '{tool_sections}' in text:
        raise ValueError('interface_zh.md 不能同时使用工具章节标记和 {tool_sections}')
    prefix = text[:markers[0].start()]
    sections = {}
    opened = None
    end = markers[0].start()
    for marker in markers:
        closing, name = marker.groups()
        if name not in SECTION_NAMES:
            raise ValueError(f'interface_zh.md 未知工具章节：{name}')
        if closing:
            if opened is None or opened[0] != name:
                raise ValueError(f'interface_zh.md 工具章节闭合不匹配：{name}')
            sections[name] = text[opened[1]:marker.start()]
            opened = None
        else:
            if opened is not None or name in sections:
                raise ValueError(f'interface_zh.md 工具章节嵌套或重复：{name}')
            if text[end:marker.start()].strip():
                raise ValueError('interface_zh.md 公共说明须放在工具章节之前')
            opened = (name, marker.end())
        end = marker.end()
    if opened is not None:
        raise ValueError(f'interface_zh.md 工具章节未闭合：{opened[0]}')
    if text[end:].strip():
        raise ValueError('interface_zh.md 公共说明须放在工具章节之前')
    missing = set(SECTION_NAMES) - sections.keys()
    if missing:
        raise ValueError('interface_zh.md 缺少工具章节：' + '、'.join(sorted(missing)))
    return prefix + '{tool_sections}\n', sections
