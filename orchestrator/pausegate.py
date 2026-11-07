"""Claude 执行器开预置模拟器（/sim）时的规划轮次记账（--limit-planning）。

只记账、不封锁：本章每进入一个没来过的房间按房间大小存入轮次，在游戏里死亡补轮次；一条调用了模拟器的消息
（同一条消息里并行的调用算一次）结束后由 playback.py 扣一轮。剩余轮次写在观测头部和工作区的 game/rounds.json
（/sim 的接口据此在输出末尾提示）。工作区文件与程序始终可用。Codex 执行器的主控限额预测用 planning_budget.py 的硬预算。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .planning_budget import room_rounds


class PauseGate:
    def __init__(self, metrics, enabled: bool = True, log=print, rounds_per_room: int = 3, death_bonus: int = 1,
                 started: bool = False, state_path: Path | None = None, room_scale: float = 2.0):
        self.metrics = metrics
        self.enabled = enabled
        self.log = log
        self.rounds_per_room = rounds_per_room
        self.death_bonus = death_bonus
        self.room_scale = room_scale          # 房间的长、高每变成这么多倍，存入的轮次乘这么多
        self._started = started               # 游戏工具真正执行过（准备阶段结束；续跑时一开始就是）
        self.bank = 0
        self._chapter: str | None = None
        self._visited: set[str] = set()
        self.rounds_used = 0
        self.last_deposit = 0                 # 最近进入的新房间存入了几轮
        self.state_path = state_path          # 工作区里的 game/rounds.json：智能体随时可读的剩余轮次
        if not enabled:
            self._publish()                   # 不记账时也写一次（limited=false），免得工作区里留着上次运行的轮次

    def _publish(self) -> None:
        self.metrics.update(plan_rounds_left=self.bank if self.enabled else None)
        if self.state_path is None:
            return
        state = {"limited": self.enabled and self._started, "left": self.bank, "used": self.rounds_used,
                 "this_room_deposit": self.last_deposit, "per_screen": self.rounds_per_room,
                 "room_scale": self.room_scale, "death_bonus": self.death_bonus, "sim_only": True,
                 "chapter": self._chapter, "rooms_seen": len(self._visited), "updated": time.time()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        """游戏工具第一次真正执行（server.py 的 begin）：准备阶段结束。"""
        if not self._started:
            self._started = True
            self._publish()

    def on_room(self, chapter: str | None, room: str | None, bounds=None) -> bool:
        """每次观测之后调用：进入本章没来过的房间就按房间大小存入轮次。返回是否存入。"""
        if not self.enabled or not room:
            return False
        if chapter != self._chapter:
            self._chapter = chapter
            self._visited = set()
            self.bank = 0
        if room in self._visited:
            return False
        self._visited.add(room)
        self.last_deposit = room_rounds(self.rounds_per_room, bounds, self.room_scale)
        self.bank += self.last_deposit
        self._publish()
        return True

    def on_death(self, count: int = 1) -> None:
        if self.enabled and count > 0:
            self.bank += self.death_bonus * count
            self._publish()

    def charge(self, label: str = "") -> None:
        """一条调用了模拟器的消息结束了：扣一轮（playback.py 调用）。"""
        if not (self.enabled and self._started):
            return
        self.bank = max(0, self.bank - 1)
        self.rounds_used += 1
        self.metrics.add(plan_rounds_used=1)
        self._publish()
        self.log(f"[gate] 用掉一轮模拟器规划（本章剩余 {self.bank} 轮）" + (f"：{label}" if label else ""))

    def status_line(self) -> str:
        if not self.enabled:
            return ""
        return (f"规划轮次：记账剩余 {self.bank} 轮（不限制工作区；这个房间存入了 {self.last_deposit} 轮：按房间大小，一屏大小的房间 "
                f"{self.rounds_per_room} 轮，长、高每加倍轮次翻倍，至少 {self.rounds_per_room} 轮；在游戏里死亡 +{self.death_bonus}；没用完的留给本章后面的房间；"
                f"只有调用模拟器的消息占一轮，同一条消息里并行的几次调用算一轮，其他工具不占轮次）")
