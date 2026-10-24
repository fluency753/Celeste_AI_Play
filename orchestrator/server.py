"""游戏工具 MCP 服务：观测、游玩、公开解说、推理强度与按用户要求退出关卡。"""
from __future__ import annotations

import asyncio
import time
import json

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .game_session import ActError, GameSession
from .telemetry import publish
from .kb_study import KbStudy
from .observation import describe_attempts, describe_obs, describe_step, room_json, upscale_png
from .exploration import GEOMETRY, action_hint
from .efforts import valid_effort
from .modes import game_tools, is_explore
from .prompting import fill

ACT_DESCRIPTION = """推进游戏若干帧并返回结果与新的观测。进入新房间时附截图、整个房间的字符地图与精确数据（JSON；房间信息以精确数据为准，截图仅作参考），其余时候只给文字状态与画面附近的信息。

inputs：按顺序执行的按键段，每段写作 "帧数,按键"，例如 ["12,R", "1,R,J", "15,R,J", "8,R"]。
  按键：L R U D 方向，J 跳，X 冲刺，Z crouch dash，G 抓墙，T 交互；K、Y、V 是第二个跳、冲刺、crouch dash 键
  （按住 J 时按下 K 也算一次新的"按下"，可以在相邻两帧各跳一次）；A:角度 表示摇杆角度（0 右、90 上）。
  只写帧数表示这些帧不按任何键，例如 "5"。一次最多 {act_max_frames} 帧。
plan_file：（有工作区时可用）工作区里的计划文件路径，与 inputs 二选一。内容是按键段数组，或对象：
  {"inputs": [...], "expect": [[x, y], [x, y, "Dash"], null, ...], "tolerance": 0.5,
   "stop_on": [...], "retry": {"command": "python3 planner.py", "max": 3, "timeout_s": 20, "on": ["death", "deviate"]}}
  expect 是每帧推进后 Madeline 的预期位置（与轨迹 x、y 同一口径，可附状态名），偏差超过 tolerance 像素就停下（deviate）。
  retry：死亡、偏离（或 "done"：按键用完仍在本房间）时，主控在零网络沙箱里运行 command（工作目录 /workspace），
  它应读取 /workspace/game/ 下的最新数据并改写同一个计划文件，主控接着执行，最多 max 次，不唤醒你。格式详见 /kb/game_data.md。
thought：一两句中文，会显示给观众。只写你对当前房间或这一段的分析与判断（难点在哪、怎么取舍、打算怎么过），
  不写具体数值（帧数、坐标、速度、推演条数等），也不写用了什么算法、程序搜出了什么路线。
stop_on：提前停下的条件，可选 death room uncontrollable land refill complete；
  默认 ["death", "room", "complete"]，death 与 complete 总会生效。
死亡、换房后的转场与重生由系统自动推进；剧情按当前模式处理，agent模式可能停在剧情中，需要阅读story与画面后自行操作。"""

WORKSPACE_NOTE = "（完整观测：/workspace/game/latest_obs.json；本次逐帧轨迹：/workspace/game/last_act.json）"
SIM_NOTE = "（完整观测：/workspace/game/latest_obs.json；本次逐帧轨迹：/workspace/game/last_act.json；当前帧的模拟器起始状态：/workspace/game/sim_state.json）"

OBSERVE_DESCRIPTION = "不推进游戏，返回当前帧的截图，以及整个房间的字符地图与精确数据（JSON；房间信息以精确数据为准，截图仅作参考）。act 的返回已经包含最新状态，只在需要重新看画面或整个房间时调用。"

# 整房间精确数据前的标题：房间信息以它为准，截图与字符地图只作参考（字段说明在系统提示词"房间数据"一节）
ROOM_JSON_HEADER = "整个房间的精确数据（像素坐标；房间信息以此为准，截图与字符地图仅作参考）：\n"

SET_EFFORT_DESCRIPTION = """调整你自己的推理强度：传入当前模型能力列表中的档位标识，当前值与可选档位见观测头部。
强度越高，每次思考越深入，但每次决策更慢、消耗的额度更多；简单的房间可以调低，需要精确规划的难房间可以调高。
切换在本轮结束后生效：调用后直接结束本轮（回复一句话，不再调用工具），系统会立即切换，并发消息让你接着玩。
不推进游戏，也不消耗游戏帧。reason：一句话说明为什么调整（记入日志）。"""


