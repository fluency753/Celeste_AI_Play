"""游玩模式：速通模式与探索模式二选一，直播模式可以和任一个叠加。

- 速通模式（speedrun）：以最短的章节计时通关。完整知识库（kb/），可选是否开放预置模拟器（--sim / --no-sim）。
- 探索模式（explore）：新手探索、学习并完成指定章节。只读 kb/explore（基本操作与工具说明），不开放模拟器文件，
  可选有额度的短路线预测（--sim-assist）。
- 直播模式（--live）：观众实时观看。HUD 默认是网页形式（可交互的直播版），解说计时 10 秒，可以按用户要求退出关卡；
  速通 + 直播不改变速通目标，探索 + 直播另加探索的直播节奏。不开直播时 HUD 默认以 MOD 形式显示在游戏画面里。

智能体在所有模式下都有 narrate（思考期间更新 HUD），提示词按 模式 × 直播 严格分版本（见 prompting.py）。
"""
from __future__ import annotations

SPEEDRUN = "speedrun"
EXPLORE = "explore"
MODES = (SPEEDRUN, EXPLORE)
MODE_NAMES = {SPEEDRUN: "速通模式", EXPLORE: "探索模式"}

# 旧参数 --knowledge 的取值与模式一一对应（full = 速通的完整知识库）
KNOWLEDGE_OF_MODE = {SPEEDRUN: "full", EXPLORE: "explore"}
MODE_OF_KNOWLEDGE = {v: k for k, v in KNOWLEDGE_OF_MODE.items()}


def mode_of(cfg) -> str:
    return getattr(cfg, "mode", None) or MODE_OF_KNOWLEDGE.get(getattr(cfg, "knowledge_profile", "full"), SPEEDRUN)


def is_explore(cfg) -> bool:
    return mode_of(cfg) == EXPLORE


def act_frame_cap(cfg) -> int:
    """单次 act 最多推进的帧数：探索模式用 explore_act_frames（默认 300），速通模式用 max_frames_per_act（默认 600）。"""
    if is_explore(cfg):
        return int(getattr(cfg, "explore_act_frames", 300))
    return int(getattr(cfg, "max_frames_per_act", 600))


def remind_seconds(live: bool) -> float:
    """解说计时：思考累计这么久没有 narrate 就在下一次工具调用时提醒。直播 10 秒，其余半分钟。"""
    return 10.0 if live else 30.0


def has_cutscene_tool(cfg) -> bool:
    """剧情工具：探索模式、直播模式（用户可在 HUD 切换剧情处理方式），或明确 --cutscene-mode agent 时开放。"""
    return is_explore(cfg) or bool(getattr(cfg, "live", False)) or getattr(cfg, "cutscene_mode", "auto") == "agent"


def game_tools(cfg) -> list[str]:
    """本次运行注册给智能体的游戏工具（不带前缀）。"""
    tools = ["observe", "act", "narrate", "set_effort", "stop_run"]
    if getattr(cfg, "live", False):
        tools.append("exit_level")          # 直播时观众/用户可以要求退出关卡
    if has_cutscene_tool(cfg):
        tools.append("cutscene")
    if getattr(cfg, "sim_assist", False):
        tools.append("simulate")            # 探索模式的限额短路线预测
    return tools


def describe(cfg) -> str:
    """日志与 HUD 用的模式说明，例如"速通模式（开模拟器）+ 直播"。"""
    mode = mode_of(cfg)
    if mode == SPEEDRUN:
        text = MODE_NAMES[mode] + ("（开模拟器）" if getattr(cfg, "sim", False) else "（关模拟器）")
    else:
        text = MODE_NAMES[mode] + ("（限额短路线预测）" if getattr(cfg, "sim_assist", False) else "")
    return text + (" + 直播" if getattr(cfg, "live", False) else "")
