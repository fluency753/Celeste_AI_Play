"""Runtime-neutral timing events. No prompts, model reasoning or tool payloads."""
from __future__ import annotations

import json
import time


class DecisionTiming:
    def __init__(self, path):
        self.path = path
        self.turn = 0
        self.active = False
        self.tools = {}
        self.baseline = 0.0
        self.context = {}
        self.paused_s = 0.0
        self.pause_started = None

    def clock(self):
        now = time.monotonic()
        return now - self.paused_s - (now - self.pause_started if self.pause_started is not None else 0)

    def pause(self):
        if self.pause_started is None:
            self.pause_started = time.monotonic()

    def resume(self):
        if self.pause_started is not None:
            self.paused_s += time.monotonic() - self.pause_started
            self.pause_started = None

    def emit(self, event, **data):
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(dict(event=event, wall=time.time(), turn=self.turn,
                                      **self.context, **data), ensure_ascii=False) + "\n")

    def begin(self, now, thinking, **context):
        if self.active:
            self.end(now)
        self.turn += 1
        self.active = True
        self.context = context
        self.baseline = thinking
        self.started = now
        self.decision_started = now
        self.decision_thinking = thinking
        self.emit("turn_requested")

    def decision_begin(self, now, thinking):
        if not self.active:
            return 0.0
        wait = max(0, thinking - self.decision_thinking)
        self.emit("decision_requested", decision_wait_s=wait,
                  decision_active_s=max(0, now - self.decision_started))
        return wait

    def decision_end(self, now, thinking, result):
        if not self.active:
            return
        self.emit("decision_completed", success=result is not None,
                  result={k: result.get(k) for k in ("planned", "consumed", "stop")} if result else None)
        self.decision_started, self.decision_thinking = now, thinking

    def acknowledge(self, now, turn_id):
        if self.active:
            self.emit("turn_acknowledged", turn_id=turn_id, request_ack_s=max(0, now - self.started))

    def tool_begin(self, ident, label, game, now, thinking, playing):
        if not self.active:
            return None
        wait = max(0, thinking - self.baseline) if not self.tools else 0.0
        self.tools[ident] = now
        self.emit("tool_requested", tool_id=ident, tool=label, game_tool=game,
                  playing=playing, decision_wait_s=wait)
        return wait if game and playing else None

    def tool_end(self, ident, now, thinking):
        started = self.tools.pop(ident, None)
        if started is not None:
            self.emit("tool_completed", tool_id=ident, tool_active_s=max(0, now - started))
        if not self.tools:
            self.baseline = thinking

    def end(self, now):
        if not self.active:
            return
        self.emit("turn_ended", active_s=max(0, now - self.started), pending_tools=list(self.tools))
        self.active = False
        self.tools.clear()