NARRATE_DESCRIPTION = """更新 HUD 上给观众看的思考内容（实时展示）：观众在直播画面上看你此刻在想什么。不推进游戏，也不计入章节计时。
thought：一两句中文（{thought_max} 字以内），只写你对当前房间或这一段的分析与判断，不写具体数值（帧数、坐标、速度等），
也不写用了什么算法、程序算出了什么路线；技巧与机制的专有名词用英文。
思考期间随时调用：一有初步判断就先写一句，之后想法每有进展再写一句，不要攒到 act 时才写。"""

# 实时展示的解说更新计时器：思考状态累计满 RunConfig.live_remind_s 秒没有 narrate，就在智能体下一次调用任何工具时
# 由容器里的 PreToolUse 钩子把这句提醒放到那次工具结果旁边（见 run.add_gate_route 与 agent-container/celeste-gate.py）
LIVE_REMINDER = ("（系统·实时展示）你已经思考了约 {s} 秒没有更新 HUD 上的思考内容，观众看不到你此刻的思路。"
                 "请调用 narrate 写一句你现在的分析与判断，之后想法每有进展再写一句。")


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


NO_RETRY_NOTE = ("\n本次运行限制规划轮次：本章每进入一个没来过的房间按房间大小存入轮次（一屏大小的房间 {n} 轮，房间的长、高每加倍"
                 "轮次翻倍，向上取整，至少 {n} 轮），在游戏里死亡补 {d} 轮，没用完的留给本章后面的房间；剩余轮数写在观测头部。{rule}"
                 "自动重试关闭：计划文件里的 retry 会被忽略。")
RULE_SIM = ("只有调用了模拟器的消息占一轮，其他工具不占；任何时候（任何位置、任何一帧停下时）都可以调用模拟器，只要还有剩余轮次。"
            "剩余轮数也写在每次调用模拟器的输出末尾，随时可读 /workspace/game/rounds.json。用完后 Bash 被拒绝（模拟器只能通过它调用），请直接 act。")
RULE_ALL = "act 与 set_effort 以外的工具每条消息占一轮，用完后只能 act。"
PLAYBACK_NOTE = "每一轮规划（一条调用了模拟器的消息）在模拟器里推演过的路线，系统会在下一轮之前画进游戏给观众看（不推进游戏、不计入章节计时）。"


