"""控制台（overlay/console.html）的运行设置 → `python -m orchestrator.run` 的命令行参数。

逐项校验后生成参数列表（不拼命令行字符串），再交给 orchestrator/background.py 的独立宿主启动。
命令行能做的事控制台都能做；控制台没有的少见参数（端口、推进速度、录像编码等）仍可以直接用命令行。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CHAPTER_SIDS, REPO_ROOT
from .efforts import valid_effort
from .modes import EXPLORE, SPEEDRUN
from .runtimes import RUNTIMES

AGENT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}"
MODEL_PATTERN = r"[A-Za-z0-9._:/\[\]-]{1,100}"
PROVIDER_PATTERN = r"[A-Za-z0-9_.-]{1,60}"

SWITCHES = ("live", "sim", "sim_assist", "playback", "record", "hud_mod", "study", "limit_planning",
            "keep_obs_open", "start_paused", "quota_wait")

DEFAULTS = {
    "engine": "claude", "provider": "anthropic", "model": "opus", "effort": "high", "agent": "",
    "mode": SPEEDRUN, "live": False, "sim": True, "sim_assist": True, "playback": True, "record": True,
    "hud_mod": None, "study": True, "limit_planning": False, "continuous_music": None, "keep_obs_open": False,
    "start_paused": False, "quota_wait": True, "cutscene_mode": "auto", "chapters": [0],
    "save": {"name": "test", "slot": None, "create": False}, "max_minutes": 240, "autocompact": "",
}


def _bool(options: dict, key: str) -> bool:
    value = options[key]
    if type(value) is not bool:
        raise ValueError(f"{key} 须为开或关")
    return value


def normalize(options: dict) -> dict:
    """补上默认值并校验；返回规范化后的设置（也用于命令预览）。"""
    if not isinstance(options, dict):
        raise ValueError("运行设置须为对象")
    unknown = set(options) - set(DEFAULTS)
    if unknown:
        raise ValueError("未知设置：" + "、".join(sorted(unknown)))
    o = {**DEFAULTS, **options}
    if o["engine"] not in RUNTIMES:
        raise ValueError("未知执行器：" + str(o["engine"]))
    if o["engine"] == "codex":
        o["provider"] = "codex"
    for key, pattern, label in (("provider", PROVIDER_PATTERN, "提供方"), ("model", MODEL_PATTERN, "模型"),
                                ("agent", AGENT_PATTERN, "智能体标签")):
        if not isinstance(o[key], str) or not re.fullmatch(pattern, o[key].strip()):
            raise ValueError(f"{label}格式无效" + ("：只能用字母、数字、点、下划线与连字符，最多 40 个字符" if key == "agent" else ""))
        o[key] = o[key].strip()
    if not valid_effort(o["effort"]) or o["effort"] == "auto":
        raise ValueError("推理强度无效")
    if o["mode"] not in (SPEEDRUN, EXPLORE):
        raise ValueError("游玩模式须为速通或探索")
    if o["hud_mod"] is None and type(o["live"]) is bool:
        o["hud_mod"] = not o["live"]             # 与命令行相同：不开直播时游戏内 HUD 默认开
    for key in SWITCHES:
        _bool(o, key)
    if o["continuous_music"] is not None and type(o["continuous_music"]) is not bool:
        raise ValueError("连续播放音乐须为开、关或默认")
    if o["cutscene_mode"] not in ("auto", "agent"):
        raise ValueError("剧情处理须为自动跳过或交给智能体")
    chapters = o["chapters"]
    if (not isinstance(chapters, list) or not chapters or len(chapters) > len(CHAPTER_SIDS)
            or any(type(c) is not int or c not in CHAPTER_SIDS for c in chapters) or len(set(chapters)) != len(chapters)):
        raise ValueError("请选择要游玩的章节（官方章节编号，不重复）")
    o["chapters"] = sorted(chapters)
    save = o["save"]
    if not isinstance(save, dict) or set(save) - {"name", "slot", "create"}:
        raise ValueError("存档设置无效")
    save = {"name": "", "slot": None, "create": False, **save}
    if not isinstance(save["name"], str) or not save["name"].strip() or len(save["name"].strip()) > 20:
        raise ValueError("请选择存档，或填写新存档名（1～20 个字符）")
    if type(save["create"]) is not bool or (save["slot"] is not None and (type(save["slot"]) is not int or save["slot"] < 0)):
        raise ValueError("存档槽位无效")
    if save["create"] and (save["slot"] is not None or o["chapters"][0] != 0):
        raise ValueError("新建存档只能从序章开始，且不指定槽位")
    save["name"] = save["name"].strip()
    o["save"] = save
    minutes = o["max_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= 24 * 60:
        raise ValueError("现实时间上限须为 1～1440 分钟")
    compact = str(o["autocompact"] or "").strip()
    if compact and compact != "auto" and not (compact.isdigit() and 100000 <= int(compact) <= 1000000):
        raise ValueError("自动压缩窗口须为 auto 或 100000～1000000")
    o["autocompact"] = compact
    return o


def build(options: dict) -> list[str]:
    """新运行的命令行参数（不含 python -m orchestrator.run）。"""
    o = normalize(options)
    argv = ["--engine", o["engine"], "--provider", o["provider"], "--model", o["model"], "--effort", o["effort"],
            "--agent", o["agent"], "--mode", o["mode"], "--max-minutes", f"{o['max_minutes']:g}",
            "--save", o["save"]["name"], "--cutscene-mode", o["cutscene_mode"]]
    argv.append("--live" if o["live"] else "--no-live")
    if o["mode"] == SPEEDRUN:
        argv.append("--sim" if o["sim"] else "--no-sim")
    else:
        argv.append("--sim-assist" if o["sim_assist"] else "--no-sim-assist")
    argv.append("--playback" if o["playback"] else "--no-playback")
    argv.append("--record" if o["record"] else "--no-record")
    argv.append("--hud-mod" if o["hud_mod"] else "--no-hud-mod")
    if len(o["chapters"]) == 1:
        argv += ["--chapter", str(o["chapters"][0])]
    else:
        argv += ["--chapters", ",".join(map(str, o["chapters"]))]
    if o["save"]["create"]:
        argv.append("--create-save")
    elif o["save"]["slot"] is not None:
        argv += ["--save-slot", str(o["save"]["slot"])]
    if not o["study"]:
        argv.append("--no-study")
    if o["limit_planning"]:
        argv.append("--limit-planning")
    if o["continuous_music"] is not None:
        argv.append("--continuous-music" if o["continuous_music"] else "--freeze-music")
    if o["keep_obs_open"]:
        argv.append("--keep-obs-open")
    if o["start_paused"]:
        argv.append("--start-paused")
    if not o["quota_wait"]:
        argv.append("--no-quota-wait")
    if o["autocompact"]:
        argv += ["--autocompact", o["autocompact"]]
    return argv


def resume_args(run_dir: Path, fresh_session: bool = False) -> list[str]:
    """续跑一次停下时保持冻结的运行：其余设置都沿用原运行（见 orchestrator/run.py 的 --resume）。"""
    run_dir = Path(run_dir).resolve()
    if (REPO_ROOT / "runs").resolve() not in run_dir.parents:
        raise ValueError("只能续跑本项目 runs 目录下的运行")
    try:
        state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("这次运行没有 run_state.json，不能续跑") from exc
    if not state.get("save"):
        raise ValueError("这次运行没有记录存档名，不能续跑")
    argv = ["--agent", str(state["agent"]), "--save", str(state["save"]), "--resume", str(run_dir)]
    if state.get("engine"):
        argv += ["--engine", str(state["engine"])]
    if fresh_session:
        argv.append("--fresh-session")
    return argv


def command_line(argv: list[str]) -> str:
    """给人看的等价命令（控制台的命令预览）。"""
    import shlex
    return "python -m orchestrator.run " + " ".join(shlex.quote(a) for a in argv)
