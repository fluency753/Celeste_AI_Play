"""主控代为换图（travel 工具，只在计划外地图开关打开时注册）。

智能体自己用暂停菜单回地图、在选章界面里一步步选，地图不冻结，思考的全程都会被录进录像。改为智能体发请求：
主控核对目标在存档里已解锁，在关卡里先用暂停菜单返回地图（与玩家相同，游戏照常保存进度），再从选章地图进入
指定章节 A 面的检查点，走完入场与开场剧情，停在第一帧可操作时交回。

这里只放目标解析、存档进度读取与核对、说明文字；执行在 GameSession.travel 与 tools/enter_save.travel_to。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import CHAPTER_SIDS, chapter_name, chapter_name_by_id

# 终章只有剧情，没有可去的检查点；告别（10）照常按存档的解锁进度核对
EXCLUDED = {8}


@dataclass(frozen=True)
class Destination:
    chapter: int
    checkpoint: int           # 0 = 章节开头
    sid: str
    room: str | None          # 检查点所在房间；开头没有检查点表时为 None
    label: str


@dataclass(frozen=True)
class SaveProgress:
    unlocked_areas: int                       # 存档的 UnlockedAreas：A 面已解锁到第几章
    checkpoints: dict                         # {章节编号: A 面已到过的检查点房间}


def _points(table: dict, chapter: int) -> list[dict]:
    return list(table.get(chapter) or [])


def point_label(point: dict, index: int) -> str:
    name = point.get('name_zh') or point.get('name') or ('开头' if index == 0 else f'检查点 {index}')
    english = point.get('name_en')
    return f'{name}（{english}）' if english and english != name else str(name)


def chapter_numbers() -> str:
    """章节编号与名称的对照（编号是游戏内部的章节编号，与 --chapters 相同；核心、告别的名称里带的是章节序号）。"""
    return '、'.join(f'{i} = {chapter_name_by_id(i)}' for i in sorted(CHAPTER_SIDS) if i not in EXCLUDED)


def chapter_id(value) -> int:
    """章节编号或名称（"核心"、"8 核心"、"Core" 都可）→ 章节编号。"""
    text = str(value).strip()
    if text.lstrip('-').isdigit():
        return int(text)
    bare = text.lower()
    for i, sid in CHAPTER_SIDS.items():
        name = chapter_name(sid)
        plain = name.split(' ', 1)[-1] if name[:1].isdigit() else name
        english = sid.split('/', 1)[-1].split('-', 1)[-1].lower()
        if text in {name, plain} or bare in {english, sid.lower()}:
            return i
    raise ValueError(f'不认识的章节“{text}”；章节编号：' + chapter_numbers())


def resolve(table: dict, chapter, checkpoint=0) -> Destination:
    """chapter：章节编号；checkpoint：序号（0 = 章节开头，1 起依次为各检查点），或检查点的房间名、中文或英文名称。"""
    if isinstance(chapter, bool) or not isinstance(chapter, (int, str)):
        raise ValueError('chapter 须为章节编号：' + chapter_numbers())
    chapter = chapter_id(chapter)
    if chapter not in CHAPTER_SIDS or chapter in EXCLUDED:
        raise ValueError(f'没有可去的章节 {chapter}：travel 只去官方章节的 A 面（终章除外）')
    points = _points(table, chapter)
    if isinstance(checkpoint, bool) or not isinstance(checkpoint, (int, str)):
        raise ValueError('checkpoint 须为检查点序号（0 = 章节开头），或检查点的房间名、名称')
    index = None
    if isinstance(checkpoint, int):
        index = checkpoint
    else:
        text = checkpoint.strip()
        if text.lstrip('-').isdigit() and not any(str(p.get('level')) == text for p in points):
            index = int(text)
        else:
            lowered = text.lower()
            for i, point in enumerate(points):
                names = {str(point.get('level')), str(point.get('name_zh') or ''), str(point.get('name') or '')}
                english = str(point.get('name_en') or '')
                if text in names - {''} or (english and english.lower() == lowered):
                    index = i
                    break
            if index is None and text in {'开头', '开始', 'start', 'Start'}:
                index = 0
            if index is None:
                raise ValueError(f'{chapter_name_by_id(chapter)}没有叫“{text}”的检查点；可选：{describe_points(table, chapter)}')
    count = max(len(points), 1)
    if not 0 <= index < count:
        raise ValueError(f'{chapter_name_by_id(chapter)}的检查点序号只有 0～{count - 1}：{describe_points(table, chapter)}')
    sid = CHAPTER_SIDS[chapter]
    room = str(points[index]['level']) if points else None
    if index == 0:
        label = f'{chapter_name(sid)} A面开头' + (f'（房间 {room}）' if room else '')
    else:
        label = f'{chapter_name(sid)} A面的检查点“{point_label(points[index], index)}”（房间 {room}）'
    return Destination(chapter, index, sid, room, label)


def describe_points(table: dict, chapter: int) -> str:
    points = _points(table, chapter)
    if not points:
        return '0 = 开头'
    return '、'.join(f'{i} = {point_label(p, i)}·房间 {p.get("level")}' for i, p in enumerate(points))


def read_progress(path) -> SaveProgress:
    """读游戏存档文件（XML）里的解锁进度：UnlockedAreas 与各章 A 面到过的检查点（Areas/AreaStats/Modes 第一项）。
    游戏在返回地图、保存退出、通关时写存档；还在关卡里时，本次进入后新到的检查点要等离开时才写进去。"""
    if not path:
        raise ValueError('观测里没有存档文件位置')
    root = ET.parse(Path(path)).getroot()
    try:
        unlocked = int((root.findtext('UnlockedAreas') or '0').strip())
    except ValueError:
        unlocked = 0
    reached: dict[int, frozenset] = {}
    areas = root.find('Areas')
    for area in (list(areas) if areas is not None else []):
        try:
            ident = int(area.get('ID'))
        except (TypeError, ValueError):
            continue
        sid = area.get('SID')
        if ident not in CHAPTER_SIDS or (sid and sid != CHAPTER_SIDS[ident]):
            continue
        modes = area.find('Modes')
        first = list(modes)[0] if modes is not None and len(list(modes)) else None
        found = first.find('Checkpoints') if first is not None else None
        reached[ident] = frozenset((item.text or '').strip() for item in (list(found) if found is not None else [])
                                   if (item.text or '').strip())
    return SaveProgress(unlocked, reached)


def locked_reason(progress: SaveProgress, dest: Destination) -> str | None:
    """目标在存档里还没解锁时的说明；已解锁返回 None。"""
    if dest.chapter > progress.unlocked_areas:
        return f'{chapter_name(dest.sid)}在存档里还没有解锁'
    if dest.checkpoint > 0 and dest.room not in progress.checkpoints.get(dest.chapter, frozenset()):
        return f'存档里还没有到过{dest.label}'
    return None


def available(table: dict, progress: SaveProgress) -> str:
    """存档里可去的目标（报错时附上，方便智能体改正）。"""
    parts = []
    for chapter in sorted(CHAPTER_SIDS):
        if chapter in EXCLUDED or chapter > progress.unlocked_areas:
            continue
        points = _points(table, chapter)
        reached = progress.checkpoints.get(chapter, frozenset())
        names = ['0 开头'] + [f'{i} {p.get("level")}' for i, p in enumerate(points) if i > 0 and str(p.get('level')) in reached]
        parts.append(f'{chapter}（{chapter_name_by_id(chapter)}）：' + '、'.join(names))
    return '；'.join(parts)


def description() -> str:
    """travel 工具说明。"""
    return ('请主控代为换图：离开当前地图，进入另一章（或同一章）A 面的某个检查点。chapter 为章节编号（游戏内部编号，'
            '核心与告别名称里的序号与编号不同）：' + chapter_numbers() + '；也可写章节名称。checkpoint 写整数是检查点序号'
            '（0 = 章节开头，1 起依次为各检查点），写字符串按检查点的房间名或名称匹配（"6" 是房间 6，不是序号 6）。只限存档里已解锁章节的 A 面、已到过的检查点。'
            '主控在关卡里先用暂停菜单返回地图（游戏照常保存进度，与玩家相同），再从选章地图进入目标，走完入场与开场剧情，'
            '停在第一帧可操作时交回并返回新地图的完整观测；换图过程不需要你操作。thought：一两句话说明为什么去，'
            '规则与 act 的 thought 相同，会显示给观众。不要自己用暂停菜单返回地图或在选章界面操作。')