def build_server(session: GameSession, study: KbStudy | None = None, gate=None) -> MCPServer:
    server = MCPServer(name="celeste", instructions="《蔚蓝》游戏工具：observe 查看当前帧，act 推进若干帧。",
                       log_level="WARNING")
    metrics = session.metrics
    ws_note = SIM_NOTE if session.cfg.sim else WORKSPACE_NOTE
    explore = is_explore(session.cfg)
    # 工具按模式注册（modes.game_tools）：narrate 所有模式都有；exit_level 只在直播模式（用户要求时退出关卡）；
    # cutscene 在探索、直播或 --cutscene-mode agent 时；simulate 只在探索模式开了限额短路线预测时。
    # 速通模式不开直播时工具集与之前一致：observe、act、narrate、set_effort。
    tools = set(game_tools(session.cfg))
    tool_exit_level = "exit_level" in tools
    tool_cutscene = "cutscene" in tools
    tool_simulate = "simulate" in tools
    session.cutscene_tool = tool_cutscene      # 没有剧情工具时，HUD 不提供"剧情交给智能体"的切换
    # 截图只在进入新房间时（以及主动 observe 时）附上；同时给出整个房间的实体与地图，平时只描述画面附近
    # 思考与工具时间由主控按 Claude Code 输出流里的 tool_use / tool_result 事件统计（见 metrics.py），这里不计时
    state = {"described_room": None}

    def room_key(obs: dict):
        level = obs.get("level") or {}
        return (level.get("sid"), level.get("room"))
    call_lock = asyncio.Lock()
    # Shared with runtimes that invoke MCP directly, rather than forwarding calls.
    session.tool_lock = call_lock

    def begin() -> None:
        if gate is not None:
            gate.start()  # 第一次真正执行游戏工具：每次暂停一轮工具的限制从这里开始
        if not metrics.playing:
            # 第一次真正执行游戏工具：准备阶段结束，现实用时从这里开始
            metrics.start_play()
            print(f"[run] 开始游玩：准备阶段用时 {metrics.state.study_s:.0f} 秒"
                  + (f"，知识库已读 {study.progress():.0%}" if study else ""), flush=True)

    def gated() -> str | None:
        """知识库没读完时不执行游戏工具，返回提示。"""
        guard = getattr(session, 'control_guard', None)
        if guard and (note := guard()):
            return note
        if study is None or study.ready():
            return None
        return study.reminder()

    def finish() -> None:
        metrics.update(status="thinking")

    def header_line() -> str:
        line = effort_line(metrics)
        levels = getattr(session, 'effort_levels', lambda: [])()
        if levels:
            line += "\n可选推理强度：" + "、".join(levels)
        energy = getattr(metrics.state, "sim_energy", None)
        if energy:
            line += f"\n模拟体力：{energy['balance']}/{energy['capacity']}（预测花费体力，真实操作和首次到新房间补充）"
        if gate is not None and gate.enabled:
            line += "\n" + gate.status_line()
        return line

    def credit(obs: dict) -> None:
        """规划轮次：进入本章没来过的房间就存入轮次。"""
        if gate is not None:
            level = obs.get("level") or {}
            gate.on_room(level.get("sid"), level.get("room"), level.get("bounds"))

    @server.tool(name="observe", description=OBSERVE_DESCRIPTION, structured_output=False)
    async def observe() -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            try:
                obs, image = await asyncio.to_thread(session.observe)
                credit(obs)
                state["described_room"] = room_key(obs)
                content: list = []
                if image and session.cfg.images:
                    content.append(Image(data=upscale_png(image), format="png"))
                content.append(describe_obs(obs, full_room=True, effort=header_line(), explore=explore)
                               + "\n\n" + ROOM_JSON_HEADER + room_json(obs)
                               + ("\n" + GEOMETRY if session.cfg.knowledge_profile == "explore" else "")
                               + ("\n" + ws_note if session.workspace else ""))
                return content
            finally:
                finish()

    rule = RULE_SIM if session.cfg.sim else RULE_ALL
    act_description = ACT_DESCRIPTION + (NO_RETRY_NOTE.format(n=session.cfg.rounds_per_room, d=session.cfg.death_bonus, rule=rule)
                                         if session.cfg.pause_limit else "")
    if session.cfg.playback and session.cfg.sim:
        act_description += PLAYBACK_NOTE

    @server.tool(name="act", description=fill(act_description, session.cfg), structured_output=False)
    async def act(thought: str, inputs: list[str] | None = None, plan_file: str | None = None,
                  stop_on: list[str] | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            try:
                deaths_before = metrics.state.deaths
                before = getattr(session, "last_obs", None)
                metrics.decision_begin()
                decision_wait = getattr(metrics.state, "decision_wait_s", 0)
                result = None
                try:
                    outcome = await asyncio.to_thread(session.act, inputs, thought, stop_on, plan_file)
                    result = outcome.result
                finally:
                    metrics.decision_end(result)
                parts = [describe_step(outcome.result)]
                if gate is not None:
                    gate.on_death(metrics.state.deaths - deaths_before)  # 在游戏里死了：补规划轮次
                    credit(outcome.obs)
                if len(outcome.attempts) > 1:
                    parts.insert(0, describe_attempts(outcome.attempts))
                if outcome.auto_notes:
                    parts.append("；".join(outcome.auto_notes))
                if session.done:
                    parts.append(f"【{session.done_reason}】本次试玩已结束，不需要再调用工具，请直接结束本轮。")
                new_room = room_key(outcome.obs) != state["described_room"]
                if new_room:
                    state["described_room"] = room_key(outcome.obs)
                    parts.append("【进入新房间】下面是新房间的截图、整个房间的字符地图，以及整个房间的精确数据（JSON）。房间信息以精确数据为准，截图仅作参考。")
                parts.append(describe_obs(outcome.obs, full_room=new_room, effort=header_line(), explore=explore))
                if new_room:
                    parts.append(ROOM_JSON_HEADER + room_json(outcome.obs))
                if session.cfg.knowledge_profile == "explore" and not session.done:
                    if new_room:
                        parts.append(GEOMETRY)
                    hint = action_hint(before, outcome.obs, outcome.result, inputs, decision_wait)
                    if hint:
                        parts.append(hint)
                if session.workspace:
                    parts.append(ws_note)
                content: list = []
                if outcome.image_b64 and new_room and session.cfg.images:
                    content.append(Image(data=upscale_png(outcome.image_b64), format="png"))
                content.append("\n\n".join(parts))
                return content
            except ActError as err:
                return [f"动作未执行（没有消耗任何游戏帧）：{err}"]
            finally:
                finish()

    def optional_tool(enabled: bool, **kw):
        # 不需要的工具不注册：装饰器原样返回函数，不进 MCP 工具列表
        return server.tool(**kw) if enabled else (lambda fn: fn)

    @optional_tool(tool_exit_level, name='exit_level', description='仅在用户要求退出时调用：通过游戏暂停菜单保存并退出当前关卡，回到地图或主菜单，结束本次试玩并保存录像。退出不算通关。', structured_output=False)
    async def exit_level(reason: str) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            result = await asyncio.to_thread(session.exit_level, reason)
            return [json.dumps(result,ensure_ascii=False) + '\n已退出关卡，本次试玩结束，请结束本轮，不再调用游戏工具。']

    @optional_tool(tool_cutscene, name='cutscene', description='处理剧情：wait观看播放，advance推进一页对话，choose选择当前可见选项index，skip尝试跳过整段。遇到分支选项必须明确choose，advance不会代选。frames为1～180。', structured_output=False)
    async def cutscene(action: str, frames: int = 30, choice: int | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:return [note]
            begin()
            obs,image,info=await asyncio.to_thread(session.cutscene,action,frames,choice)
            result=[]
            if image and session.cfg.images:result.append(Image(data=upscale_png(image),format='png'))
            result.append(json.dumps(info,ensure_ascii=False)+'\n'+describe_obs(obs,full_room=False,effort=header_line(),explore=explore)+'\n'+room_json(obs))
            return result

    @optional_tool(tool_simulate, name='simulate', description='消耗模拟体力，从当前真实帧预测短路线，不推进游戏。candidates 为按键段数组的数组；省略时查询体力。只有本次启用限额模拟辅助才可预测。', structured_output=False)
    async def simulate(candidates: list[list[str]] | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            if not getattr(session, 'sim_energy', None):
                return ['本次未启用限额模拟辅助。']
            if candidates is None:
                result = session.sim_energy.snapshot()
            else:
                begin()
                result = await asyncio.to_thread(session.simulate, {'candidates': candidates})
            return [json.dumps(result, ensure_ascii=False)]

    # narrate 在所有模式都有：思考期间更新 HUD 的思考语句
    @server.tool(name="narrate", description=fill(NARRATE_DESCRIPTION, session.cfg), structured_output=False)
    async def narrate(thought: str) -> list:
        # 不推进游戏、不受准备阶段限制、不开始计现实用时；只换 HUD 上的思考语句
        text = " ".join((thought or "").split())
        if not text:
            return ["thought 不能为空。"]
        limit = getattr(session.cfg, "thought_max_chars", 120)
        if len(text) > limit:
            return [f"thought 太长，请压缩到一两句话（{limit} 字以内）。"]
        metrics.set_thought(text)
        metrics.add(narrates=1)
        metrics.narrated()
        try:  # 进 HUD 的公开解说历史（只记这句话本身）
            publish(session.run_dir, f"narrate:{time.time_ns()}", text)
        except OSError:
            pass
        return ["已更新。"]

    @server.tool(name="set_effort", description=SET_EFFORT_DESCRIPTION, structured_output=False)
    async def set_effort(level: str, reason: str = "") -> list:
        # 不推进游戏、不受准备阶段限制，也不开始计现实用时；耗时可以忽略
        level = (level or "").strip()
        if not valid_effort(level):
            return ["推理强度标识格式无效。"]
        policy = getattr(session, 'effort_policy', None)
        if not policy:
            return ["尚未连接可核验模型能力的 agent，不能调整推理强度。"]
        if policy:
            try:
                await policy(level)
            except ValueError as exc:
                return [str(exc)]
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
