"""按段通关：官方章节（A 面）按检查点分段，可以只玩其中几段。

- 段 k：从检查点 k 到检查点 k+1；最后一段到章节结尾（通关）。检查点 0 是章节开头。
  例如 1A 有两个检查点，分成三段：开头 → 渡口、渡口 → 深渊、深渊 → 通关。
- 选中的段按官方顺序排列；同一章里相邻的段连成一"截"（Piece），一截里的段连续游玩，不传送；
  两截之间由主控换过去：前一截在章节结尾结束时照常走完结算画面，否则直接传送到下一截的起点检查点。
- 一截结束于进入它终点检查点的房间（或章节通关），记一条分段成绩。

检查点表来自桥接的 areas 指令（与游戏里选章面板的检查点一致）。configs/checkpoints.json 是同一份数据
（tools/dump_checkpoints.py 从游戏里导出），控制台和主控在不连游戏时用它。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import CHAPTER_SIDS, REPO_ROOT, chapter_name_by_id

CHECKPOINTS_FILE = REPO_ROOT / "configs" / "checkpoints.json"

Segment = tuple[int, int]          # (章节编号, 段号)


@dataclass(frozen=True)
class Piece:
    """连续游玩的一截：章节 chapter 里从检查点 start 开始，进入检查点 end 的房间时结束；end 为 None 时到章节通关。"""
    chapter: int
    start: int = 0
    end: int | None = None

    @staticmethod
    def of(item) -> "Piece":
        """队列里的一项：整章（章节编号）或 Piece。"""
        return item if isinstance(item, Piece) else Piece(int(item))

    def segments(self, table: dict | None = None) -> list[Segment]:
        stop = self.end if self.end is not None else segment_count(table or {}, self.chapter)
        return [(self.chapter, k) for k in range(self.start, max(stop, self.start + 1))]

    def whole(self) -> bool:
        return self.start == 0 and self.end is None


# ---------------------------------------------------------------- 检查点表

def load_table(path: Path = CHECKPOINTS_FILE) -> dict[int, list[dict]]:
    """{章节编号: [{"index", "level", "name", ...}, ...]}（第 0 项是章节开头）；没有文件时为空表。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return table_from_areas(data.get("areas") or [])


def table_from_areas(areas: list[dict]) -> dict[int, list[dict]]:
    """桥接 areas 指令（或 checkpoints.json）的 areas 列表 → 检查点表；只留官方章节。"""
    table = {}
    for area in areas:
        try:
            chapter = int(area["id"])
        except (KeyError, TypeError, ValueError):
            continue
        points = [p for p in area.get("checkpoints") or [] if isinstance(p, dict) and p.get("level") is not None]
        if chapter in CHAPTER_SIDS and points:
            table[chapter] = sorted(points, key=lambda p: int(p.get("index", 0)))
    return table


def segment_count(table: dict, chapter: int) -> int:
    """章节分成几段（检查点数 + 1）；表里没有这一章时当作一段（整章）。"""
    return max(1, len(table.get(chapter) or []))


def checkpoint_room(table: dict, chapter: int, index: int) -> str | None:
    points = table.get(chapter) or []
    return str(points[index]["level"]) if 0 <= index < len(points) else None


def checkpoint_name(table: dict, chapter: int, index: int) -> str:
    points = table.get(chapter) or []
    if 0 <= index < len(points):
        point = points[index]
        return str(point.get("name_zh") or point.get("name") or point.get("level"))
    return "开头" if index == 0 else f"检查点 {index}"


# ---------------------------------------------------------------- 解析与分截

SEGMENT_ITEM = re.compile(r"^(\d+)(?:\.(\d+)(?:-(\d+))?)?$")


def parse_segments(text: str, table: dict) -> list[Segment]:
    """命令行 --segments：逗号分隔，每项是 章节（整章）、章节.段 或 章节.起-止（含两端），
    例如 "1.1-2,2,9.3"。返回按官方顺序排好、去重后的段列表；段号超出范围时报错。"""
    segments: set[Segment] = set()
    for item in (x.strip() for x in str(text).split(",")):
        if not item:
            continue
        match = SEGMENT_ITEM.match(item)
        if not match:
            raise ValueError(f"--segments 的 {item!r} 不对：写成 章节、章节.段 或 章节.起-止，例如 1.1-2")
        chapter = int(match.group(1))
        if chapter not in CHAPTER_SIDS:
            raise ValueError(f"没有编号为 {chapter} 的官方章节")
        count = segment_count(table, chapter)
        if match.group(2) is None:
            first, last = 0, count - 1
        else:
            first = int(match.group(2))
            last = int(match.group(3)) if match.group(3) is not None else first
        if not 0 <= first <= last < count:
            raise ValueError(f"{chapter_name_by_id(chapter)}只有第 0～{count - 1} 段（{item}）")
        segments.update((chapter, k) for k in range(first, last + 1))
    if not segments:
        raise ValueError("--segments 至少要选一段")
    return sorted(segments)


