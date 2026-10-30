"""Shared play, knowledge preparation and resume prompts."""
from dataclasses import dataclass

from .config import REPO_ROOT, chapter_name_by_id
from .kb_study import KbStudy
from .execution import workspace_capabilities
from .modes import act_frame_cap, act_frame_min, collision_image_mode, game_tools, has_cutscene_tool, is_explore

PROMPTS = REPO_ROOT / "orchestrator" / "prompts"


def remind_text(seconds: float) -> str:
    return "半分钟" if seconds == 30 else f"{seconds:g} 秒"


# 速通模式的系统提示词里逐个列出本次注册的游戏工具（随直播、剧情、限额预测等设置变化，见 modes.game_tools）
TOOL_NOTES = {
    "observe": '查看当前帧，不推进游戏；detail="time"按秒查询计时与各时长目标已用／剩余',
    "act": "推进若干帧",
    "narrate": "思考期间更新观众看到的思考，不推进游戏",
    "set_effort": "调整你自己的推理强度",
    "stop_run": "任务阻塞时说明原因报告给用户，主控暂停试玩等用户处理",
    "exit_level": "用户要求返回地图时离开当前关卡并继续任务；仅明确结束整次试玩才设finish_run=true",
    "cutscene": "在剧情决策点尝试跳过或继续观看，在真实输入节点推进或选择",
    "simulate": "花模拟体力预测短路线",
}
CN_COUNT = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}


def game_tool_list(cfg) -> str:
    items = [f"`{name}`（{TOOL_NOTES.get(name, '见工具说明')}）" for name in game_tools(cfg)]
    listed = items[0] if len(items) == 1 else "、".join(items[:-1]) + "和 " + items[-1]
    return f"游戏工具有{CN_COUNT.get(len(items), str(len(items)))}个：{listed}。"


def auto_handling(cfg) -> str:
    """interface_zh.md 里"系统自动处理的部分"：没有剧情工具时剧情一律自动跳过；有剧情工具时按当前剧情模式处理。"""
    if has_cutscene_tool(cfg):
        return ("屏幕转场、死亡动画与重生由系统自动推进。拿到磁带、吃心后画面冻结等确认的界面，auto 时系统替你按确认，agent 时交回由你用 act 发 C。"
                "剧情按当前的剧情模式处理：auto 时系统自动推进并跳过；"
                "agent 时剧情可尝试跳过便先交回，用 `cutscene` 的 skip 尝试跳过或 watch 观看到下一真实输入节点；对白用 advance，分支明确 choose，教学用 `act`，无需 wait。剧情标记不等于失控，角色可操作时仍可用 `act`；桥接明确识别自动播放时，也可显式 `watch` 观看。"
                "agent 模式下，吃心提示、结算确认及返回选章地图都由你观察界面后用 act 操作：C 确认、B 返回，纯帧数推进动画。"
                "complete 事件或 completed=true 只是游戏登记了成绩，不是任务结束；回到选章地图后主控才确认章节完成，不会代按结算。")
    return ("剧情对话、屏幕转场、死亡动画与重生都由系统自动推进并跳过，拿到磁带、吃心后画面冻结等确认的界面也由系统按确认，"
            "`act` 返回时 Madeline 已经可以操作；仍卡在确认界面时可以用 act 发 C 确认。")


def explore_program_rule(cfg) -> str:
    """探索模式提示词里单次程序时长的一句（explore_program_s 为 None 时不限时）。"""
    from .tuning import seconds_text
    seconds = getattr(cfg, "explore_program_s", 15)
    return "单次规划程序不限时长；" if seconds is None else f"单次规划程序最多 {seconds_text(seconds)}；"


def cpu_rule(cfg) -> str:
    """探索模式提示词里最多可以用几个 CPU 核的一句：容器实际能用的核数（apply_cpu_limit 设置后），
    否则是可调参数 agent_cpus（默认本机的核数）。"""
    from .tuning import HOST_CPUS
    cpus = getattr(cfg, "agent_cpus_effective", None) or getattr(cfg, "agent_cpus", None) or HOST_CPUS
    return f"本次最多可以用 {cpus} 个核，用几个按实际环境决定"


# 探索模式里关于自写程序的几句按执行器区分：Codex 同时支持当前问题与持续建模，并行要点只写一句；
# Claude Code 执行器保留原来的"完全可以自由发挥"和详细的并行要点。
# 提示词里是 {preset_sim}{program_freedom}……{program_lead}{cpu_rule}{parallel_rule}
PROGRAM_FREEDOM = {"claude": "，但不限制你自行编写模拟器或搜索程序等，你完全可以自由发挥。",
                   "codex": "。你也可以自行编写模拟器、搜索或其他辅助程序，自由发挥。"}
# 自写工具用 C++、能用多线程就用多线程（用户 10-27 要求：Python 循环里逐个启动模拟器进程的穷举只用到不到一个核）
PROGRAM_LEAD = {"claude": ("编写模拟器、搜索、穷举或批量计算等工具时用 C++ 写，不要用 Python；有多线程就用多线程来缩短计算时间"
                           "（例如 g++ -O2 -pthread），同一批推演放在一个进程里做，不要逐个启动进程，"),
                "codex": ("辅助程序可以解决当前问题，也可以持续积累可复用的模型与方法。需要较多计算时用 C++ 写、不要用 Python，"
                          "有多线程就用多线程缩短计算时间，")}
