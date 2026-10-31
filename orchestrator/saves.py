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


def _flag(element, name):
    return str(element.get(name) if element is not None else '').strip().lower() == 'true'


def side_progress(path):
    """读存档 XML 里 B／C 面的解锁事实（只读）：{'cheat': CheatMode, 'cassette': {章节编号: 拿到了磁带},
    'b_hearts': {章节编号: B 面拿到了心}}。章节编号按 AreaStats 的 ID（官方章节 0～10）。"""
    from .config import CHAPTER_SIDS
    root = ET.parse(path).getroot()
    cheat = (root.findtext('CheatMode') or '').strip().lower() == 'true'
    cassette, b_hearts = {}, {}
    areas = root.find('Areas')
    for area in (list(areas) if areas is not None else []):
        try:
            ident = int(area.get('ID'))
        except (TypeError, ValueError):
            continue
        sid = area.get('SID')
        if ident not in CHAPTER_SIDS or (sid and sid != CHAPTER_SIDS[ident]):
            continue
        cassette[ident] = _flag(area, 'Cassette')
        modes = area.find('Modes')
        rows = list(modes) if modes is not None else []
        b_hearts[ident] = len(rows) > 1 and _flag(rows[1], 'HeartGem')
    return {'cheat': cheat, 'cassette': cassette, 'b_hearts': b_hearts}


def side_locked_reason(progress, chapter, side):
    """计划的 B／C 面在存档里还没解锁时的中文说明；已解锁（或是 A 面）返回 None。规则与游戏选章面板相同
    （OuiChapterPanel.Reset，调试模式不计）：
    - B 面：该章 AreaStats.Cassette（拿到了这一章的磁带），或存档开了作弊模式（CheatMode）。
    - C 面：SaveData.UnlockedModes ≥ 3。它在 CheatMode 时为 3；否则取官方关卡集的 LevelSetStats.UnlockedModes：
      有 B 面且不是插曲的每一章（原版是第 1～7 章与核心，内部编号 9）的 B 面都满足"地图里有心时拿到心
      （HeartGem），没有心时通关（Completed）"才是 3。原版这 8 章的 B 面都有心，所以按这 8 章 B 面的 HeartGem 判断。
      同时该章要有 C 面（原版也是这 8 章）。"""
    from .config import SIDE_CHAPTERS, chapter_name_by_id
    if side == 'A':
        return None
    name = chapter_name_by_id(chapter)
    if chapter not in SIDE_CHAPTERS:
        return f'{name}没有 {side} 面：只有第 1～7 章与核心有 B／C 面'
    if progress.get('cheat'):
        return None
    if side == 'B':
        if not progress['cassette'].get(chapter):
            return (f'{name}的 B 面在这个存档里还没有解锁：需要先在这一章的 A 面拿到磁带'
                    '（游戏选章面板的规则：该章 Cassette 为真，或存档开了作弊模式）')
        return None
    missing = [chapter_name_by_id(c) for c in SIDE_CHAPTERS if not progress['b_hearts'].get(c)]
    if missing:
        return (f'{name}的 C 面在这个存档里还没有解锁：游戏要求第 1～7 章与核心这 8 章的 B 面都拿到心'
                f'（UnlockedModes ≥ 3，或存档开了作弊模式）；还差：{"、".join(missing)}的 B 面')
    return None


def save_file(slot):
    """槽位对应的存档文件（游戏目录 Saves/N.celeste）。"""
    return celeste_dir() / 'Saves' / f'{int(slot)}.celeste'


def validate_sides(pieces, *, create, slot=None, name=None):
    """开跑前核对计划里的 B／C 面在所选存档里已经解锁；只读存档文件，不改任何东西。没有 B／C 面时什么都不读。
    新建存档不能计划 B／C 面。已有存档按槽位读（没给槽位时按名称找唯一的那个）。"""
    from .segments import Piece
    sided = [Piece.of(p) for p in pieces or [] if Piece.of(p).side != 'A']
    if not sided:
        return
    if create:
        raise ValueError('新建存档不能计划 B／C 面：B 面要先在该章 A 面拿到磁带，C 面要先拿到 8 个 B 面的心')
    if slot is None:
        matches = [item for item in inventory() if item['name'] == name]
        if len(matches) != 1:
            raise ValueError('计划里有 B／C 面：请指定存档槽位（--save-slot），主控按存档核对这些面是否已解锁')
        slot = matches[0]['slot']
    path = save_file(slot)
    try:
        progress = side_progress(path)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f'读不到存档 {path.name} 里的解锁进度（{exc}），不能核对 B／C 面是否已解锁') from exc
    for piece in sided:
        reason = side_locked_reason(progress, piece.chapter, piece.side)
        if reason:
            raise ValueError(reason)


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
