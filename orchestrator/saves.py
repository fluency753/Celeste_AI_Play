"""Read-only save inventory and explicit selection validation. Never edits game saves."""
import re
import xml.etree.ElementTree as ET

from .gamepaths import celeste_dir


def supports_unicode():
    try:
        manifest = (celeste_dir() / 'Mods/CelesteAIBridge/everest.yaml').read_text(encoding='utf-8-sig')
        version = re.search(r'\bVersion:\s*(\d+)\.(\d+)\.(\d+)', manifest)
        return bool(version and tuple(map(int, version.groups())) >= (0, 12, 1))
    except (OSError, KeyError, TypeError, ValueError):
        return False


def inventory():
    try:
        directory = celeste_dir() / 'Saves'
        if not directory.is_dir():
            raise ValueError(f'{directory} 不存在：先启动一次游戏并建好存档')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('无法读取存档列表：' + str(exc)) from exc
    saves = []
    for path in directory.glob('*.celeste'):
        if not path.stem.isdigit():
            continue
        try:
            root = ET.parse(path).getroot()
            name = root.findtext('Name')
            if name:
                saves.append({'slot': int(path.stem), 'name': name})
        except (OSError, ET.ParseError):
            continue
    return sorted(saves, key=lambda item: item['slot'])


def validate_selection(name, create, chapter, slot=None):
    if not isinstance(name, str) or not name.strip() or type(create) is not bool:
        raise ValueError('请显式选择已有存档，或选择新建并填写存档名')
    name = name.strip()
    if create and (len(name) > 20 or not name.isprintable() or any(ord(c) > 0xffff for c in name)):
        raise ValueError('新存档名须为1～20个可显示字符，支持中文')
    if create and not name.isascii() and not supports_unicode():
        raise ValueError('中文新档名需要安装桥接0.12.1并重启游戏；当前可用英文新档名')
    if create:
        if chapter != 0:
            raise ValueError('新存档必须从序章（0）开始')
        if slot is not None:
            raise ValueError('新建存档由游戏分配空槽位，不能指定已有槽位')
    else:
        matches = [item for item in inventory() if item['name'] == name]
        if slot is not None:
            if type(slot) is not int or slot < 0:
                raise ValueError('存档槽位无效')
            matches = [item for item in matches if item['slot'] == slot]
        if len(matches) != 1:
            raise ValueError('请选择具体存档槽位并刷新列表')
    return name