PARALLEL_RULE = {"claude": ("。并行时注意，单线程做的汇总与合并容易成为瓶颈（搜得越深 CPU 占用越低）。"
                            "以按层展开的搜索为例：去重与择优也要分片并行，不要每层由一个线程串行处理越来越大的去重集合；"
                            "各线程把候选的分数、序号和去重键全部交回，不要各自先截取前几名；"
                            "选定保留的节点后，再由各线程从父节点重放生成。多线程版本要用同样的输入与单线程对比，确认结果完全一致。"),
                 "codex": "；避免频繁的线程间同步，用相同输入核对多线程与单线程结果。"}


def _engine_text(table: dict, cfg) -> str:
    return table["codex" if getattr(cfg, "engine", "claude") == "codex" else "claude"]


# 思考语句的语气（用户要求）：不必一直冷静理智，可以带情绪。两种执行器都有；提示词与 act、narrate 的说明里都写
THOUGHT_MOOD = ("思考语句的语气不必一直冷静理智，可以自然流露真实的情绪，比如找到好路线时的高兴、反复失败后的懊恼、"
               "面对难关时的紧张；但不要夸张，像正常人说话一样就好。")
THOUGHT_MOOD_SHORT = "语气不必一直冷静理智，可以自然带点情绪，但不要夸张。"


def thought_brief_rule(cfg) -> str:
    """Claude Code 执行器提示词里思考语句汉字数的一句（可调参数 thought_max_hanzi，见 orchestrator/thought.py）；
    Codex 执行器为空。开了"仅正式思考"时只说 act 的 thought（正式思考），临时思考的字数写在 formal_thought_rule 里。"""
    from .thought import hanzi_limit, temp_thoughts
    n = hanzi_limit(cfg)
    if n is None:
        return ""
    if temp_thoughts(cfg):
        return f"act 的 thought（正式思考）尽量简短，汉字不超过 {n} 个（英文术语不计）。"
    return f"思考语句尽量简短，汉字不超过 {n} 个（英文术语不计）。"


def temp_thought_where(cfg) -> str:
    """临时思考显示在哪里：游戏内 HUD 只显示正式思考；用网页 HUD（直播或 --no-hud-mod）时它的历史一栏也会列出临时思考。"""
    if getattr(cfg, "hud_mod", True):
        return "只显示在控制台，游戏画面的 HUD 和录像里都看不到"
    return "显示在控制台和网页 HUD 的历史里，但不算正式思考"


def formal_thought_rule(cfg) -> str:
    """开了"仅正式思考"（--formal-thought）时提示词里的说明；没开时为空（提示词与原来完全相同）。"""
    from .thought import hanzi_limit, temp_thoughts
    if not temp_thoughts(cfg):
        return ""
    n = hanzi_limit(cfg, temp=True)
    where = temp_thought_where(cfg)
    temp = (f"冻结期间的 narrate 与公开 commentary 是临时思考，{where}。" if n is None else
            f"冻结期间用 narrate 写的是临时思考，{where}；每条汉字不超过 {n} 个"
            "（英文术语不计），写你此刻的分析、尝试与判断，想法有进展就更新。")
    return ("本次开启了“仅正式思考”：游戏画面的 HUD 上只显示每次 act 的 thought（正式思考），它要概括这次冻结期间"
            "（从上一次 act 结束到这次 act）你整个的思考、分析和判断，而不只是这一段按键的意图。" + temp)


SAVE_CONTEXT_ACT = ("本次开启了节省上下文：同一房间里的 act 结果省略每次都一样的部分——观测文件只给一行（路径、帧号、快照编号，"
                    "覆盖不完整时才写明缺什么）、不列可选推理强度、没有僵直时不写僵直说明、不重复本房间范围；"
                    "进新房间时和 observe 照常给全。")
SAVE_CONTEXT_PRINT = "字符地图等大块输出只打印关心的区域，没变化的内容不必重复打印，需要时再读文件。"


def save_context_act(cfg) -> str:
    """开了"节省上下文"（--save-context）时 act 说明里的一句；没开时为空。"""
    return SAVE_CONTEXT_ACT if getattr(cfg, "save_context", False) else ""


def save_context_print(cfg) -> str:
    """开了"节省上下文"时探索提示词辅助程序一节的一句（只是建议，不加限制）；没开时为空。"""
    return SAVE_CONTEXT_PRINT if getattr(cfg, "save_context", False) else ""


def formal_thought_act(cfg) -> str:
    """act 工具说明里 thought 的补充（开了"仅正式思考"时）；没开时为空。"""
    from .thought import temp_thoughts
    if not temp_thoughts(cfg):
        return ""
    return ("\n  本次开启了“仅正式思考”：这里的 thought 是正式思考，游戏画面的 HUD 上只显示它；用它概括这次冻结期间"
            f"（从上一次 act 结束到现在）你整个的思考、分析和判断。冻结期间的临时思考用 narrate 写，{temp_thought_where(cfg)}。")


def thought_temp_limit(cfg) -> str:
    """开了"仅正式思考"时 narrate（临时思考）说明里的字数。"""
    from .thought import char_cap, hanzi_limit
    n = hanzi_limit(cfg, temp=True)
    return f"{char_cap(cfg, temp=True)} 字以内" if n is None else f"汉字不超过 {n} 个，英文术语不计"


