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
from .observation import describe_attempts, describe_obs, describe_step, upscale_png
from .efforts import valid_effort
from .modes import collision_image_mode, game_tools, is_explore
from .prompting import fill
from .collision_view import render_collision_view
from .geometry_context import geometry_page
from .observation_delivery import ObservationDelivery, FILE_JSON_HEADER, ROOM_JSON_HEADER
from .execution import workspace_capabilities

ACT_DESCRIPTION = """推进游戏若干帧并返回结果、角色状态{act_collision_image}。{observation_delivery_rule} observe(detail="geometry",region=[x,y,w,h])可按需查看最近缓存的局部精确对象；detail="player"查询完整玩家内部状态。不把当帧阻挡当成永久不可达。

inputs：按顺序执行的按键段，每段写作 "帧数,按键"，例如 ["12,R", "1,R,J", "15,R,J", "8,R"]。
input_format：省略为 celeste_ai；显式 celeste_tas 使用 TAS 离散按键 J/K 双跳、X/C 双冲刺、Z/V 双蹲冲、G/H 双抓、S 暂停、Q 重试、N 日志、O 确认，并保留兼用绑定。须 agent 剧情模式与桥接1.0.20。TAS执行和观察不自动补帧；不支持的模拟或脚本命令明确报错。
  按键：L R U D 方向，J 跳，X 冲刺，Z crouch dash，G 抓墙，T 交互；K、Y、V 是第二个跳、冲刺、crouch dash 键
  （按住 J 时按下 K 也算一次新的"按下"，可以在相邻两帧各跳一次）；A:角度 表示摇杆角度（0 右、90 上）。
  agent 剧情模式还可用 C 确认、B 返回、P 暂停、N 日志，直接操作菜单、对白、重试与结算。
  只写帧数表示这些帧不按任何键，例如 "5"。一次至少 {act_min_frames} 帧、最多 {act_max_frames} 帧（不足时在末尾补空帧或继续保持按键，截断条件照样提前停下）。
plan_file：（有工作区时可用）工作区里的计划文件路径，与 inputs 二选一。内容是按键段数组，或对象：
  {"inputs": [...], "expect": [[x, y], [x, y, "Dash"], null, ...], "tolerance": 0.5,
   "stop_on": [...], "retry": {"command": "python3 planner.py", "max": 3, "timeout_s": 20, "on": ["death", "deviate"]}}
  expect 是每帧推进后 Madeline 的预期位置（与轨迹 x、y 同一口径，可附状态名），偏差超过 tolerance 像素就停下（deviate）。
  JSON也可声明input_format，旧会话工具没有同名参数时使用此方式。/workspace/game/tas.py 可把显式选定的TAS输入行切成有来源游标的JSON计划；动作返回input_cursor与remaining_inputs，按实际consumed继续，不自动执行剩余输入。
  retry：死亡、偏离（或 "done"：按键用完仍在本房间）时，主控在零网络沙箱里运行 command（工作目录 /workspace），
  它应读取 /workspace/game/ 下的最新数据并改写同一个计划文件，主控接着执行，最多 max 次，不唤醒你。格式详见 /kb/game_data.md。
thought：一两句中文{thought_brief}，会显示给观众。只写你对当前房间或这一段的分析与判断（难点在哪、怎么取舍、打算怎么过），
  不写具体数值（帧数、坐标、速度、推演条数等），也不写用了什么算法、程序搜出了什么路线。{thought_mood_short}{formal_thought_act}
stop_on：提前停下的条件，可选 death room uncontrollable controllable land refill complete story_input story_decision；
  默认 ["death", "room", "complete"]，death 总会生效；agent 模式已登记通关后不再用 complete 反复截断结算操作。
celeste_ai格式下，死亡、换房后的转场与重生由系统自动推进；agent剧情模式下主控在可尝试跳过时先交回，可选skip或watch继续观看到实际输入节点或恢复控制。celeste_tas格式下不自动补这些帧，由明确输入推进。读取storyDecision、storyInput与story；inCutscene标记本身不表示角色失控。"""

