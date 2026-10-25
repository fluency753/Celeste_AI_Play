"""Shared play, knowledge preparation and resume prompts."""
from dataclasses import dataclass

from .config import REPO_ROOT, chapter_name_by_id
from .kb_study import KbStudy
from .modes import game_tools, has_cutscene_tool, is_explore

PROMPTS = REPO_ROOT / "orchestrator" / "prompts"


def remind_text(seconds: float) -> str:
    return "半分钟" if seconds == 30 else f"{seconds:g} 秒"


def fill(text: str, cfg) -> str:
    """把提示词里可调参数的占位符换成本次的值（orchestrator/tuning.py；默认值下与原来的文字完全相同）。"""
    from .tuning import seconds_text
    values = {
        "{act_max_frames}": str(getattr(cfg, "max_frames_per_act", 600)),
        "{bash_default}": seconds_text(getattr(cfg, "bash_default_s", 300)),
        "{bash_max}": seconds_text(getattr(cfg, "bash_max_s", 600)),
        "{explore_program}": seconds_text(getattr(cfg, "explore_program_s", 15)),
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
    if cfg.chapters and len(cfg.chapters) > 1:
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
        if chapters:
            parts.append(PromptPart("本次指定章节（自动加）", "本次指定章节：" + "、".join(chapter_name_by_id(c) for c in chapters)
                                    + "；不得自行扩展。", None, "\n"))
    else:
        parts = _speedrun_parts(cfg)
    if has_cutscene_tool(cfg):
        parts.append(PromptPart("剧情模式说明（有剧情工具时自动加）",
                                f"当前剧情模式：{cfg.cutscene_mode}。auto由主控自动跳过；agent由你使用cutscene的"
                                "wait/advance/choose/skip决定。遇到分支必须明确choose，不得用advance代选。", None, "\n"))
    if is_explore(cfg) and getattr(cfg, "explore_program_runs", None):
        parts.append(PromptPart("两次 act 之间的次数（设了才自动加）",
                                f"两次 act 之间最多运行 {cfg.explore_program_runs} 次规划程序（Bash），用完后先做一次真实尝试。",
                                None, "\n"))
    if cfg.sim_assist:
        from .sim_assist import AssistRules
        parts.append(PromptPart("有限模拟辅助（开了限额预测时自动加）",
                                AssistRules.of(cfg).instructions().lstrip("\n"), None, "\n\n"))
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
