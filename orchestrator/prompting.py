"""Shared play, knowledge preparation and resume prompts."""
from .config import REPO_ROOT, chapter_name_by_id
from .kb_study import KbStudy

PROMPTS = REPO_ROOT / "orchestrator" / "prompts"


def compose_prompt(runtime: str, sim: bool = False, chapters: list[int] | None = None, pause_limit: bool = False,
                   rounds_per_room: int = 3, death_bonus: int = 1, playback: bool = False, live: bool = False) -> str:
    """系统提示词。按预置模拟器的开关分成两个版本（目标都是以最短的章节计时通关）：
    - 开模拟器（默认，容器运行方式）：智能体可以随意调用 /sim 寻找并优化路线；提示词里写明做法、建议的工作方式、
      物理参数简表、规划与优化的要求、自动重试与规划回放（system_sim、container_sim、sim、free/pause、retry、playback）；
    - 关模拟器（--no-sim，或本机运行方式）：不教怎么玩，只写目标、规则（不得调用或破解模拟器、不读游戏程序）与工具接口，
      其余由智能体自己决定（system_nosim、container_nosim）；容器里另有展示接口（show.trajectory），算出的逐帧轨迹
      可以交给系统画给观众看，只用于展示。
    实时展示（live，两个版本都适用）：要求思考期间用 narrate 随时更新 HUD 的思考语句、运行程序期间报告进度（live_zh.md）。"""
    interface = (PROMPTS / "interface_zh.md").read_text(encoding="utf-8")

    def part(name: str) -> str:
        return (PROMPTS / name).read_text(encoding="utf-8")

    if not (sim and runtime == "container"):
        text = part("system_nosim_zh.md").replace("{interface}", interface)
        text += "\n\n" + part("container_nosim_zh.md" if runtime == "container" else "host_zh.md")
    else:
        text = part("system_sim_zh.md").replace("{interface}", interface)
        text += "\n\n" + part("container_sim_zh.md") + "\n\n" + part("sim_zh.md")
        # 默认不限制思考与调用（free_zh.md，可以把规划程序写成自动重试命令）；--limit-planning 时按章节记账（pause_zh.md）
        if pause_limit:
            text += "\n\n" + part("pause_zh.md").replace(
                "{rounds4}", str(rounds_per_room * 4)).replace("{rounds}", str(rounds_per_room)).replace("{death}", str(death_bonus))
        else:
            text += "\n\n" + part("free_zh.md")
            text += "\n\n" + part("retry_zh.md")
        if playback:
            text += "\n\n" + part("playback_zh.md")
    if live and runtime == "container":
        text += "\n\n" + part("live_zh.md")
    if chapters and len(chapters) > 1:
        names = " → ".join(chapter_name_by_id(c) for c in chapters)
        text += ("\n\n# 本次的章节\n\n本次要依次通关：" + names + "。评价指标是各章章节计时之和。"
                 "一章通关后系统自动进入下一章，你接着玩；之前写的程序与笔记都在工作区里，可以继续用。")
    return text


def play_prompt(cfg) -> str:
    """Common play policy; each runtime adds only its tool/protocol mapping."""
    if cfg.knowledge_profile == 'explore':
        prompt = (PROMPTS / 'explore_zh.md').read_text(encoding='utf-8')
        prompt += '\n本次指定章节：' + str(cfg.chapters) + '；不得自行扩展。'
    else:
        prompt = compose_prompt(cfg.runtime, cfg.sim, cfg.chapters, cfg.pause_limit,
                                cfg.rounds_per_room, cfg.death_bonus, cfg.playback, cfg.live)
    prompt += (f'\n当前剧情模式：{cfg.cutscene_mode}。auto由主控自动跳过；agent由你使用cutscene的'
               'wait/advance/choose/skip决定。遇到分支必须明确choose，不得用advance代选。')
    if cfg.sim_assist:
        from .sim_assist import INSTRUCTIONS
        prompt += INSTRUCTIONS
    return prompt


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