def thought_brief(cfg) -> str:
    """act 工具说明里 thought 后的括注（Codex 执行器为空）。"""
    from .thought import hanzi_limit
    n = hanzi_limit(cfg)
    return "" if n is None else f"（尽量简短，汉字不超过 {n} 个，英文术语不计）"


def thought_limit(cfg) -> str:
    """narrate 工具说明里 thought 的字数：Claude Code 执行器写汉字数上限，Codex 执行器照旧写总长度上限。"""
    from .thought import hanzi_limit
    n = hanzi_limit(cfg)
    return (f"{getattr(cfg, 'thought_max_chars', 120)} 字以内" if n is None
            else f"尽量简短，汉字不超过 {n} 个，英文术语不计")


def explore_search_rule(cfg) -> str:
    """探索模式提示词里自写模拟器与搜索程序推演帧数的一句（explore_search_frames 为 None 时不写）。"""
    if not workspace_capabilities(cfg).programs:
        return ""
    frames = getattr(cfg, "explore_search_frames", 150)
    return "" if frames is None else f"自写的模拟器或搜索程序每次推演最多 {frames} 帧（从起始状态往后模拟的帧数）。"


def stiffness_rule(cfg) -> str:
    """探索模式开了思考僵直（--stiffness）时提示词里的说明；没开时为空。数字来自可调参数 stiff_unit_s 等。"""
    if not getattr(cfg, "stiffness", False):
        return ""
    unit = float(getattr(cfg, "stiff_unit_s", 150.0))
    unit_text = f"{unit / 60:g} 分钟" if unit >= 60 and (unit * 2) % 60 == 0 else f"{unit:g} 秒"
    think, tool = getattr(cfg, "stiff_think_s", 0.2), getattr(cfg, "stiff_tool_s", 0.4)
    return (f"本次开启了思考僵直：游戏冻结期间花掉的现实时间会按比例变成角色的僵直。从上一次 act 结束（本次运行的第一次从第一次调用游戏工具算起）"
            f"到下一次 act 执行，思考时间满 {unit_text}后，每 {unit_text}僵直 {think:g} 秒；运行工具（Bash 程序、观测等）的时间满 {unit_text}后，"
            f"每 {unit_text}另外僵直 {tool:g} 秒。两部分各自计算：不满 {unit_text}的那部分不僵直，满了之后按实际时长等比例计算。"
            "用户暂停、等待额度和上下文压缩的时间不算。"
            f"僵直发生在下一次 act 执行你的按键之前，不计入每次 act 的 {act_frame_cap(cfg)} 帧上限：角色保持上一帧的按键不变（例如一直按着方向或抓墙）；"
            "僵直期间死亡会立刻松开所有按键；死亡、进入别的房间、通关或进入剧情时僵直结束，这次 act 的按键（包括自动重试）都不执行；角色不可操作时（剧情等）不僵直。"
            "observe 的返回开头会写出这次冻结已累计的思考、工具时间和预计僵直；每次 act 的结果会写明这次冻结的思考、工具时间和实际僵直了多久。"
            "打算长时间思考或运行程序之前，先让角色停在保持当前按键也安全的位置。")


