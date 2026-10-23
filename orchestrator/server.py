"""游戏工具 MCP 服务：智能体能用的只有这里的 observe 与 act。"""
from __future__ import annotations

import asyncio
import time

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .game_session import ActError, GameSession
from .observation import describe_obs, describe_step, room_json, upscale_png

ACT_DESCRIPTION = """推进游戏若干帧并返回结果与新的观测。进入新房间时附截图与整个房间的实体和字符地图，其余时候只给文字状态与画面附近的信息。

inputs：按顺序执行的按键段，每段写作 "帧数,按键"，例如 ["12,R", "1,R,J", "15,R,J", "8,R"]。
plan_file：（有工作区时可用）工作区里的计划文件路径，内容是同样格式的 JSON 数组；与 inputs 二选一。
  按键：L R U D 方向，J 跳，X 冲刺，Z 蹲冲，G 抓墙，T 交互；A:角度 表示摇杆角度（0 右、90 上）。
  只写帧数表示这些帧不按任何键，例如 "5"。一次最多 600 帧。
thought：一两句中文，说明这段操作的意图，会显示给观众。
stop_on：提前停下的条件，可选 death room uncontrollable land refill complete；
  默认 ["death", "room", "complete"]，death 与 complete 总会生效。
死亡、换房后的转场、剧情与重生由系统自动推进，返回时 Madeline 已可操作。"""

WORKSPACE_NOTE = "（完整观测：/workspace/game/latest_obs.json；本次逐帧轨迹：/workspace/game/last_act.json）"

OBSERVE_DESCRIPTION = "不推进游戏，返回当前帧的截图，以及整个房间的实体与字符地图。act 的返回已经包含最新状态，只在需要重新看画面或整个房间时调用。"


# 只接受本机与容器（经 host.docker.internal）的请求，防止 DNS 重绑定
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "host.docker.internal:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://host.docker.internal:*"],
)


def build_server(session: GameSession) -> MCPServer:
    server = MCPServer(name="celeste", instructions="《蔚蓝》游戏工具：observe 查看当前帧，act 推进若干帧。",
                       log_level="WARNING")
    metrics = session.metrics
    # 截图只在进入新房间时（以及主动 observe 时）附上；同时给出整个房间的实体与地图，平时只描述画面附近
    state = {"last_return": time.monotonic(), "described_room": None}

    def room_key(obs: dict):
        level = obs.get("level") or {}
        return (level.get("sid"), level.get("room"))
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
                state["described_room"] = room_key(obs)
                content: list = []
                if image:
                    content.append(Image(data=upscale_png(image), format="png"))
                content.append(describe_obs(obs, *clock_text(), full_room=True)
                               + "\n\n整个房间的精确数据（像素坐标）：\n" + room_json(obs)
                               + ("\n" + WORKSPACE_NOTE if session.workspace else ""))
                return content
            finally:
                finish(t0, 0.0)

    @server.tool(name="act", description=ACT_DESCRIPTION, structured_output=False)
    async def act(thought: str, inputs: list[str] | None = None, plan_file: str | None = None,
                  stop_on: list[str] | None = None) -> list:
        async with call_lock:
            t0 = begin()
            game_s = 0.0
            auto_before = metrics.state.auto_game_s
            try:
                outcome = await asyncio.to_thread(session.act, inputs, thought, stop_on, plan_file)
                # 工具时间不含任何推进游戏的时间（动作块与自动推进段都算游戏时间）
                game_s = outcome.game_s + (metrics.state.auto_game_s - auto_before)
                parts = [describe_step(outcome.result)]
                if outcome.auto_notes:
                    parts.append("；".join(outcome.auto_notes))
                if session.done:
                    parts.append(f"【{session.done_reason}】本章已结束，不需要再调用工具，请直接结束本轮。")
                new_room = room_key(outcome.obs) != state["described_room"]
                if new_room:
                    state["described_room"] = room_key(outcome.obs)
                    parts.append("【进入新房间】下面是新房间的截图、整个房间的字符地图，以及整个房间的精确数据（JSON）。")
                parts.append(describe_obs(outcome.obs, *clock_text(), full_room=new_room))
                if new_room:
                    parts.append("整个房间的精确数据（像素坐标）：\n" + room_json(outcome.obs))
                if session.workspace:
                    parts.append(WORKSPACE_NOTE)
                content: list = []
                if outcome.image_b64 and new_room:
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
