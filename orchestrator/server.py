"""游戏工具 MCP 服务：智能体能用的游戏工具只有这里的 observe、act 与 set_effort。"""
from __future__ import annotations

import asyncio

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .game_session import ActError, GameSession
from .kb_study import KbStudy
from .observation import describe_attempts, describe_obs, describe_step, room_json, upscale_png

ACT_DESCRIPTION = """推进游戏若干帧并返回结果与新的观测。进入新房间时附截图与整个房间的实体和字符地图，其余时候只给文字状态与画面附近的信息。

inputs：按顺序执行的按键段，每段写作 "帧数,按键"，例如 ["12,R", "1,R,J", "15,R,J", "8,R"]。
  按键：L R U D 方向，J 跳，X 冲刺，Z 蹲冲，G 抓墙，T 交互；K、Y、V 是第二个跳、冲刺、蹲冲键
  （按住 J 时按下 K 也算一次新的"按下"，可以在相邻两帧各跳一次）；A:角度 表示摇杆角度（0 右、90 上）。
  只写帧数表示这些帧不按任何键，例如 "5"。一次最多 600 帧。
plan_file：（有工作区时可用）工作区里的计划文件路径，与 inputs 二选一。内容是按键段数组，或对象：
  {"inputs": [...], "expect": [[x, y], [x, y, "Dash"], null, ...], "tolerance": 0.5,
   "stop_on": [...], "retry": {"command": "python3 planner.py", "max": 3, "timeout_s": 20, "on": ["death", "deviate"]}}
  expect 是每帧推进后 Madeline 的预期位置（与轨迹 x、y 同一口径，可附状态名），偏差超过 tolerance 像素就停下（deviate）。
  retry：死亡、偏离（或 "done"：按键用完仍在本房间）时，主控在零网络沙箱里运行 command（工作目录 /workspace），
  它应读取 /workspace/game/ 下的最新数据并改写同一个计划文件，主控接着执行，最多 max 次，不唤醒你。格式详见 /kb/game_data.md。
thought：一两句中文，说明这段操作的意图，会显示给观众。
stop_on：提前停下的条件，可选 death room uncontrollable land refill complete；
  默认 ["death", "room", "complete"]，death 与 complete 总会生效。
死亡、换房后的转场、剧情与重生由系统自动推进，返回时 Madeline 已可操作。"""

WORKSPACE_NOTE = "（完整观测：/workspace/game/latest_obs.json；本次逐帧轨迹：/workspace/game/last_act.json）"
SIM_NOTE = "（完整观测：/workspace/game/latest_obs.json；本次逐帧轨迹：/workspace/game/last_act.json；当前帧的模拟器起始状态：/workspace/game/sim_state.json）"

OBSERVE_DESCRIPTION = "不推进游戏，返回当前帧的截图，以及整个房间的实体与字符地图。act 的返回已经包含最新状态，只在需要重新看画面或整个房间时调用。"

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

SET_EFFORT_DESCRIPTION = """调整你自己的推理强度：low、medium、high、xhigh、max（由低到高；默认 high，当前值在观测头部）。
强度越高，每次思考越深入，但每次决策更慢、消耗的额度更多；简单的房间可以调低，需要精确规划的难房间可以调高。
切换在本轮结束后生效：调用后直接结束本轮（回复一句话，不再调用工具），系统会立即切换，并发消息让你接着玩。
不推进游戏，也不消耗游戏帧。reason：一句话说明为什么调整（记入日志）。"""


def effort_line(metrics) -> str:
    st = metrics.state
    line = f"推理强度：{st.effort}"
    if st.effort_pending:
        line += f"（已申请切换为 {st.effort_pending}，结束本轮后生效）"
    return line


# 只接受本机与容器（经 host.docker.internal）的请求，防止 DNS 重绑定
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "host.docker.internal:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://host.docker.internal:*"],
)


