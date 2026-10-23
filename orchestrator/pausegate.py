"""每次暂停的工具预算：act 返回之后（游戏冻结），智能体只能再思考一轮、调用一轮工具，然后必须 act。

为什么：让试错过程真的在游戏里发生、被录进录像——智能体不能在一次暂停里反复跑模拟器，直到算出一条
一次通关整个房间的路线再执行；每次尝试都带一句新的思考语句。

怎么做：容器的托管配置里登记了 PreToolUse 钩子（agent-container/celeste-gate.py），Claude Code 每次调用
工具之前都会问主控的 /gate。这里按 Claude Code 输出流里的 tool_use 所属的消息分组：一条消息里并行发起的
几个调用算同一轮。规则：
- 准备阶段（游戏工具还没有真正执行过）不受限制；第一次执行游戏工具时开始第一次暂停（这次调用本身不占预算）。
  注意不能看 metrics.playing：有准备阶段时，第一次游戏工具的钩子先于它开始计时。
  续跑时没有准备阶段：一开始就是一次暂停。
- act 与 set_effort 不受限制。act 执行完、返回之后是一次新的暂停，预算重新计——但只有这一次 act 算得上
  一次尝试时才重新计：推进了至少 min_frames 帧，或者因为事件（死亡、换房间、落地、偏离预期……）停下。
  只推进一两帧的 act（例如为了换取新的一轮工具）不重新计预算；动作不合法、没有推进时也不重新计。
- 每次暂停里，第一条带工具调用的消息放行；之后别的消息里的工具调用（包括 observe）一律拒绝，
  拒绝理由交给模型，提示它直接 act。
"""
from __future__ import annotations

import asyncio
import time

EXEMPT = {"mcp__celeste__act", "mcp__celeste__set_effort"}

DENY_REASON = ("这次暂停的工具机会已经用完：每次 act 之后只能再调用一轮工具（同一条消息里并行发起的几个调用算一轮）。"
               "现在请直接调用 act 执行——路线没有把握也没关系，试错的过程本身会展示给观众；"
               "act 的 thought 写这一次尝试的意图或对上次结果的修正。")


class PauseGate:
    def __init__(self, metrics, enabled: bool = True, log=print, min_frames: int = 20, started: bool = False):
        self.metrics = metrics
        self.enabled = enabled
        self.log = log
        self.min_frames = min_frames
        self._msg_of: dict[str, str] = {}     # tool_use 编号 → 所在消息的编号（来自输出流）
        self._round_msg: str | None = None    # 本次暂停已经放行的那一轮（消息编号）
        self._used = False
        self._started = started               # 调用过游戏工具（准备阶段结束；续跑时一开始就是）

    def note_tool_use(self, tool_use_id: str, message_id: str | None) -> None:
        """agent.py 读到输出流里的 tool_use 时调用。"""
        self._msg_of[tool_use_id] = message_id or tool_use_id
        if len(self._msg_of) > 5000:  # 只需要最近的
            for key in list(self._msg_of)[:2500]:
                self._msg_of.pop(key, None)

    def reset(self) -> None:
        """新的一次暂停：工具预算重新计。"""
        self._round_msg = None
        self._used = False

    def after_act(self, consumed: int, stop: str) -> bool:
        """act 执行完：算得上一次尝试就开始新的暂停。返回预算是否重新计（False 时 act 的结果里会提示）。"""
        if not self.enabled:
            return True
        if consumed >= self.min_frames or (consumed > 0 and stop not in ("done", "")):
            self.reset()
            return True
        return not self._used   # 这一轮还没用掉时，预算本来就在

    async def _message_of(self, tool_use_id: str, wait_s: float = 15.0) -> str | None:
        """钩子可能比输出流里的 tool_use 事件先到：等一会儿。"""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if tool_use_id in self._msg_of:
                return self._msg_of[tool_use_id]
            await asyncio.sleep(0.02)
        return None

    def start(self) -> None:
        """游戏工具第一次真正执行（server.py 的 begin）：准备阶段结束，开始第一次暂停。
        以执行为准、不以钩子里看到的调用为准：准备阶段知识库没读完时游戏工具不执行，不算开始。"""
        if not self._started:
            self._started = True
            self.reset()

    async def decide(self, tool_name: str, tool_use_id: str | None) -> tuple[bool, str]:
        # 准备阶段（还没有真正执行过游戏工具）不受限制；第一次调用游戏工具本身也不占预算（它在 start 之前）
        if not self.enabled or not self._started or tool_name in EXEMPT:
            return True, ""
        message = await self._message_of(tool_use_id) if tool_use_id else None
        message = message or f"?{tool_use_id}"
        if not self._used:
            self._used = True
            self._round_msg = message
            return True, ""
        if message == self._round_msg:
            return True, ""
        self.metrics.add(pause_denials=1)
        self.log(f"[gate] 拒绝 {tool_name}：本次暂停的工具机会已经用完")
        return False, DENY_REASON
