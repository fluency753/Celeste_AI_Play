"""游戏工具 MCP 服务：智能体能用的只有这里的 observe 与 act。"""
from __future__ import annotations

import asyncio
import time

from mcp.server.mcpserver import Image, MCPServer

from .game_session import ActError, GameSession
from .observation import describe_obs, describe_step, upscale_png

ACT_DESCRIPTION = """推进游戏若干帧并返回结果与新的观测（截图 + 文字状态）。

inputs：按顺序执行的按键段，每段写作 "帧数,按键"，例如 ["12,R", "1,R,J", "15,R,J", "8,R"]。
  按键：L R U D 方向，J 跳，X 冲刺，Z 下冲刺，G 抓墙，T 交互；A:角度 表示摇杆角度（0 右、90 上）。
  只写帧数表示这些帧不按任何键，例如 "5"。一次最多 600 帧。
thought：一两句中文，说明这段操作的意图，会显示给观众。
stop_on：提前停下的条件，可选 death room uncontrollable land refill complete；
  默认 ["death", "room", "complete"]，death 与 complete 总会生效。
死亡、换房后的转场、剧情与重生由系统自动推进，返回时 Madeline 已可操作。"""

OBSERVE_DESCRIPTION = "不推进游戏，返回当前帧的截图与文字状态。通常 act 的返回已经包含最新观测，只在需要重新查看时调用。"


def build_server(session: GameSession) -> MCPServer:
    server = MCPServer(name="celeste", instructions="《蔚蓝》游戏工具：observe 查看当前帧，act 推进若干帧。",
                       log_level="WARNING")
    metrics = session.metrics
    state = {"last_return": time.monotonic()}
    call_lock = asyncio.Lock()

    def begin() -> float:
        now = time.monotonic()
        metrics.add(thinking_s=now - state["last_return"])
        return now

    def finish(t_start: float, game_s: float) -> None:
        now = time.monotonic()
        metrics.add(tool_s=max(0.0, (now - t_start) - game_s))
        state["last_return"] = now
        metrics.update(status="thinking")

    def clock_text() -> tuple[float, float]:
        return metrics.elapsed(), metrics.state.budget_s

    @server.tool(name="observe", description=OBSERVE_DESCRIPTION, structured_output=False)
    async def observe() -> list:
        async with call_lock:
            t0 = begin()
            try:
                obs, image = await asyncio.to_thread(session.observe)
                content: list = []
                if image:
                    content.append(Image(data=upscale_png(image), format="png"))
                content.append(describe_obs(obs, *clock_text()))
                return content
            finally:
                finish(t0, 0.0)

    @server.tool(name="act", description=ACT_DESCRIPTION, structured_output=False)
    async def act(inputs: list[str], thought: str, stop_on: list[str] | None = None) -> list:
        async with call_lock:
            t0 = begin()
            game_s = 0.0
            auto_before = metrics.state.auto_game_s
            try:
                outcome = await asyncio.to_thread(session.act, inputs, thought, stop_on)
                # 工具时间不含任何推进游戏的时间（动作块与自动推进段都算游戏时间）
                game_s = outcome.game_s + (metrics.state.auto_game_s - auto_before)
                parts = [describe_step(outcome.result)]
                if outcome.auto_notes:
                    parts.append("；".join(outcome.auto_notes))
                if session.done:
                    parts.append(f"【{session.done_reason}】本章已结束，不需要再调用工具，请直接结束本轮。")
                parts.append(describe_obs(outcome.obs, *clock_text()))
                content: list = []
                if outcome.image_b64:
                    content.append(Image(data=upscale_png(outcome.image_b64), format="png"))
                content.append("\n\n".join(parts))
                return content
            except ActError as err:
                return [f"动作未执行（没有消耗任何游戏帧）：{err}"]
            finally:
                finish(t0, game_s)

    def reset_clock() -> None:
        state["last_return"] = time.monotonic()

    server.reset_clock = reset_clock  # type: ignore[attr-defined]
    return server
