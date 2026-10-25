"""规划轮次：按章节记账，限制智能体在每个房间能用模拟器打磨路线的次数。

为什么：不限制时，智能体会在暂停里反复跑模拟器，直到把一个房间打磨到极致（录像里看不到这些尝试）；
每次暂停只给一轮又太紧，它只敢走稳妥的慢路线。折中是按章节给一个规划轮次的池子：每进入一个没来过的
房间存入 rounds_per_room 轮（默认 3），在游戏里真的死了补 death_bonus 轮（默认 1），没用完的留给后面的
房间，换章节清零。规划过程本身由 playback.py 在执行前画进游戏（规划回放），观众能看到试错。

怎么做：容器的托管配置里登记了 PreToolUse 钩子（agent-container/celeste-gate.py），Claude Code 每次调用
工具之前都会问主控的 /gate。规则：
- 每个房间存入的轮次按房间大小算：以一屏大小（320×180，1A 第一个房间那么大）存入 rounds_per_room 轮为基准，房间的长、
  高每变成 room_scale 倍（默认 2），轮次就乘 room_scale，向上取整——即 ceil(rounds_per_room × (宽/320)^k × (高/180)^k)，
  k = log2(room_scale)；默认 k = 1，与面积成正比（默认 3 轮时，两倍长、两倍高的房间 12 轮）；每个房间至少 rounds_per_room 轮。
- 准备阶段（游戏工具还没有真正执行过）不受限制。注意不能看 metrics.playing：有准备阶段时，
  第一次游戏工具的钩子先于它开始计时。
- 开放了预置模拟器（sim_only）时，只有调用了模拟器的消息占一轮：一条消息的工具全部返回后，这期间 /sim 写出了
  推演记录（或命令里直接调用了 celeste-sim），playback.py 就调用 charge 扣一轮；同一条消息里并行的几次调用算一轮。
  其他工具（Read、Write、不调用模拟器的 Bash、observe……）不占轮次。模拟器只能通过 Bash 调用，所以池子空了
  就拒绝 Bash（拒绝理由交给模型，提示它直接 act），其他工具照常放行。
- 没有预置模拟器时，act 与 set_effort 以外的工具每条新消息占一轮（按 Claude Code 输出流里 tool_use 所属的消息分组），
  池子空了就全部拒绝。
- 续跑时没有准备阶段，池子从续跑后进入的第一个房间开始重新记。
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path

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

EXEMPT = {"mcp__celeste__stop_run", "mcp__celeste__act", "mcp__celeste__set_effort", "mcp__celeste__narrate"}


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
            return (f"规划轮次：剩余 {self.bank} 轮（这个房间存入了 {self.last_deposit} 轮：按房间大小，一屏大小的房间 "
                    f"{self.rounds_per_room} 轮，长、高每加倍轮次翻倍，至少 {self.rounds_per_room} 轮；在游戏里死亡 +{self.death_bonus}；没用完的留给本章后面的房间；"
                    f"只有调用模拟器的消息占一轮，同一条消息里并行的几次调用算一轮，其他工具不占轮次）")
        return (f"规划轮次：剩余 {self.bank} 轮（本章每进入一个没来过的房间 +{self.rounds_per_room}，"
                f"在游戏里死亡 +{self.death_bonus}；同一条消息里并行的工具调用算一轮，act 与 set_effort 不占轮次）")

    def deny_reason(self) -> str:
        if self.sim_only:
            return (f"本章的规划轮次已经用完（每进入一个没来过的房间按房间大小存入轮次，在游戏里死亡补 "
                    f"{self.death_bonus} 轮）。模拟器只能通过 Bash 调用，所以 Bash 暂时不能用；Read、Write、Edit、Glob、Grep "
                    "与 observe 照常可用。请直接调用 act 执行目前最好的路线。")
        return (f"本章的规划轮次已经用完（每进入一个没来过的房间存入 {self.rounds_per_room} 轮，在游戏里死亡补 "
                f"{self.death_bonus} 轮）。现在请直接调用 act 执行目前最好的路线；act 的 thought 写这一次尝试的意图。")

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
        if not self.enabled or not self._started or tool_name in EXEMPT:
            return True, ""
        if self.sim_only:
            if tool_name != "Bash":
                return True, ""         # 只有模拟器占轮次，而模拟器只能通过 Bash 调用
            # 等输出流读到这次调用：上一条消息的工具结果在它前面，上一轮该扣的已经扣了
            if tool_use_id:
                await self._message_of(tool_use_id)
            if self.bank <= 0:
                self.metrics.add(pause_denials=1)
                self.log("[gate] 拒绝 Bash：本章的规划轮次已用完")
                return False, self.deny_reason()
            return True, ""
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
