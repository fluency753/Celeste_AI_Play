"""Shared play, knowledge preparation and resume prompts."""
from dataclasses import dataclass

from .config import REPO_ROOT, chapter_name_by_id
from .kb_study import KbStudy
from .modes import act_frame_cap, game_tools, has_cutscene_tool, is_explore

PROMPTS = REPO_ROOT / "orchestrator" / "prompts"


def remind_text(seconds: float) -> str:
    return "半分钟" if seconds == 30 else f"{seconds:g} 秒"


# 速通模式的系统提示词里逐个列出本次注册的游戏工具（随直播、剧情、限额预测等设置变化，见 modes.game_tools）
TOOL_NOTES = {
    "observe": "查看当前帧，不推进游戏",
    "act": "推进若干帧",
    "narrate": "思考期间更新观众看到的思考，不推进游戏",
    "set_effort": "调整你自己的推理强度",
    "stop_run": "任务阻塞时说明原因报告给用户，主控暂停试玩等用户处理",
    "exit_level": "用户要求退出时保存并退出关卡",
    "cutscene": "在主控交回的输入节点推进、选择或跳过剧情",
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
        return ("屏幕转场、死亡动画与重生由系统自动推进。剧情按当前的剧情模式处理：auto 时系统自动推进并跳过；"
                "agent 时主控自动播放到需要输入的节点；对白用 `cutscene` 推进，分支明确选择，教学用 `act`，无需 wait。剧情标记不等于失控，角色可操作时继续用 `act`。")
    return "剧情对话、屏幕转场、死亡动画与重生都由系统自动推进并跳过，`act` 返回时 Madeline 已经可以操作。"


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


LOCAL_TARGET_RULE = "无法确认后半段时，先验证下一个局部目标，用短动作或限额模拟消除歧义，不必长时间推完整条路线。"


def local_target_rule(cfg) -> str:
    """探索模式粗路线约定的最后一句：只给原生 GPT（codex 执行器）；Claude Code 执行器的智能体不加（用户要求）。"""
    return LOCAL_TARGET_RULE if getattr(cfg, "engine", "claude") == "codex" else ""


# 思考语句的语气（用户要求）：不必一直冷静理智，可以带情绪。两种执行器都有；提示词与 act、narrate 的说明里都写
THOUGHT_MOOD = ("思考语句的语气不必一直冷静理智，可以自然流露真实的情绪，比如找到好路线时的高兴、反复失败后的懊恼、"
               "面对难关时的紧张；但不要夸张，像正常人说话一样就好。")
THOUGHT_MOOD_SHORT = "语气不必一直冷静理智，可以自然带点情绪，但不要夸张。"


def thought_brief_rule(cfg) -> str:
    """Claude Code 执行器提示词里思考语句汉字数的一句（可调参数 thought_max_hanzi，见 orchestrator/thought.py）；
    Codex 执行器为空。"""
    from .thought import hanzi_limit
    n = hanzi_limit(cfg)
    return "" if n is None else f"思考语句尽量简短，汉字不超过 {n} 个（英文术语不计）。"


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
    frames = getattr(cfg, "explore_search_frames", 150)
    return "" if frames is None else f"自写的模拟器或搜索程序每次推演最多 {frames} 帧（从起始状态往后模拟的帧数）。"


def fill(text: str, cfg) -> str:
    """把提示词里的占位符换成本次的值：可调参数（orchestrator/tuning.py；默认值下与原来的文字完全相同），
    以及随设置变化的游戏工具清单与剧情处理说明。"""
    from .tuning import seconds_text
    if "{game_tool_list}" in text:
        text = text.replace("{game_tool_list}", game_tool_list(cfg))
    if "{preset_sim}" in text:     # 探索模式：开了限额预测时预设模拟器只能经 simulate 按额度用，否则完全不开放
        text = text.replace("{preset_sim}", "预设模拟器只能通过 simulate 按额度使用" if getattr(cfg, "sim_assist", False)
                            else "禁用预设模拟器")
    if "{auto_handling}" in text:
        text = text.replace("{auto_handling}", auto_handling(cfg))
    values = {
        "{act_max_frames}": str(act_frame_cap(cfg)),
        "{explore_search_rule}": explore_search_rule(cfg),
        "{cpu_rule}": cpu_rule(cfg),
        "{local_target_rule}": local_target_rule(cfg),
        "{thought_brief_rule}": thought_brief_rule(cfg),
        "{thought_mood}": THOUGHT_MOOD,
        "{thought_mood_short}": THOUGHT_MOOD_SHORT,
        "{thought_brief}": thought_brief(cfg),
        "{thought_limit}": thought_limit(cfg),
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


def _file_parts(name: str, transform=None, sep: str = "\n\n") -> list[PromptPart]:
    """一个提示词文件；system_*.md 里的 {interface} 处插入 interface_zh.md（拆成前、接口、后三块）。"""
    text = read_part(name)
    if transform:
        text = transform(text)
    if "{interface}" not in text:
        return [PromptPart(name, text, name, sep)]
    pieces = text.split("{interface}")
    interface = read_part("interface_zh.md")
    out = [PromptPart(name, pieces[0], name, sep)]
    for rest in pieces[1:]:
        out.append(PromptPart("interface_zh.md", interface, "interface_zh.md"))
        out.append(PromptPart(name + "（接上）", rest, name))
    return out


def _speedrun_parts(cfg) -> list[PromptPart]:
    """速通模式的系统提示词。按预置模拟器的开关分成两个版本（目标都是以最短的章节计时通关）：
    - 开模拟器（默认，容器运行方式）：智能体可以随意调用 /sim 寻找并优化路线；提示词里写明做法、建议的工作方式、
      物理参数简表、规划与优化的要求、自动重试与规划回放（system_sim、container_sim、sim、free/pause、retry、playback）；
    - 关模拟器（--no-sim，或本机运行方式）：不教怎么玩，只写目标、规则（不得调用或破解模拟器、不读游戏程序）与工具接口，
      其余由智能体自己决定（system_nosim、container_nosim）；容器里另有展示接口（show.trajectory），算出的逐帧轨迹
      可以交给系统画给观众看，只用于展示。
    容器运行方式下都有 hud_zh.md：思考期间用 narrate 随时更新 HUD 的思考语句、运行程序期间报告进度；直播模式（live）
    另加 live_zh.md：观众实时观看、解说计时 10 秒、可按用户要求退出关卡，但不改变速通目标。"""
    runtime = cfg.runtime
    parts: list[PromptPart] = []
    if not (cfg.sim and runtime == "container"):
        parts += _file_parts("system_nosim_zh.md", sep="")
        parts += _file_parts("container_nosim_zh.md" if runtime == "container" else "host_zh.md")
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
    if runtime == "container":
        parts += _file_parts("hud_zh.md")
        if cfg.live:
            parts += _file_parts("live_zh.md")
    from .segments import describe_plan, plan_is_partial
    if plan_is_partial(getattr(cfg, "plan", None)):
        parts.append(PromptPart("本次的范围（按段通关时自动加）",
                                "# 本次的范围\n\n" + describe_plan(cfg.plan, getattr(cfg, "checkpoints", {}) or {})
                                + "之前写的程序与笔记都在工作区里，可以继续用。", None, "\n\n"))
    elif cfg.chapters and len(cfg.chapters) > 1:
        names = " → ".join(chapter_name_by_id(c) for c in cfg.chapters)
        parts.append(PromptPart("本次的章节（多章时自动加）",
                                "# 本次的章节\n\n本次要依次通关：" + names + "。评价指标是各章章节计时之和。"
                                "一章通关后系统自动进入下一章，你接着玩；之前写的程序与笔记都在工作区里，可以继续用。", None, "\n\n"))
    return parts


def prompt_parts(cfg) -> list[PromptPart]:
    """游玩提示词的各块，按 模式 × 直播 严格分版本（各执行器只另加自己的工具映射）：
    - 速通模式：开 / 关模拟器两个版本 + hud_zh；直播时再加 live_zh；
    - 探索模式：explore_zh（工具清单按本次注册的工具生成）；直播时再加 explore_live_zh。
    剧情模式的说明只在开放了 cutscene 工具时加（见 modes.has_cutscene_tool）。可调参数的占位符在这里按本次的值填入。"""
    if is_explore(cfg):
        tools = "/".join(game_tools(cfg))
        parts = _file_parts("explore_zh.md", lambda t: t.replace("{game_tools}", tools), sep="")
        if cfg.live:
            parts += _file_parts("explore_live_zh.md")
        chapters = cfg.chapters or ([cfg.chapter] if cfg.chapter is not None else [])
        from .segments import describe_plan, plan_is_partial
        if plan_is_partial(getattr(cfg, "plan", None)):
            parts.append(PromptPart("本次指定范围（按段通关时自动加）",
                                    describe_plan(cfg.plan, getattr(cfg, "checkpoints", {}) or {}, speedrun=False)
                                    + "不得自行扩展。", None, "\n"))
        elif chapters:
            parts.append(PromptPart("本次指定章节（自动加）", "本次指定章节：" + "、".join(chapter_name_by_id(c) for c in chapters)
                                    + "；不得自行扩展。", None, "\n"))
    else:
        parts = _speedrun_parts(cfg)
    if has_cutscene_tool(cfg):
        parts.append(PromptPart("剧情模式说明（有剧情工具时自动加）",
                                f"当前剧情模式：{cfg.cutscene_mode}。auto由主控自动跳过；agent模式由主控自动播放到输入节点，无需wait；"
                                "用advance/choose/skip决定，教学用act。遇到分支必须明确choose，不得用advance代选。", None, "\n"))
    if is_explore(cfg) and getattr(cfg, "explore_program_runs", None):
        parts.append(PromptPart("两次 act 之间的次数（设了才自动加）",
                                f"两次 act 之间最多运行 {cfg.explore_program_runs} 次规划程序（Bash），用完后先做一次真实尝试。",
                                None, "\n"))
    if cfg.sim_assist:
        from .sim_assist import AssistRules
        parts.append(PromptPart("有限模拟辅助（开了限额预测时自动加）",
                                AssistRules.of(cfg).instructions().lstrip("\n"), None, "\n\n"))
    parts.append(PromptPart("任务阻塞处理", "任务因工具故障、缺少必要能力等无法继续时，调用 stop_run(reason=明确的阻塞原因) 报告给用户：主控暂停试玩、游戏保持冻结，由用户决定继续还是停止；然后结束本轮等待用户。不要无限重试失败工具，也不要把阻塞当作通关；普通死亡和仍可尝试的难点继续正常游玩。", None, "\n\n"))
    for part in parts:
        part.text = fill(part.text, cfg)
    return parts


def play_prompt(cfg) -> str:
    """游玩提示词全文（见 prompt_parts）。"""
    return "".join(part.sep + part.text for part in prompt_parts(cfg))


FIRST_PROMPT = "开始游玩。先调用 observe 看清当前画面，然后用 act 推进，直到通关。"


def study_prompt(study: KbStudy, sim: bool = False) -> str:
    files = "\n".join(f"- {path}" for path in study.container_paths())
    return (
        "开始游玩之前，先用 Read 工具把知识库从头到尾完整读一遍，按下面的顺序：\n"
        f"{files}\n\n"
        "要求：每个文件都读到最后一行。文件长时 Read 会按长度截断，看到截断就用 offset 与 limit 接着读，"
        "不要只读开头，也不要用 Grep 或 Bash 代替阅读。同一资料的翻译副本不需要重复阅读。"
        "主控会核对你读过的行，读完之前 observe 与 act 不会执行。\n\n"
        + ("读完后，结合知识库想清楚：哪些技巧能在本章用来缩短游戏内计时、它们的输入写法与帧窗口，以及你要写哪些搜索与规划程序"
           "（模拟器直接用 /sim 预置的）。把要点整理进 /workspace/notes.md（工作区里已有的笔记和程序先看一遍），然后调用 observe 开始游玩。\n"
           if sim else "读完后调用 observe 开始游玩。\n") +
        f"准备阶段最多 {study.minutes:g} 分钟，到时游戏工具自动开放。"
        "游戏在你思考期间一直暂停，章节计时只算游戏真正推进的帧。"
    )
CONTINUE_PROMPT = "本章还没有通关，请继续调用 act 游玩。"


def resume_prompt(reason: str, minutes: float, study_note: str | None = None) -> str:
    """Claude Code 进程被结束后，用 --resume 接着同一个会话时发出的第一条消息。"""
    text = (f"（系统）{reason}，你的上一轮在中途被结束，暂停了约 {minutes:.0f} 分钟，现在接着同一个会话继续。"
            "这段时间游戏一直冻结，章节计时没有走；但如果被结束时有一个 act 正在执行，它已经执行完了，"
            "结果没能送回给你。")
    if study_note:
        return text + "准备阶段还没有结束：\n" + study_note
    return text + "请先调用 observe 看清当前这一帧（工作区里的 game/ 文件也已更新），再继续游玩。"