WORKSPACE_NOTE = "（完整观测：/workspace/game/latest_obs.json；统一碰撞：/workspace/game/collision.json，image指向同一快照的碰撞图；本次逐帧轨迹：/workspace/game/last_act.json）"
SIM_NOTE = WORKSPACE_NOTE[:-1] + "；当前帧的模拟器起始状态：/workspace/game/sim_state.json）"

OBSERVE_DESCRIPTION = '默认获取新观测{observe_collision_image}。{observation_delivery_rule}detail="geometry"按region=[x,y,w,h]查询最近观测缓存，offset/limit分页最多64；后续页必须带snapshot_id，观测更新后失效。详情保留完整反射状态与整形状，不裁掉孔洞；detail="player"只查询完整玩家状态。两种详情均不推进游戏、不改变变化基线，无工作区也可用。来源scope/coverage仍限制覆盖。'

# 整房间精确数据前的标题：房间信息以它为准，截图与字符地图只作参考（字段说明在系统提示词"房间数据"一节）

SET_EFFORT_DESCRIPTION = """调整你自己的推理强度：传入当前模型能力列表中的档位标识，当前值与可选档位见观测头部。
强度越高，每次思考越深入，但每次决策更慢、消耗的额度更多；简单的房间可以调低，需要精确规划的难房间可以调高。
切换在本轮结束后生效：调用后直接结束本轮（回复一句话，不再调用工具），系统会立即切换，并发消息让你接着玩。
不推进游戏，也不消耗游戏帧。reason：一句话说明为什么调整（记入日志）。"""


NARRATE_DESCRIPTION = """更新 HUD 上给观众看的思考内容（实时展示）：观众在直播画面上看你此刻在想什么。不推进游戏，也不计入章节计时。
thought：一两句中文（{thought_limit}），只写你对当前房间或这一段的分析与判断，不写具体数值（帧数、坐标、速度等），
也不写用了什么算法、程序算出了什么路线；技巧与机制的专有名词用英文。{thought_mood_short}
思考期间随时调用：一有初步判断就先写一句，之后想法每有进展再写一句，不要攒到 act 时才写。"""

# 开了"仅正式思考"（--formal-thought）时 narrate 的说明：写的是临时思考，只显示在控制台，游戏画面的 HUD 上没有
NARRATE_TEMP_DESCRIPTION = """更新临时思考：本次开启了"仅正式思考"，你在冻结期间（两次 act 之间）写的临时思考{temp_thought_where}；游戏画面的 HUD 只显示每次 act 的 thought（正式思考）。不推进游戏，也不计入章节计时。
thought：中文（{thought_temp_limit}），写你此刻的分析、尝试与判断，不写具体数值（帧数、坐标、速度等），
也不写用了什么算法、程序算出了什么路线；技巧与机制的专有名词用英文。{thought_mood_short}
思考期间随时调用：一有初步判断就先写一句，之后想法每有进展再写一句。"""

# 实时展示的解说更新计时器：思考状态累计满 RunConfig.live_remind_s 秒没有 narrate，就在智能体下一次调用任何工具时
# 由容器里的 PreToolUse 钩子把这句提醒放到那次工具结果旁边（见 run.add_gate_route 与 agent-container/celeste-gate.py）
LIVE_REMINDER = ("（系统·实时展示）你已经思考了约 {s} 秒没有更新 HUD 上的思考内容，观众看不到你此刻的思路。"
                 "请调用 narrate 写一句你现在的分析与判断，之后想法每有进展再写一句。")
TEMP_REMINDER = ("（系统）你已经思考了约 {s} 秒没有更新临时思考。"
                 "请调用 narrate 写一句你现在的分析与判断（只显示在控制台），之后想法每有进展再写一句。")


