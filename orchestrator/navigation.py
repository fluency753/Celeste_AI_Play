"""Chapter-plan ownership: a pending target stays queued until its entry is observed.

This state has no bridge, metrics or filesystem side effects. Both automatic menu
navigation and explicit agent inputs confirm entry through the same transition.
"""
from dataclasses import dataclass, field

from .config import CHAPTER_SIDS
from .segments import Piece, checkpoint_room


@dataclass
class ChapterNavigation:
    current: Piece | None = None
    queue: list = field(default_factory=list)
    segment: int = 0
    switching: bool = False
    previous_sid: str | None = None
    left_previous: bool = False

    @classmethod
    def start(cls, plan, *, pending=False, resume_segment=None, previous_sid=None):
        plan = list(plan)
        current = Piece.of(plan[0]) if plan and not pending else None
        segment = max(current.start, resume_segment or 0) if current else 0
        return cls(current, plan if pending else plan[1:], segment, pending, previous_sid)

    @property
    def target(self):
        """The same target governs input guards and entry confirmation."""
        return Piece.of(self.queue[0]) if self.switching and self.queue else self.current

    def complete_current(self):
        if not self.queue:
            return False
        self.switching = True
        self.left_previous = False
        return True

    def note_scene(self, obs):
        if self.switching and obs.get('scene') != 'Level':
            self.left_previous = True

    def confirm_entry(self, obs, table, *, require_departure=False):
        """Atomically commit the queued target only after a matching live Level.

        A failed macro or a result screen retaining level metadata cannot consume
        the queue. Same-chapter restarts by the agent must first leave the old Level.
        """
        self.note_scene(obs)
        if not self.switching or not self.queue:
            return None
        piece = self.target
        level = obs.get('level') or {}
        if obs.get('scene') != 'Level' or level.get('completed') or level.get('sid') != CHAPTER_SIDS.get(piece.chapter):
            return None
        if piece.side != 'A' and (level.get('side') or 'A') != piece.side:
            return None     # B／C 面的计划段只认同一章的同一面（A 面的计划段由调用方按是否区分计划内外判断）
        # 检查点表只有 A 面：B／C 面整章从开头进入，不核对起点房间
        start_room = checkpoint_room(table, piece.chapter, piece.start) if piece.side == 'A' else None
        # 同一张地图（章节与面都相同）的重新进入要先离开原关卡；4A 之后的 4B 是另一张地图
        same_chapter = ((self.current.chapter, self.current.side) == (piece.chapter, piece.side) if self.current else
                        self.previous_sid == CHAPTER_SIDS.get(piece.chapter) and piece.side == 'A')
        if start_room is not None:
            if str(level.get('room')) != str(start_room):
                return None
        elif piece.start != 0 or (require_departure and same_chapter):
            return None
        if require_departure and same_chapter and piece.start == 0 and not self.left_previous:
            return None
        self.queue.pop(0)
        self.current, self.segment = piece, piece.start
        self.switching = False
        self.previous_sid = level['sid']
        self.left_previous = False
        return piece

    def replace_future(self, current, queue):
        if self.switching:
            raise ValueError('正在换到下一截，稍后再改')
        self.current, self.queue = current, list(queue)
