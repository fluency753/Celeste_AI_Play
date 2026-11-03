"""Game-side planning accounting. Workspace tools always retain their capability.

Offline simulator messages can still be counted by playback. This counter does
not revoke file/Shell access. Online metered prediction uses its own wallet and
running-game guard. Non-workspace game tools retain their planning policy.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from .execution import WORKSPACE_TOOLS

SCREEN_W, SCREEN_H = 320, 180     # 一屏：1A 第一个房间的大小，房间轮次的基准


def room_rounds(base: int, bounds, scale: float = 2.0) -> int:
    """一个房间存入的规划轮次：一屏大小的房间 base 轮，长、高每变成 scale 倍轮次乘 scale，向上取整；比一屏小的房间也给 base 轮。"""
    try:
        w, h = float(bounds[2]), float(bounds[3])
    except (TypeError, IndexError, ValueError):
        return base
    if w <= 0 or h <= 0:
        return base
    k = math.log2(scale) if scale > 0 else 1.0
    return max(base, math.ceil(base * (w / SCREEN_W) ** k * (h / SCREEN_H) ** k - 1e-9))

EXEMPT = {"mcp__celeste__stop_run", "mcp__celeste__act", "mcp__celeste__set_effort", "mcp__celeste__narrate",
          "mcp__celeste__travel"}


class PauseGate:
    def __init__(self, metrics, enabled: bool = True, log=print, rounds_per_room: int = 3, death_bonus: int = 1,
                 started: bool = False, sim_only: bool = False, state_path: Path | None = None,
                 room_scale: float = 2.0):
        self.metrics = metrics
        self.enabled = enabled
        self.log = log
        self.rounds_per_room = rounds_per_room
        self.death_bonus = death_bonus
        self._msg_of: dict[str, str] = {}     # tool_use 编号 → 所在消息的编号（来自输出流）
        self._round_msg: str | None = None    # 正在进行的一轮（消息编号）
        self._started = started               # 游戏工具真正执行过（准备阶段结束；续跑时一开始就是）
        self.bank = 0
        self._chapter: str | None = None
        self._visited: set[str] = set()
        self.rounds_used = 0
        self.sim_only = sim_only              # 只有调用模拟器的消息占轮次（由 playback.py 在一轮结束后扣）
        self.room_scale = room_scale          # 房间的长、高每变成这么多倍，存入的轮次乘这么多
        self.last_deposit = 0                 # 最近进入的新房间存入了几轮
        self.state_path = state_path          # 工作区里的 game/rounds.json：智能体随时可读的剩余轮次（/sim 也据此在输出末尾提示）
        if not enabled:
            self._publish()                   # 不限制时也写一次（limited=false），免得工作区里留着上次运行的轮次

    # ---- 记账 ----

    def _publish(self) -> None:
        self.metrics.update(plan_rounds_left=self.bank if self.enabled else None)
        if self.state_path is None:
            return
        state = {"limited": self.enabled and self._started, "left": self.bank, "used": self.rounds_used,
                 "this_room_deposit": self.last_deposit, "per_screen": self.rounds_per_room,
                 "room_scale": self.room_scale, "death_bonus": self.death_bonus, "sim_only": self.sim_only,
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
        if not (self.enabled and self.sim_only and self._started):
            return
        self.bank = max(0, self.bank - 1)
        self.rounds_used += 1
        self.metrics.add(plan_rounds_used=1)
        self._publish()
        self.log(f"[gate] 用掉一轮模拟器规划（本章剩余 {self.bank} 轮）" + (f"：{label}" if label else ""))

    def status_line(self) -> str:
        if not self.enabled:
            return ""
        if self.sim_only:
            return (f"规划轮次：记账剩余 {self.bank} 轮（不限制工作区；这个房间存入了 {self.last_deposit} 轮：按房间大小，一屏大小的房间 "
                    f"{self.rounds_per_room} 轮，长、高每加倍轮次翻倍，至少 {self.rounds_per_room} 轮；在游戏里死亡 +{self.death_bonus}；没用完的留给本章后面的房间；"
                    f"只有调用模拟器的消息占一轮，同一条消息里并行的几次调用算一轮，其他工具不占轮次）")
        return (f"规划轮次：剩余 {self.bank} 轮（本章每进入一个没来过的房间 +{self.rounds_per_room}，"
                f"在游戏里死亡 +{self.death_bonus}；同一条消息里并行的工具调用算一轮，act 与 set_effort 不占轮次）")

    def deny_reason(self, *, workspace_paused=False) -> str:
        if workspace_paused:
            return ('本章的规划额度已用完，本次工具未执行。试玩仍处于用户暂停状态；'
                    '请在现有权限内处理用户要求，或等待用户继续、调整额度。不要为补充额度调用 act 或自行恢复试玩。')
        if self.sim_only:
            return (f"本章的规划轮次已经用完（每进入一个没来过的房间按房间大小存入轮次，在游戏里死亡补 "
                    f"{self.death_bonus} 轮）。工作区工具不受轮次限制；Read、Write、Edit、Glob、Grep、Bash "
                    "与 observe 照常可用。请直接调用 act 执行目前最好的路线。")
        return (f"本章的规划轮次已经用完（每进入一个没来过的房间存入 {self.rounds_per_room} 轮，在游戏里死亡补 "
                f"{self.death_bonus} 轮）。工作区工具始终可用；游戏操作仍按当前试玩状态执行。")

    # ---- 钩子 ----

    def note_tool_use(self, tool_use_id: str, message_id: str | None) -> None:
        """agent.py 读到输出流里的 tool_use 时调用。"""
        self._msg_of[tool_use_id] = message_id or tool_use_id
        if len(self._msg_of) > 5000:  # 只需要最近的
            for key in list(self._msg_of)[:2500]:
                self._msg_of.pop(key, None)

    async def _message_of(self, tool_use_id: str, wait_s: float = 15.0) -> str | None:
        """钩子可能比输出流里的 tool_use 事件先到：等一会儿。"""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if tool_use_id in self._msg_of:
                return self._msg_of[tool_use_id]
            await asyncio.sleep(0.02)
        return None

    async def decide(self, tool_name: str, tool_use_id: str | None) -> tuple[bool, str]:
        if not self.enabled or not self._started or tool_name in EXEMPT or tool_name in WORKSPACE_TOOLS:
            return True, ""
        if self.sim_only:
            return True, ""             # 离线推演仅记账，不收回工作区能力
        message = await self._message_of(tool_use_id) if tool_use_id else None
        message = message or f"?{tool_use_id}"
        if message == self._round_msg:
            return True, ""             # 同一条消息里并行的调用：同一轮
        if self.bank <= 0:
            self.metrics.add(pause_denials=1)
            self.log(f"[gate] 拒绝 {tool_name}：本章的规划轮次已用完")
            return False, self.deny_reason()
        self.bank -= 1
        self.rounds_used += 1
        self._round_msg = message
        self.metrics.add(plan_rounds_used=1)
        self._publish()
        return True, ""