def build_server(session: GameSession, study: KbStudy | None = None) -> MCPServer:
    server = MCPServer(name="celeste", instructions="《蔚蓝》游戏工具：observe 查看当前帧，act 推进若干帧。",
                       log_level="WARNING")
    metrics = session.metrics
    ws_note = SIM_NOTE if session.cfg.sim else WORKSPACE_NOTE
    # 截图只在进入新房间时（以及主动 observe 时）附上；同时给出整个房间的实体与地图，平时只描述画面附近
    # 思考与工具时间由主控按 Claude Code 输出流里的 tool_use / tool_result 事件统计（见 metrics.py），这里不计时
    state = {"described_room": None}

    def room_key(obs: dict):
        level = obs.get("level") or {}
        return (level.get("sid"), level.get("room"))
    call_lock = asyncio.Lock()

    def begin() -> None:
        if not metrics.playing:
            # 第一次真正执行游戏工具：准备阶段结束，现实用时从这里开始
            metrics.start_play()
            print(f"[run] 开始游玩：准备阶段用时 {metrics.state.study_s:.0f} 秒"
                  + (f"，知识库已读 {study.progress():.0%}" if study else ""), flush=True)

    def gated() -> str | None:
        """知识库没读完时不执行游戏工具，返回提示。"""
        if study is None or study.ready():
            return None
        return study.reminder()

    def finish() -> None:
        metrics.update(status="thinking")

    def clock_text() -> tuple[float, float]:
        return metrics.elapsed(), metrics.state.budget_s

    @server.tool(name="observe", description=OBSERVE_DESCRIPTION, structured_output=False)
    async def observe() -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            try:
                obs, image = await asyncio.to_thread(session.observe)
                state["described_room"] = room_key(obs)
                content: list = []
                if image:
                    content.append(Image(data=upscale_png(image), format="png"))
                content.append(describe_obs(obs, *clock_text(), full_room=True, effort=effort_line(metrics))
                               + "\n\n整个房间的精确数据（像素坐标）：\n" + room_json(obs)
                               + ("\n" + ws_note if session.workspace else ""))
                return content
            finally:
                finish()

    @server.tool(name="act", description=ACT_DESCRIPTION, structured_output=False)
    async def act(thought: str, inputs: list[str] | None = None, plan_file: str | None = None,
                  stop_on: list[str] | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            try:
                outcome = await asyncio.to_thread(session.act, inputs, thought, stop_on, plan_file)
                parts = [describe_step(outcome.result)]
                if len(outcome.attempts) > 1:
                    parts.insert(0, describe_attempts(outcome.attempts))
                if outcome.auto_notes:
                    parts.append("；".join(outcome.auto_notes))
                if session.done:
                    parts.append(f"【{session.done_reason}】本次要通关的章节已全部完成，不需要再调用工具，请直接结束本轮。")
                new_room = room_key(outcome.obs) != state["described_room"]
                if new_room:
                    state["described_room"] = room_key(outcome.obs)
                    parts.append("【进入新房间】下面是新房间的截图、整个房间的字符地图，以及整个房间的精确数据（JSON）。")
                parts.append(describe_obs(outcome.obs, *clock_text(), full_room=new_room, effort=effort_line(metrics)))
                if new_room:
                    parts.append("整个房间的精确数据（像素坐标）：\n" + room_json(outcome.obs))
                if session.workspace:
                    parts.append(ws_note)
                content: list = []
                if outcome.image_b64 and new_room:
                    content.append(Image(data=upscale_png(outcome.image_b64), format="png"))
                content.append("\n\n".join(parts))
                return content
            except ActError as err:
                return [f"动作未执行（没有消耗任何游戏帧）：{err}"]
            finally:
                finish()

    @server.tool(name="set_effort", description=SET_EFFORT_DESCRIPTION, structured_output=False)
    async def set_effort(level: str, reason: str = "") -> list:
        # 不推进游戏、不受准备阶段限制，也不开始计现实用时；耗时可以忽略
        level = (level or "").strip().lower()
        if level not in EFFORT_LEVELS:
            return [f"无效的推理强度 {level!r}：只能是 {'、'.join(EFFORT_LEVELS)}。"]
        current, pending = metrics.state.effort, metrics.state.effort_pending
        if level == current:
            if pending:
                metrics.request_effort(None)
                return [f"已取消切换到 {pending} 的申请，推理强度保持 {current}。"]
            return [f"推理强度已经是 {current}，不需要切换。"]
        metrics.request_effort(level, (reason or "").strip()[:200])
        print(f"[run] 智能体申请把推理强度从 {current} 切换为 {level}：{reason}", flush=True)
        return [f"已登记：推理强度将从 {current} 切换为 {level}。现在请直接结束本轮（回复一句话，不再调用工具），"
                f"系统会立即切换并让你继续；在本轮结束之前仍按 {current} 运行。"]

    return server