def narrate_reminder(cfg, seconds: float) -> str:
    """解说计时满了时提醒的话：开了"仅正式思考"时提醒更新临时思考。"""
    return (TEMP_REMINDER if getattr(cfg, "formal_thought", False) else LIVE_REMINDER).format(s=int(seconds))


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
    # 所有模式都开放 stop_run：智能体报告任务阻塞，主控暂停试玩等用户处理（不结束运行，章节内保持冻结，菜单正常运行）。
    tools = set(game_tools(session.cfg))
    tool_exit_level = "exit_level" in tools
    tool_cutscene = "cutscene" in tools
    tool_simulate = "simulate" in tools
    session.cutscene_tool = tool_cutscene      # 没有剧情工具时，HUD 不提供"剧情交给智能体"的切换
    # 截图只在进入新房间时（以及主动 observe 时）附上；同时给出整个房间的实体与地图，平时只描述画面附近
    # 思考与工具时间由主控按 Claude Code 输出流里的 tool_use / tool_result 事件统计（见 metrics.py），这里不计时
    state = {"described_room": None}
    delivery = ObservationDelivery()

    def observation_context(obs, *, force=False):
        # Limited planning tools can become unavailable immediately after a quota charge.
        # Keep inline geometry available throughout those runs.
        files_enabled = workspace_capabilities(session.cfg).programs and not (gate is not None and gate.enabled)
        return delivery.prepare(session, obs, files_enabled=files_enabled, force=force)

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

    def header_line(freeze: bool = True) -> str:
        line = effort_line(metrics)
        levels = getattr(session, 'effort_levels', lambda: [])()
        if levels:
            line += "\n可选推理强度：" + "、".join(levels)
        energy = getattr(metrics.state, "sim_energy", None)
        if energy:
            line += f"\n模拟体力：{energy['balance']}/{energy['capacity']}（预测花费体力，真实操作和首次到新房间补充）"
        if gate is not None and gate.enabled:
            line += "\n" + gate.status_line()
        # 探索模式开了思考僵直：这次冻结已累计的时间与预计僵直（act 的结果里另有这次冻结的说明，不再重复）
        text = getattr(session, "freeze_line", lambda: "")() if freeze else ""
        if text:
            line += "\n" + text
        return line

    def credit(obs: dict) -> None:
        """规划轮次：进入本章没来过的房间就存入轮次。"""
        if gate is not None:
            level = obs.get("level") or {}
            gate.on_room(level.get("sid"), level.get("room"), level.get("bounds"))

    def images(obs, screenshot=None, *, show_screenshot=True, room_view=True):
        """room_view：这次是进新房间或 observe（--collision-image room 时只在这时附碰撞图）。"""
        if not session.cfg.images:
            return []
        def tagged(data, role):
            block = Image(data=data, format='png').to_image_content()
            level = obs.get('level') or {}
            block.meta = {'celeste': {'role': role, 'frame': obs.get('frame'),
                                      'sid': level.get('sid'), 'room': level.get('room')}}
            return block
        content = []
        if screenshot and show_screenshot:
            content.append(tagged(upscale_png(screenshot), 'screenshot'))
        image_mode = collision_image_mode(session.cfg)
        if image_mode == 'off' or (image_mode == 'room' and not room_view):
            return content                     # 碰撞图照样写进工作区（game/collision.json 的 image），需要时自己读
        renderer = getattr(session, 'collision_png', None)
        try:
            collision = renderer(obs) if renderer is not None else render_collision_view(obs)
        except Exception as exc:  # noqa: BLE001
            # 画图失败时游戏可能已经推进：照常返回结果，只少这张图（报错会让智能体以为没执行而重发同样的按键）
            print(f"[collision] 画碰撞图失败：{exc}", flush=True)
            content.append('（这次没有附碰撞图：画图失败；碰撞数据仍在 /workspace/game/latest_obs.json。）')
            return content
        if collision is not None:
            content.append(tagged(collision, 'collision'))
        return content

    @server.tool(name="observe", description=fill(OBSERVE_DESCRIPTION, session.cfg), structured_output=False)
    async def observe(detail: str = 'overview', region: list[float] | None = None, offset: int = 0,
                      limit: int = 32, snapshot_id: str | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            try:
                if detail == 'player':
                    if region is not None or offset or snapshot_id is not None:
                        return ['player 查询不接受区域或分页参数。']
                    with session.lock:
                        cached = session.last_obs or {}
                        if cached.get("frame") is None:
                            raise ValueError("尚无观测缓存，请先默认observe。")
                        player = {key: cached.get(key) for key in ('frame', 'player', 'controllable')}
                        player.update({key: (cached.get('level') or {}).get(key) for key in ('sid', 'room')})
                    return [json.dumps(player,ensure_ascii=False,separators=(',', ':'))]
                if detail == 'geometry':
                    with session.lock:
                        page = geometry_page(session.last_obs or {}, region=region, offset=offset, limit=limit,
                                             expected_snapshot=snapshot_id)
                    return [json.dumps(page,ensure_ascii=False,separators=(',', ':'))]
                if detail != 'overview' or region is not None or offset or snapshot_id is not None:
                    return ['区域或分页查询请用 detail="geometry"；默认 overview 会获取新观测。']
                obs, image = await asyncio.to_thread(session.observe)
                credit(obs)
                context, file_context, candidate = observation_context(obs, force=True)
                content = images(obs, image)
                notes = getattr(session, "observe_notes", None) or []
                content.append(("主控：" + "；".join(notes) + "\n\n" if notes else "")
                               + describe_obs(obs, full_room=True, effort=header_line(), explore=explore, compact_context=True, file_context=file_context)
                               + "\n\n" + context
                               + ("\n" + ws_note if session.workspace and not file_context else ""))
                delivery.commit(candidate)
                state["described_room"] = room_key(obs)
                return content
            except ValueError as exc:
                return [str(exc)]
            finally:
                finish()

    rule = RULE_SIM if session.cfg.sim else RULE_ALL
    act_description = ACT_DESCRIPTION + (NO_RETRY_NOTE.format(n=session.cfg.rounds_per_room, d=session.cfg.death_bonus, rule=rule)
                                         if session.cfg.pause_limit else "")
    if session.cfg.playback and session.cfg.sim:
        act_description += PLAYBACK_NOTE

    @server.tool(name="act", description=fill(act_description, session.cfg), structured_output=False)
    async def act(thought: str, inputs: list[str] | None = None, plan_file: str | None = None,
                  stop_on: list[str] | None = None, input_format: str | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            try:
                deaths_before = metrics.state.deaths
                metrics.decision_begin()
                result = None
                try:
                    options = {'input_format':input_format} if input_format is not None else {}
                    outcome = await asyncio.to_thread(session.act, inputs, thought, stop_on, plan_file, **options)
                    result = outcome.result
                finally:
                    metrics.decision_end(result)
                parts = [describe_step(outcome.result, max_rows=8)]
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
                context, file_context, candidate = observation_context(outcome.obs, force=new_room)
                if new_room:
                    parts.append("【进入新房间】完整碰撞数据已更新到当前观测文件。" if file_context else "【进入新房间】下面是截图、完整碰撞图及精确布局；后续只返回相对此布局的变化。")
                parts.append(describe_obs(outcome.obs, full_room=new_room, effort=header_line(freeze=False), explore=explore, compact_context=True, file_context=file_context))
                parts.append(context)
                if session.workspace and not file_context:
                    parts.append(ws_note)
                content = images(outcome.obs, outcome.image_b64, show_screenshot=new_room, room_view=new_room)
                content.append("\n\n".join(parts))
                delivery.commit(candidate)
                state["described_room"] = room_key(outcome.obs)
                return content
            except ActError as err:
                return [f"动作未执行（没有消耗任何游戏帧）：{err}"]
            finally:
                finish()

    def optional_tool(enabled: bool, **kw):
        # 不需要的工具不注册：装饰器原样返回函数，不进 MCP 工具列表
        return server.tool(**kw) if enabled else (lambda fn: fn)

    @server.tool(name='stop_run', description='任务被工具故障、缺少能力或其他无法继续的条件阻塞时，用 reason 说明原因（200 字以内）报告给用户：主控暂停试玩，章节内保持冻结，菜单正常运行，由用户决定继续还是停止。不是通关，不退出关卡，不用于普通死亡或仍可尝试的难点。调用后结束本轮，等待用户处理；暂停或知识库未读完也能报告。', structured_output=False)
    async def stop_run(reason: str) -> list:
        async with call_lock:
            try:
                result = await asyncio.to_thread(session.stop_run, reason)
            except ActError as err:
                return [str(err)]
            publish(session.run_dir, f"system:agent-stop:{len(session.agent_stop['reports'])}",
                    '智能体报告任务阻塞：' + result['reason'] + ('；试玩已暂停，等待用户处理。' if result['paused'] else ''),
                    role='system', kind='系统')
            tail = ('主控已把试玩暂停，章节内保持冻结，菜单正常运行，等待用户处理。请结束本轮，不再调用工具。' if result['paused']
                    else '主控已记下阻塞原因，等待用户处理。请结束本轮，不再调用工具。')
            return [json.dumps(result, ensure_ascii=False) + '\n' + tail]

    @optional_tool(tool_exit_level, name='exit_level', description='仅在用户要求退出时调用：通过游戏暂停菜单保存并退出当前关卡，回到地图或主菜单，结束本次试玩并保存录像。退出不算通关。', structured_output=False)
    async def exit_level(reason: str) -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            begin()
            result = await asyncio.to_thread(session.exit_level, reason)
            return [json.dumps(result,ensure_ascii=False) + '\n已退出关卡，本次试玩结束，请结束本轮，不再调用游戏工具。']

    @optional_tool(tool_cutscene, name='cutscene', description='剧情可尝试跳过时先交回决定：skip尝试暂停菜单跳过整段，watch不按键观看到下一真实输入节点或恢复控制。advance确认当前完整对白或明信片，choose选择当前可见选项index；教学用act。无需wait，advance不会代选，确认后自动播放到下一节点。', structured_output=False)
    async def cutscene(action: str, choice: int | None = None) -> list:
        async with call_lock:
            if (note := gated()) is not None:return [note]
            begin()
            obs,image,info=await asyncio.to_thread(session.cutscene,action,choice)
            new_room = room_key(obs) != state['described_room']
            context, file_context, candidate = observation_context(obs, force=new_room)
            result=images(obs, image, room_view=new_room)
            result.append(json.dumps(info,ensure_ascii=False)+'\n'+describe_obs(obs,full_room=False,effort=header_line(),explore=explore,compact_context=True,file_context=file_context)+'\n'+context)
            delivery.commit(candidate)
            state['described_room'] = room_key(obs)
            return result

    @optional_tool(tool_simulate, name='simulate', description='消耗模拟体力，从当前真实帧预测短路线，不推进游戏。candidates 为按键段数组的数组；省略时查询体力。只有本次启用限额模拟辅助才可预测。', structured_output=False)
    async def simulate(candidates: list[list[str]] | None = None, input_format: str = 'celeste_ai') -> list:
        async with call_lock:
            if (note := gated()) is not None:
                return [note]
            if not getattr(session, 'sim_energy', None):
                return ['本次未启用限额模拟辅助。']
            if candidates is None:
                result = session.sim_energy.snapshot()
            else:
                begin()
                request = {'candidates':candidates}
                if input_format != 'celeste_ai':request['input_format'] = input_format
                result = await asyncio.to_thread(session.simulate, request)
            return [json.dumps(result, ensure_ascii=False)]

    # narrate 在所有模式都有：思考期间更新 HUD 的思考语句
    temp_thoughts = bool(getattr(session.cfg, "formal_thought", False))

    @server.tool(name="narrate", description=fill(NARRATE_TEMP_DESCRIPTION if temp_thoughts else NARRATE_DESCRIPTION,
                                                  session.cfg), structured_output=False)
    async def narrate(thought: str) -> list:
        # 不推进游戏、不受准备阶段限制、不开始计现实用时；只换 HUD 上的思考语句
        # 开了"仅正式思考"时写的是临时思考：只进公开历史（控制台），不换游戏内 HUD 上的正式思考
        text = " ".join((thought or "").split())
        if not text:
            return ["thought 不能为空。"]
        from .thought import thought_problem
        problem = thought_problem(session.cfg, text, temp=temp_thoughts)
        if problem:
            return [problem + "。"]
        if temp_thoughts:
            metrics.set_temp_thought(text)
        else:
            metrics.set_thought(text)
        metrics.add(narrates=1)
        metrics.narrated()
        try:  # 进 HUD 的公开解说历史（只记这句话本身）
            publish(session.run_dir, f"narrate:{time.time_ns()}", text, kind="临时思考" if temp_thoughts else "解说")
        except OSError:
            pass
        return ["已更新（临时思考，只显示在控制台）。" if temp_thoughts else "已更新。"]

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
