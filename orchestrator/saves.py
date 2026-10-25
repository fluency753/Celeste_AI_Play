"""Read-only save inventory and explicit selection validation. Never edits game saves."""
import xml.etree.ElementTree as ET

from .gamepaths import celeste_dir


def supports_unicode():
    # 能写入 Unicode 不代表当前游戏字体能显示。保留清单 API 的能力字段。
    return False


def validate_new_name(name):
    if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 20
            or not name.isascii() or not name.isprintable()):
        raise ValueError('新存档名须为1～20个可打印 ASCII 字符；中文名请使用拼音，避免游戏字体缺字')
    return name.strip()


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
    if create:
        name = validate_new_name(name)
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