def fill(text: str, cfg) -> str:
    """把提示词里的占位符换成本次的值：可调参数（orchestrator/tuning.py；默认值下与原来的文字完全相同），
    以及随设置变化的游戏工具清单与剧情处理说明。"""
    from .tuning import seconds_text
    workspace = workspace_capabilities(cfg)
    if "{game_tool_list}" in text:
        text = text.replace("{game_tool_list}", game_tool_list(cfg))
    if "{preset_sim}" in text:     # 探索模式：开了限额预测时预设模拟器只能经 simulate 按额度用，否则完全不开放
        text = text.replace("{preset_sim}", "预设模拟器只能通过 simulate 按额度使用" if getattr(cfg, "sim_assist", False)
                            else "禁用预设模拟器")
    if "{auto_handling}" in text:
        text = text.replace("{auto_handling}", auto_handling(cfg))
    values = {
        "{tas_workspace_rule}": ("`/workspace/game/tas.py` 提供 `load(path,start_line=...)`、`window(document,offset,count)` 和 `to_sim(inputs)`。前两者只生成计划，不操作游戏；不执行 console/Set/Read 等命令，需显式选取纯输入区。模拟转换拒绝不能表达的菜单键，不忽略它们。"
                                  if workspace.programs else ""),
        "{tas_cursor_rule}": ("`input_cursor.next`是选定输入文档从0开始的帧偏移，不是源文件行号；用同一load文档和window(offset=next)生成下一窗，或把remaining_inputs写入仍声明celeste_tas的JSON。旧工具不能把剩余数组直接交给默认格式的inputs。"
                               if workspace.programs else "继续TAS输入时保留 input_format=celeste_tas，按返回的input_cursor.next与remaining_inputs核对输入进度；不要把TAS剩余数组当成默认格式的inputs。"),
        "{explore_reading_rule}": ("先读必读清单，再在 /workspace/notes.md 记录工具约定。help/ 里是操作提示，需要时随时可以读。"
                                   if workspace.workspace_read and workspace.workspace_write else
                                   "根据当前观测、工具说明与真实尝试学习；当前没有文件工具，不要求读取知识库或工作区笔记。"),
        "{explore_reference_rule}": ("基本规则见 /kb/basics.md，坐标与观测格式见 /kb/data.md，按键和工具约定见 /kb/tools.md。"
                                     if workspace.workspace_read else "按键和工具约定以本次提供的游戏工具说明为准。"),
        "{explore_environment_rule}": ("Read/Write/Bash 对应容器的文件读取、写入和断网命令；"
                                       if workspace.programs else "当前只有游戏工具，不能读取文件或运行程序；"),
        "{observation_delivery_rule}": ("具备脚本能力且当前观测文件核对成功时，默认正文只给状态、快照身份及文件位置；程序优先读取latest_obs.json的obs，完整碰撞数据不重复进入正文。文件不可用或规划工具受限时回退完整布局baseline/同房delta。"
                                        if workspace.programs else "首次入房或默认observe返回完整布局baseline，同房act/cutscene返回变化delta；快照链缺失时默认observe重建。"),
        "{observation_source_rule}": ("优先从 `/workspace/game/latest_obs.json` 的 `obs` 读取当前原始数据；默认正文只给状态、快照身份和文件位置；若回退布局则按实际格式读取。"
                                      "同类 JSON 查询反复出现时，将共用读取和筛选保存为工作区参数化脚本，后续用短命令复用；一次性小查询仍可直接执行"
                                      if workspace.workspace_read else "使用首次返回的布局和后续变化，缺少基线时默认 observe 重建"),
        "{geometry_program_rule}": ("确需计算时可用 `/workspace/game/collision_matrix.py` 读取原始快照、按需生成多来源矩阵文件或做基础几何相交查询；用法见同目录 `collision_matrix.md`。通行规则、实体交互与路线由你按具体场景自行写脚本分析，不预设某类来源始终阻挡；不要把完整矩阵打印进上下文，结论仍需用动作或模拟验证。"
                                    if workspace.programs else "根据观测中的矩形边界和角色尺寸核对候选通道，用短动作验证存在歧义的连接。"),
        "{geometry_record_rule}": ("在笔记或程序里保留已验证的通道、连接依据、失败假设及适用条件。"
                                   if workspace.workspace_write else "在公开解说中简短总结已验证的通道、连接依据与失败假设。"),
        "{act_max_frames}": str(act_frame_cap(cfg)),
        "{act_min_frames}": str(act_frame_min(cfg)),
        "{explore_search_rule}": explore_search_rule(cfg),
        "{cpu_rule}": cpu_rule(cfg),
        "{program_freedom}": _engine_text(PROGRAM_FREEDOM, cfg) if workspace.programs else "。",
        "{program_lead}": _engine_text(PROGRAM_LEAD, cfg),
        "{parallel_rule}": _engine_text(PARALLEL_RULE, cfg),
        "{stiffness_rule}": stiffness_rule(cfg),
        "{thought_brief_rule}": thought_brief_rule(cfg),
        "{thought_mood}": THOUGHT_MOOD,
        "{thought_mood_short}": THOUGHT_MOOD_SHORT,
        "{thought_brief}": thought_brief(cfg),
        "{thought_limit}": thought_limit(cfg),
        "{thought_temp_limit}": thought_temp_limit(cfg),
        "{formal_thought_rule}": formal_thought_rule(cfg),
        "{formal_thought_act}": formal_thought_act(cfg),
        "{save_context_rule}": save_context_act(cfg),
        "{save_context_print}": save_context_print(cfg),
        # 碰撞图什么时候附在工具结果里（--collision-image；默认 Codex 每次，其余只在进新房间与 observe 时）
        "{act_collision_image}": {
            "always": "和完整碰撞图（开启图像时）",
            "room": "；进入新房间时另附截图与完整碰撞图（开启图像时），同一房间里的碰撞图只写进工作区"
                    "（game/collision.json 的 image 指向的文件），需要时自己查看",
            "off": "；碰撞图不附在结果里，只写进工作区（game/collision.json 的 image 指向的文件），需要时自己查看",
        }[collision_image_mode(cfg)],
        "{observe_collision_image}": ("（碰撞图只写进工作区，不附在结果里）" if collision_image_mode(cfg) == "off"
                                      else "和完整碰撞图"),
        "{collision_image_when}": {
            "always": "每次观测与动作返回",
            "room": "进入新房间与 observe 时返回（同一房间里的动作只写进工作区）",
            "off": "工具结果不附图，工作区里写有",
        }[collision_image_mode(cfg)],
        "{temp_thought_where}": temp_thought_where(cfg),
        # 开了"仅正式思考"时换掉提示词里说 narrate 给观众看的几处；没开时与原来的文字完全相同
        "{narrate_target}": "写下临时思考" if getattr(cfg, "formal_thought", False) else "告诉观众",
        "{hud_thought_shown}": ("你最近一次 act 的 thought（正式思考）" if getattr(cfg, "formal_thought", False)
                                else "你最新写下的一句思考"),
        "{narrate_viewers}": ("。这些是临时思考（规则见前面“仅正式思考”的说明）：中文，" if getattr(cfg, "formal_thought", False)
                              else "——观众在你思考的这段时间里只能看到 `narrate` 写下的内容。规则与 `act` 的 `thought` 相同：一两句中文，"),
        "{bash_default}": seconds_text(getattr(cfg, "bash_default_s", 300)),
        "{bash_max}": seconds_text(getattr(cfg, "bash_max_s", 600)),
        "{explore_program_rule}": explore_program_rule(cfg),
        "{explore_program}": ("不限" if getattr(cfg, "explore_program_s", 15) is None
                              else seconds_text(getattr(cfg, "explore_program_s", 15))),
        "{playback_ratio}": str(getattr(cfg, "playback_ratio", 100)),
        "{playback_min_tracks}": str(getattr(cfg, "playback_min_tracks", 40)),
        "{thought_max}": str(getattr(cfg, "thought_max_chars", 120)),
        "{remind}": remind_text(getattr(cfg, "live_remind_s", 30.0)),
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


# 控制台"提示词"页保存的本机修改：local/prompts/<文件名>（不入库）。有这个文件时用它代替仓库里的同名文件，
# 删掉它就恢复仓库版本。这样更新仓库（git pull）不会和本机修改冲突，每次运行也会记下用了哪些本机修改。
OVERRIDES = REPO_ROOT / "local" / "prompts"


def prompt_files() -> list[str]:
    return sorted(p.name for p in PROMPTS.glob("*.md"))


def read_part(name: str) -> str:
    """提示词文件的当前内容：有本机修改时用本机修改。"""
    override = OVERRIDES / name
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return (PROMPTS / name).read_text(encoding="utf-8")


def overridden_parts() -> list[str]:
    known = set(prompt_files())
    return sorted(p.name for p in OVERRIDES.glob("*.md") if p.name in known) if OVERRIDES.is_dir() else []


@dataclass
class PromptPart:
    """提示词的一块：source 是来源文件（自动生成的部分为 None），sep 是它和前一块之间的分隔。"""
    label: str
    text: str
    source: str | None = None
    sep: str = ""


def _file_parts(name: str, transform=None, sep: str = "\n\n", slots=None) -> list[PromptPart]:
    """Expand explicit slots and the shared interface, retaining each source for preview."""
    text = read_part(name)
    if transform:
        text = transform(text)
    blocks = dict(slots or {})
    if '{interface}' in text:
        blocks['{interface}'] = [PromptPart('interface_zh.md', read_part('interface_zh.md'), 'interface_zh.md')]
    if not any(key in text for key in blocks):
        return [PromptPart(name, text, name, sep)]
    import re
    parts = []
    for i, piece in enumerate(re.split('(' + '|'.join(re.escape(key) for key in blocks) + ')', text)):
        if i % 2:
            parts.extend(blocks[piece])
        else:
            parts.append(PromptPart(name + ('（接上）' if i else ''), piece, name, sep if i == 0 else ''))
    return parts


def _speedrun_parts(cfg) -> list[PromptPart]:
    """速通模式的系统提示词。按预置模拟器的开关分成两个版本（目标都是以最短的章节计时通关）：
    - 开模拟器（有容器程序通道）：智能体可以随意调用 /sim 寻找并优化路线；提示词里写明做法、建议的工作方式、
      物理参数简表、规划与优化的要求、自动重试与规划回放（system_sim、container_sim、sim、free/pause、retry、playback）；
    - 关模拟器（--no-sim，或没有程序通道）：不教怎么玩，只写目标、规则（不得调用或破解模拟器、不读游戏程序）与工具接口，
      其余由智能体自己决定（system_nosim、container_nosim）；容器里另有展示接口（show.trajectory），算出的逐帧轨迹
      可以交给系统画给观众看，只用于展示。
    容器运行方式下都有 hud_zh.md：思考期间用 narrate 随时更新 HUD 的思考语句、运行程序期间报告进度；直播模式（live）
    另加 live_zh.md：观众实时观看、解说计时 10 秒、可按用户要求退出关卡，但不改变速通目标。"""
    workspace = workspace_capabilities(cfg)
    reuse = "之前写的程序与笔记都在工作区里，可以继续用。" if workspace.workspace_read else "以当前观测和已有对话核对进度后继续。"
    parts: list[PromptPart] = []
    if not (cfg.sim and workspace.programs):
        parts += _file_parts("system_nosim_zh.md", sep="")
        parts += _file_parts("container_nosim_zh.md" if workspace.programs else "host_zh.md")
    else:
        parts += _file_parts("system_sim_zh.md", sep="")
        parts += _file_parts("container_sim_zh.md") + _file_parts("sim_zh.md")
        # 默认不限制思考与调用（free_zh.md，可以把规划程序写成自动重试命令）；--limit-planning 时按章节记账（pause_zh.md）
        if cfg.pause_limit:
            n, d = cfg.rounds_per_room, cfg.death_bonus
            parts += _file_parts("pause_zh.md", lambda t: t.replace("{rounds4}", str(n * 4)).replace(
                "{rounds}", str(n)).replace("{death}", str(d)))
        else:
            parts += _file_parts("free_zh.md") + _file_parts("retry_zh.md")
        if cfg.playback:
            parts += _file_parts("playback_zh.md")
    if workspace.programs:
        parts += _file_parts("hud_zh.md")
        if cfg.live:
            parts += _file_parts("live_zh.md")
    from .segments import describe_plan, plan_is_partial
    if getattr(cfg, 'play_scope', 'chapter') == 'visited':
        pass  # shared free-exploration instructions below replace the traversal plan
    elif plan_is_partial(getattr(cfg, "plan", None)):
        parts.append(PromptPart("本次的范围（按段通关时自动加）",
                                "# 本次的范围\n\n" + describe_plan(cfg.plan, getattr(cfg, "checkpoints", {}) or {}, agent_navigation=cfg.cutscene_mode == 'agent')
                                + reuse, None, "\n\n"))
    elif cfg.chapters and len(cfg.chapters) > 1:
        names = " → ".join(chapter_name_by_id(c) for c in cfg.chapters)
        parts.append(PromptPart("本次的章节（多章时自动加）",
                                "# 本次的章节\n\n本次要依次通关：" + names + "。评价指标是各章章节计时之和。"
                                + ("一章结算并返回选章地图后，按主控报告的下一目标自行导航；主控只核对章节和起点。" if cfg.cutscene_mode == 'agent' else "一章通关后系统自动进入下一章，你接着玩。")
                                + reuse, None, "\n\n"))
    return parts


def prompt_parts(cfg) -> list[PromptPart]:
    """游玩提示词的各块，按 模式 × 直播 严格分版本（各执行器只另加自己的工具映射）：
    - 速通模式：开 / 关模拟器两个版本 + hud_zh；直播时再加 live_zh；
    - 探索模式：explore_zh（工具清单按本次注册的工具生成）；直播时再加 explore_live_zh。
    剧情模式的说明只在开放了 cutscene 工具时加（见 modes.has_cutscene_tool）。可调参数的占位符在这里按本次的值填入。"""
    if is_explore(cfg):
        tools = "/".join(game_tools(cfg))
        workspace = workspace_capabilities(cfg)
        slots = {
            '{explore_notes}': _file_parts('explore_notes_zh.md') if workspace.workspace_read and workspace.workspace_write else [],
            '{explore_programs}': _file_parts('explore_programs_zh.md') if workspace.programs else [],
        }
        from .detour import SCOPE_FROM, SCOPE_TO, enabled as detour_enabled
        detour_scope = (lambda t: t.replace(SCOPE_FROM, SCOPE_TO)) if detour_enabled(cfg) else (lambda t: t)
        parts = _file_parts("explore_zh.md", lambda t: detour_scope(t.replace("{game_tools}", tools)), sep="", slots=slots)
        if cfg.live:
            parts += _file_parts("explore_live_zh.md")
        chapters = cfg.chapters or ([cfg.chapter] if cfg.chapter is not None else [])
        from .segments import describe_plan, plan_is_partial
        if getattr(cfg, 'play_scope', 'chapter') == 'visited':
            pass
        elif plan_is_partial(getattr(cfg, "plan", None)):
            parts.append(PromptPart("本次指定范围（按段通关时自动加）",
                                    describe_plan(cfg.plan, getattr(cfg, "checkpoints", {}) or {}, speedrun=False, agent_navigation=cfg.cutscene_mode == 'agent')
                                    + ("计划不得自行扩展，计划外地图只作绕行（见“计划外地图”一节）。" if detour_enabled(cfg)
                                       else "不得自行扩展。"), None, "\n"))
        elif chapters:
            parts.append(PromptPart("本次指定章节（自动加）", "本次指定章节：" + "、".join(chapter_name_by_id(c) for c in chapters)
                                    + ("；计划不得自行扩展，计划外地图只作绕行（见“计划外地图”一节）。" if detour_enabled(cfg)
                                       else "；不得自行扩展。"), None, "\n"))
    else:
        parts = _speedrun_parts(cfg)
    if getattr(cfg, 'play_scope', 'chapter') == 'visited':
        parts.append(PromptPart("已到访地图自由探索", "范围以本次主控保存的已到访地图许可集合为准，区分 A/B/C 面；先 observe 查看允许地图。可以通过原生菜单反复进出允许的地图、选择已解锁检查点，按操作权限做观测实验和寻找收集品。不要进入未授权地图；通关不扩大范围，主控不会代按选章。", None, "\n\n"))
    if getattr(cfg, 'completion_policy', 'stop') == 'continue':
        parts.append(PromptPart("通关后继续", "本条优先于通用提示词中的通关收尾约定：通关不是本次任务的停止条件。结算和选章由你显式操作；计划结束后仍在许可地图内继续探索、建模或收集，直到用户目标触发、用户停止或出现真正阻塞。可用 observe(detail='time') 查询时间和目标剩余量。", None, "\n\n"))
    parts += _file_parts("room_analysis_zh.md")
    if workspace_capabilities(cfg).workspace_read:
        threshold = getattr(cfg, 'notes_warn_percent', 60)
        before = (f"已知上下文窗口占用超过 {threshold:g}% 且本监测周期未检测到笔记内容更新时提醒一次。" if threshold else "前置整理提醒已关闭。")
        parts.append(PromptPart("笔记维护提醒", "主控在压缩完成后通过后续工具交互提醒读取 /workspace/notes.md；" + before
                                + "提醒不代替主动维护，不代表笔记已经读过或整理过，也不要求机械重写。遵守当前暂停和仅聊天状态。", None, "\n\n"))
    if cfg.cutscene_mode == 'agent':
        parts += _file_parts("agent_controls_zh.md")
    from .action_policy import permissions_for
    from .detour import enabled as detour_enabled, instructions as detour_instructions, map_rule
    parts.append(PromptPart("游戏操作权限", permissions_for(cfg).instructions(experiments=getattr(cfg, 'play_scope', 'chapter') == 'visited'), None, "\n\n"))
    if detour_enabled(cfg):
        parts.append(PromptPart("计划外地图（开了才加）", detour_instructions(cfg), None, "\n\n"))
    if 'exit_level' in game_tools(cfg):
        parts.append(PromptPart("返回地图与结束任务",
                                "用户要求返回地图时调用 exit_level，默认保留任务、后续章节和录制；返回后 observe，"
                                f"只进入当前计划或许可允许的地图，{map_rule(cfg)}。"
                                "只有用户明确结束整次试玩才设 finish_run=true。按当前操作权限执行，不用保存退出绕过返回地图禁令；返回地图不算通关。", None, "\n\n"))
    if workspace_capabilities(cfg).programs and getattr(cfg, "live", False):
        parts += _file_parts("program_batches_zh.md")     # 长程序分批反馈只用于直播模式（用户要求）
    if has_cutscene_tool(cfg):
        parts.append(PromptPart("剧情模式说明（有剧情工具时自动加）",
                                f"当前剧情模式：{cfg.cutscene_mode}。auto由主控自动跳过；agent由你显式操作，无需wait："
                                "skip尝试跳过，watch只播放已识别动画到下一输入节点或恢复控制，每次最多600帧，未知状态立即交回，不重复空等；"
                                "advance确认对白、明信片、旧纸一页或confirm画面；分支先读选项再choose，不用advance代选。"
                                "教学和breathing用act，角色可操作时不因inCutscene标记停下；旧纸U/D翻页、B关闭。", None, "\n"))
    if is_explore(cfg) and workspace_capabilities(cfg).programs and getattr(cfg, "explore_program_runs", None):
        parts.append(PromptPart("两次 act 之间的次数（设了才自动加）",
                                f"两次 act 之间最多运行 {cfg.explore_program_runs} 次规划程序（Bash），用完后先做一次真实尝试。",
                                None, "\n"))
    if cfg.sim_assist:
        from .sim_assist import AssistRules
        parts.append(PromptPart("有限模拟辅助（开了限额预测时自动加）",
                                AssistRules.of(cfg).instructions(
                                    'celeste_simulate' if getattr(cfg, 'engine', None) == 'codex' else 'simulate'
                                ).lstrip("\n"), None, "\n\n"))
    parts.append(PromptPart("工具异常与任务阻塞", "首次遇到大量编码串（如Base64）、乱码、无信息重复或关键内容截断时，公开报告工具名、现象及影响；不编造token数，不粘贴大块原文，不把编码当作已看到图像，不反复调用堆积。能从可靠局部查询或原始文件恢复时继续；信息不可靠或缺少必要能力导致任务无法继续时，调用stop_run说明原因，主控冻结暂停，结束本轮等待用户。正常长数据、普通死亡和仍可尝试的难点不算阻塞，也不能把阻塞当作通关。", None, "\n\n"))
    for part in parts:
        part.text = fill(part.text, cfg)
    return parts


def play_prompt(cfg) -> str:
    """游玩提示词全文（见 prompt_parts）。"""
    return "".join(part.sep + part.text for part in prompt_parts(cfg))


def first_play_prompt(cfg):
    from .button_prompts import notification
    return notification('start_explore' if getattr(cfg, 'completion_policy', 'stop') == 'continue' else 'start_play')


def continue_play_prompt(cfg):
    from .button_prompts import notification
    return notification('continue_play')


def controller_turn_prompt(text):
    """Label controller-generated turn fallbacks without relabeling real user input."""
    from .button_prompts import notification
    return notification('continue_prepare', facts=text)


def study_prompt(study: KbStudy, sim: bool = False, *, prepare_only: bool = False) -> str:
    """准备阶段的第一条消息。仅准备时不开始试玩（结束方式见 agent._lifecycle_prompt，这里不重复）；
    准备后接着试玩时写明读完后的下一步，开了预置模拟器的速通另外要求先想清技巧与要写的程序并记进笔记。"""
    from .button_prompts import notification
    files = "\n".join(f"- {path}" for path in study.container_paths())
    sim_note = "本次预置模拟器已开放，资料位于 /sim。\n" if sim else ""
    if prepare_only:
        return notification('prepare', facts=(
            f"本次必读清单：\n{files}\n\n主控核对实际阅读进度。\n" + sim_note
            + f"准备阶段最多 {study.minutes:g} 分钟，到时仍未读完则本次准备未完成。"))
    after = ("读完后，结合知识库想清楚：哪些技巧能在本章用来缩短游戏内计时、它们的输入写法与帧窗口，以及你要写哪些搜索与规划程序"
             "（模拟器直接用 /sim 预置的）。把要点整理进 /workspace/notes.md（工作区里已有的笔记和程序先看一遍），然后调用 observe 开始游玩。\n"
             if sim else "读完后调用 observe 开始游玩。\n")
    return notification('prepare', facts=(
        f"本次必读清单：\n{files}\n\n"
        "主控核对实际阅读进度，完成前 observe 与 act 不会执行。\n" + sim_note + after +
        f"准备阶段最多 {study.minutes:g} 分钟，到时游戏工具自动开放。"
        "游戏在你思考期间一直暂停，章节计时只算游戏真正推进的帧。"
    ))


def next_chapter_prompt(cfg, fresh_session: bool = False) -> str:
    """续跑一次已经全部通关的运行、追加了新的章节时（run.py 的 --resume … --chapters）发出的第一条消息。
    不是中途被打断的续跑，不能说"上一轮被结束、游戏一直冻结"；会话里还有旧规则，提醒以当前系统提示词为准。"""
    from .segments import piece_label
    table = getattr(cfg, "checkpoints", None) or {}
    items = "、".join(piece_label(p, table) for p in (cfg.plan or [])) \
        or "、".join(chapter_name_by_id(c) for c in (cfg.chapters or []))
    workspace = workspace_capabilities(cfg)
    fresh_context = "这是一个新的会话：之前的对话不在了。"
    if workspace.workspace_read:
        fresh_context += "工作区里的笔记与程序仍保留。"
    head = ("（系统）之前指定的章节都已经通关。"
            + (fresh_context if fresh_session else "现在接着同一个会话，"))
    rules = ("本次的规则以当前的系统提示词为准" if fresh_session or getattr(cfg, 'engine', 'claude') == 'codex'
             else "本次以这条消息后面的配置与指南版本为准" if workspace.workspace_read
             else "本次的规则以这条消息后面附上的当前规则为准")
    changed = "单次 act 与自写推演的帧数上限、思考语句的要求" if workspace.programs else "单次 act 的帧数上限、思考语句的要求"
    reuse = "之前写的程序与笔记按新规则检查后再用。" if workspace.workspace_read else "按当前规则核对后继续。"
    entry = ("已进入存档，请先观察当前界面，再自行导航到本次指定的章节与起点。" if cfg.cutscene_mode == 'agent'
             else "主控已经进入这一章。")
    from .button_prompts import notification
    return notification('next_chapter', facts=(head + f"开始新的范围：{items}。{entry}{rules}，和之前相比可能有变化"
            + f"（例如{changed}），{reuse}"
            "请先调用 observe 看清当前画面，再开始游玩。"))


def current_rules_note(cfg, *, guide=None) -> str:
    """续跑同一个 Claude Code 会话时附在第一条消息后面的当前规则。Claude Code 压缩上下文时会把当时的系统提示词与工具说明
    存成快照（prompt_snapshot），之后续跑沿用快照，要到下一次压缩才换成新的；所以续跑时改过的规则要在对话里再给一遍。"""
    # Codex thread/resume replaces baseInstructions. Only Claude needs a copy in history.
    if getattr(cfg, 'engine', 'claude') == 'codex':
        return ""
    from .startup_prompt import runtime_prompt
    if workspace_capabilities(cfg).workspace_read:
        return '\n\n（系统）续跑配置与指南如下，当前配置及指南内容替代旧快照中的任务与操作约定：\n' + runtime_prompt(cfg, guide=guide)
    return ("\n\n（系统）续跑的会话沿用之前保存的提示词快照：你看到的系统提示词与工具说明可能还是旧版本，要到下次压缩上下文后才会更新。"
            f"当前生效的完整规则如下，与旧版本不一致时以这里为准；act 一次至少推进 {act_frame_min(cfg)} 帧、最多 {act_frame_cap(cfg)} 帧，不在范围内会被主控退回。\n\n"
            + runtime_prompt(cfg, guide=guide))


def net_resume_prompt(minutes: float, chat: bool = False, study_note: str | None = None) -> str:
    """模型连不上、主控暂停等待之后，网络恢复时接着同一个会话发出的第一条消息。"""
    text = (f"（系统）刚才模型连不上（断网或接口不可用），主控暂停了智能体约 {max(minutes, 0):.0f} 分钟，现在已经恢复，"
            "接着同一个会话继续。中断前已经完成的工具调用结果都在对话里；最后那次回复没有送达，需要的话重新想一遍。")
    from .button_prompts import notification
    if chat:
        return notification('wake_chat', facts=text + "请接着处理用户刚才的消息。")
    text += "这段时间游戏一直冻结，章节计时没有走，也不计入思考僵直。"
    if study_note:
        return notification('resume_prepare', facts=text + "准备阶段还没有结束：\n" + study_note)
    return notification('reconnect', facts=text)


def resume_prompt(reason: str, minutes: float, study_note: str | None = None) -> str:
    """Claude Code 进程被结束后，用 --resume 接着同一个会话时发出的第一条消息。"""
    text = (f"（系统）{reason}，你的上一轮在中途被结束，暂停了约 {minutes:.0f} 分钟，现在接着同一个会话继续。"
            "这段时间游戏一直冻结，章节计时没有走；但如果被结束时有一个 act 正在执行，它已经执行完了，"
            "结果没能送回给你。")
    from .button_prompts import notification
    if study_note:
        return notification('resume_prepare', facts=text + "准备阶段还没有结束：\n" + study_note)
    return notification('reconnect', facts=text)
