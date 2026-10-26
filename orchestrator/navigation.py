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
        start_room = checkpoint_room(table, piece.chapter, piece.start)
        same_chapter = (self.current.chapter == piece.chapter if self.current else
                        self.previous_sid == CHAPTER_SIDS.get(piece.chapter))
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