def validate_segments(segments, table: dict) -> list[Segment]:
    """控制台给的 [[章节, 段], ...] → 排好序的段列表；格式或范围不对时报错。"""
    if not isinstance(segments, list) or not segments or len(segments) > 200:
        raise ValueError("请至少选择一段")
    out: set[Segment] = set()
    for item in segments:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or any(type(v) is not int for v in item) or item[0] not in CHAPTER_SIDS):
            raise ValueError("段须写成 [章节编号, 段号]")
        if not 0 <= item[1] < segment_count(table, item[0]):
            raise ValueError(f"{chapter_name_by_id(item[0])}没有第 {item[1]} 段")
        out.add((item[0], item[1]))
    return sorted(out)


def pieces_of(segments: list[Segment], table: dict) -> list[Piece]:
    """把排好序的段连成截：同一章里相邻的段连在一起。"""
    pieces: list[Piece] = []
    run: list[Segment] = []

    def close():
        if run:
            chapter, first, last = run[0][0], run[0][1], run[-1][1]
            end = None if last + 1 >= segment_count(table, chapter) else last + 1
            pieces.append(Piece(chapter, first, end))

    for seg in sorted(set(segments)):
        if run and not (seg[0] == run[-1][0] and seg[1] == run[-1][1] + 1):
            close()
            run = []
        run.append(seg)
    close()
    return pieces


def segments_of(pieces, table: dict) -> list[Segment]:
    out: list[Segment] = []
    for piece in pieces:
        out.extend(Piece.of(piece).segments(table))
    return out


def chapters_of(pieces) -> list[int]:
    """各截所在的章节（按顺序，不重复）。"""
    out: list[int] = []
    for piece in pieces:
        chapter = Piece.of(piece).chapter
        if chapter not in out:
            out.append(chapter)
    return out


def piece_label(piece, table: dict) -> str:
    """给人看的一截：整章写章节名，否则写"章节名 起点 → 终点"。"""
    piece = Piece.of(piece)
    name = chapter_name_by_id(piece.chapter)
    if piece.whole():
        return name
    start = checkpoint_name(table, piece.chapter, piece.start)
    end = "通关" if piece.end is None else checkpoint_name(table, piece.chapter, piece.end)
    return f"{name}（{start} → {end}）"


def split_segments(split: dict, table: dict) -> list[Segment]:
    """一条分段成绩覆盖的段（没有 from/to 的旧成绩是整章）。"""
    chapter = next((i for i, s in CHAPTER_SIDS.items() if s == split.get("chapter")), None)
    if chapter is None:
        return []
    return Piece(chapter, int(split.get("from") or 0), split.get("to")).segments(table)


def fetch_table(port: int, timeout: float = 10) -> dict[int, list[dict]]:
    """从正在运行的游戏取检查点表（桥接 1.0.6 起的 areas 指令）；连不上或桥接太旧时为空表。"""
    try:
        from tools.bridge_client import BridgeClient  # noqa: WPS433
        client = BridgeClient(port=port, timeout=timeout)
    except Exception:  # noqa: BLE001
        return {}
    try:
        if not client.call("hello").get("checkpoints"):
            return {}
        return table_from_areas(client.call("areas").get("areas") or [])
    except Exception:  # noqa: BLE001
        return {}
    finally:
        client.close()


def describe_piece(piece, table: dict) -> str:
    """提示词里的一截：起点与终点（带房间名，智能体据此知道到哪里为止）。"""
    piece = Piece.of(piece)
    name = chapter_name_by_id(piece.chapter)
    if piece.whole():
        return f"{name}（整章，到通关为止）"
    start = ("章节开头" if piece.start == 0 else
             f"检查点“{checkpoint_name(table, piece.chapter, piece.start)}”（房间 {checkpoint_room(table, piece.chapter, piece.start)}）")
    end = ("章节通关" if piece.end is None else
           f"检查点“{checkpoint_name(table, piece.chapter, piece.end)}”（进入房间 {checkpoint_room(table, piece.chapter, piece.end)} 即完成）")
    return f"{name}：从{start}到{end}"


def plan_is_partial(pieces) -> bool:
    return any(not Piece.of(p).whole() for p in pieces or [])


def describe_plan(pieces, table: dict, speedrun: bool = True) -> str:
    """按段通关时提示词里的"本次范围"一节。"""
    items = "；".join(describe_piece(p, table) for p in pieces)
    handover = ("每一截完成后由系统接管：下一截不紧接着时，系统直接把你送到下一截的起点（新的计时从 0 开始）；"
                "最后一截完成后本次运行结束。")
    if speedrun:
        return f"本次要依次完成：{items}。{handover}评价指标是各截计时之和。"
    return f"本次指定范围（依次完成）：{items}。{handover}"
