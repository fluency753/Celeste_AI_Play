"""控制台（overlay/console.html）的运行设置 → `python -m orchestrator.run` 的命令行参数。

逐项校验后生成参数列表（不拼命令行字符串），再交给 orchestrator/background.py 的独立宿主启动。
主控的全部参数（含 --tune 可调参数与 --kb-deny 知识库权限）都能在控制台里设置；只在命令行有的是
--prepared（用原生 GPT 预先准备好的会话开局，见 start_gpt.ps1）与只用于测试的隐藏参数。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CHAPTER_SIDS, REPO_ROOT
from .efforts import valid_effort
from .modes import EXPLORE, SPEEDRUN
from .runtimes import RUNTIMES
from .execution import workspace_capabilities

AGENT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}"
MODEL_PATTERN = r"[A-Za-z0-9._:/\[\]-]{1,100}"
PROVIDER_PATTERN = r"[A-Za-z0-9_.-]{1,60}"

SWITCHES = ("live", "sim", "sim_assist", "stiffness", "formal_thought", "save_context", "playback", "record", "hud_mod", "study", "limit_planning",
            "keep_obs_open", "start_paused", "quota_wait", "record_marker", "skip_enter", "no_agent", "hud_window", "hud", "allow_chapter_reset",
            "freeze_chapter_select", "freeze_file_select", "freeze_main_menu")
MAPS = {"celeste": "蔚蓝官方章节"}      # 以后可能加 MOD 地图

DEFAULTS = {
    "engine": "claude", "provider": "anthropic", "model": "opus", "effort": "high", "agent": "",
    "mode": SPEEDRUN, "live": False, "sim": True, "sim_assist": True, "stiffness": False, "formal_thought": False, "save_context": True, "collision_image": None, "playback": True, "record": True,
    "hud_mod": None, "study": True, "limit_planning": False, "continuous_music": None, "keep_obs_open": False,
    "start_paused": False, "quota_wait": True, "allow_chapter_reset": False, "cutscene_mode": "auto", "chapters": [0], "segments": None,
    "action_permissions": None,
    "recording_mode": "chapter", "freeze_chapter_select": True, "freeze_file_select": False, "freeze_main_menu": False,
    "save": {"name": "test", "slot": None, "create": False}, "max_minutes": 240, "autocompact": "",
    # 以下是"更多设置"里的参数，默认值与命令行相同
    "map": "celeste", "runtime": "container", "study_minutes": 60, "max_usage": None, "quota_wait_max_hours": 24,
    "play_speed": 1, "auto_speed": 1, "rounds_per_room": 3, "room_scale": 2.0, "record_marker": False,
    "skip_enter": False, "no_agent": False, "hud_window": True, "hud": True, "bridge_port": 47774,
    "mcp_port": 47780, "hud_port": 47790, "ffmpeg": "", "record_video_args": "", "audio_offset_ms": None,
    "tune": {}, "kb_deny": [],
}


RECORDING_KEYS = ('recording_mode', 'freeze_chapter_select', 'freeze_file_select', 'freeze_main_menu')


def recording_options(options: dict, *, legacy: bool = False) -> dict:
    """Shared HUD/console settings; old runs without fields retain their behavior."""
    values = {key: options.get(key, ('continuous' if key == 'recording_mode' else False)
                              if legacy else DEFAULTS[key]) for key in RECORDING_KEYS}
    if values['recording_mode'] not in ('chapter', 'continuous'):
        raise ValueError('录像组织须为 chapter 或 continuous')
    for key in RECORDING_KEYS[1:]:
        if type(values[key]) is not bool:
            raise ValueError(f'{key} 须为开或关')
    return values


def recording_args(options: dict) -> list[str]:
    values = recording_options(options)
    return ['--recording-mode', values['recording_mode']] + [
        ('--' if values[key] else '--no-') + key.replace('_', '-') for key in RECORDING_KEYS[1:]]


def _number(options: dict, key: str, low: float, high: float, label: str, integer: bool = False, optional: bool = False):
    value = options[key]
    if value is None or value == "":
        if optional:
            return None
        raise ValueError(f"{label}不能为空")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}须为数字")
    if integer and value != int(value):
        raise ValueError(f"{label}须为整数")
    if not low <= value <= high:
        raise ValueError(f"{label}须在 {low:g}～{high:g} 之间")
    return int(value) if integer else float(value)


def _text(options: dict, key: str, limit: int, label: str) -> str:
    value = options[key]
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or any(c in value for c in "\r\n\0"):
        raise ValueError(f"{label}格式无效")
    return value.strip()


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
    o.update(recording_options(o))
    if o["hud_mod"] is None and type(o["live"]) is bool:
        o["hud_mod"] = not o["live"]             # 与命令行相同：不开直播时游戏内 HUD 默认开
    for key in SWITCHES:
        _bool(o, key)
    from .action_policy import ActionPermissions
    o['action_permissions'] = (ActionPermissions.legacy(o['allow_chapter_reset']) if o['action_permissions'] is None
                               else ActionPermissions.read(o['action_permissions'])).snapshot()
    if o["collision_image"] in ("", "default"):
        o["collision_image"] = None
    if o["collision_image"] not in (None, "always", "room", "off"):
        raise ValueError("碰撞图须为默认、每次、只在进房与观察时或不附")
    if o["continuous_music"] is not None and type(o["continuous_music"]) is not bool:
        raise ValueError("连续播放音乐须为开、关或默认")
    if o["cutscene_mode"] not in ("auto", "agent"):
        raise ValueError("剧情处理须为自动跳过或交给智能体")
    chapters = o["chapters"]
    if (not isinstance(chapters, list) or not chapters or len(chapters) > len(CHAPTER_SIDS)
            or any(type(c) is not int or c not in CHAPTER_SIDS for c in chapters) or len(set(chapters)) != len(chapters)):
        raise ValueError("请选择要游玩的章节（官方章节编号，不重复）")
    o["chapters"] = sorted(chapters)
    if o.get("segments") is not None:
        # 按段通关：[[章节, 段], ...]；要玩的章节由选中的段决定
        from .segments import chapters_of, load_table, pieces_of, validate_segments
        table = load_table()
        o["segments"] = [list(s) for s in validate_segments(o["segments"], table)]
        o["chapters"] = chapters_of(pieces_of([tuple(s) for s in o["segments"]], table))
    save = o["save"]
    if not isinstance(save, dict) or set(save) - {"name", "slot", "create"}:
        raise ValueError("存档设置无效")
    save = {"name": "", "slot": None, "create": False, **save}
    if not isinstance(save["name"], str) or not save["name"].strip() or len(save["name"].strip()) > 20:
        raise ValueError("请选择存档，或填写新存档名（1～20 个字符）")
    if type(save["create"]) is not bool or (save["slot"] is not None and (type(save["slot"]) is not int or save["slot"] < 0)):
        raise ValueError("存档槽位无效")
    if save["create"] and (save["slot"] is not None or o["chapters"][0] != 0
                           or (o.get("segments") and o["segments"][0] != [0, 0])):
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
    if o["map"] not in MAPS:
        raise ValueError("目前只支持蔚蓝官方章节")
    if o["runtime"] not in ("container", "host"):
        raise ValueError("运行方式须为容器或本机")
    if o['engine'] == 'codex':
        o['runtime'] = 'container'
    from types import SimpleNamespace
    capabilities = workspace_capabilities(SimpleNamespace(engine=o['engine'], runtime=o['runtime']))
    if not capabilities.programs and ((o["mode"] == SPEEDRUN and o["sim"]) or (o["mode"] == EXPLORE and o["sim_assist"])):
        raise ValueError("当前本机执行器没有沙箱，不能开预置模拟器或限额预测")
    o["study_minutes"] = _number(o, "study_minutes", 1, 600, "准备阶段上限")
    o["max_usage"] = _number(o, "max_usage", 0.01, 1, "提前暂停的用量比例", optional=True)
    o["quota_wait_max_hours"] = _number(o, "quota_wait_max_hours", 0.1, 240, "额度等待上限")
    o["play_speed"] = _number(o, "play_speed", 1, 20, "动作推进速度", integer=True)
    o["auto_speed"] = _number(o, "auto_speed", 1, 20, "自动推进速度", integer=True)
    o["rounds_per_room"] = _number(o, "rounds_per_room", 1, 100, "每屏规划轮次", integer=True)
    o["room_scale"] = _number(o, "room_scale", 1, 10, "房间放大倍数")
    for key, label in (("bridge_port", "桥接端口"), ("mcp_port", "游戏工具端口"), ("hud_port", "HUD 端口")):
        o[key] = _number(o, key, 1024, 65535, label, integer=True)
    if len({o["bridge_port"], o["mcp_port"], o["hud_port"]}) != 3:
        raise ValueError("三个端口不能相同")
    o["ffmpeg"] = _text(o, "ffmpeg", 400, "ffmpeg 路径")
    o["record_video_args"] = _text(o, "record_video_args", 600, "视频编码参数")
    o["audio_offset_ms"] = _number(o, "audio_offset_ms", -2000, 2000, "声音偏移", optional=True)
    from .tuning import BY_KEY, check_relations, coerce
    tune = o["tune"] or {}
    if not isinstance(tune, dict):
        raise ValueError("可调参数须为对象")
    clean = {}
    for key, value in tune.items():
        if key not in BY_KEY:
            raise ValueError(f"没有名为 {key} 的可调参数")
        if value is None or value == "":
            if not BY_KEY[key].optional:
                continue                      # 留空 = 用默认值
        clean[key] = coerce(key, value)
    check_relations(clean)
    o["tune"] = {k: v for k, v in clean.items() if v != BY_KEY[k].default}
    from .kb_access import normalize_deny
    o["kb_deny"] = normalize_deny(o["mode"], o["kb_deny"])
    if any("," in x for x in o["kb_deny"]):
        raise ValueError("知识库文件名里不能有逗号")
    return o


def build(options: dict) -> list[str]:
    """新运行的命令行参数（不含 python -m orchestrator.run）。"""
    o = normalize(options)
    argv = ["--engine", o["engine"], "--provider", o["provider"], "--model", o["model"], "--effort", o["effort"],
            "--agent", o["agent"], "--mode", o["mode"], "--max-minutes", f"{o['max_minutes']:g}",
            "--save", o["save"]["name"], "--cutscene-mode", o["cutscene_mode"]]
    argv.append("--live" if o["live"] else "--no-live")
    argv += recording_args(o)
    argv.append("--allow-chapter-reset" if o["allow_chapter_reset"] else "--no-allow-chapter-reset")
    for action, enabled in o['action_permissions'].items():
        argv.append(('--allow-' if enabled else '--no-allow-') + action.replace('_', '-'))
    if o["mode"] == SPEEDRUN:
        argv.append("--sim" if o["sim"] else "--no-sim")
    else:
        argv.append("--sim-assist" if o["sim_assist"] else "--no-sim-assist")
        if o["stiffness"]:
            argv.append("--stiffness")          # 思考僵直（探索模式，默认关）
    if o["formal_thought"]:
        argv.append("--formal-thought")         # 仅正式思考（默认关）
    argv.append("--save-context" if o["save_context"] else "--no-save-context")   # 节省上下文（默认开）
    if o["collision_image"]:
        argv += ["--collision-image", o["collision_image"]]
    argv.append("--playback" if o["playback"] else "--no-playback")
    argv.append("--record" if o["record"] else "--no-record")
    argv.append("--hud-mod" if o["hud_mod"] else "--no-hud-mod")
    if o.get("segments"):
        argv += ["--segments", segments_arg(o["segments"])]
    elif len(o["chapters"]) == 1:
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
    d = DEFAULTS
    if o["runtime"] != d["runtime"]:
        argv += ["--runtime", o["runtime"]]
    for key, flag in (("study_minutes", "--study-minutes"), ("quota_wait_max_hours", "--quota-wait-max-hours"),
                      ("play_speed", "--play-speed"), ("auto_speed", "--auto-speed"),
                      ("rounds_per_room", "--rounds-per-room"), ("room_scale", "--room-scale"),
                      ("bridge_port", "--bridge-port"), ("mcp_port", "--mcp-port"), ("hud_port", "--hud-port")):
        if o[key] != d[key]:
            argv += [flag, f"{o[key]:g}"]
    if o["max_usage"] is not None:
        argv += ["--max-usage", f"{o['max_usage']:g}"]
    if o["audio_offset_ms"] is not None:
        argv += ["--audio-offset-ms", f"{o['audio_offset_ms']:g}"]
    if o["ffmpeg"]:
        argv += ["--ffmpeg", o["ffmpeg"]]
    if o["record_video_args"]:
        argv += ["--record-video-args", o["record_video_args"]]
    for key, flag in (("record_marker", "--record-marker"), ("skip_enter", "--skip-enter"), ("no_agent", "--no-agent")):
        if o[key]:
            argv.append(flag)
    if not o["hud"]:
        argv.append("--no-hud")
    elif not o["hud_window"]:
        argv.append("--no-hud-window")
    from .tuning import argv as tune_argv
    argv += tune_argv(o["tune"])
    if o["kb_deny"]:
        argv += ["--kb-deny", ",".join(o["kb_deny"])]
    return argv


def segments_arg(segments) -> str:
    """[[章节, 段], ...] → --segments 的写法（同一章里连续的段写成 章节.起-止）。"""
    out, run = [], []
    for chapter, k in sorted(tuple(s) for s in segments):
        if run and (chapter != run[0][0] or k != run[-1][1] + 1):
            out.append(_segment_text(run))
            run = []
        run.append((chapter, k))
    if run:
        out.append(_segment_text(run))
    return ",".join(out)


def _segment_text(run) -> str:
    chapter, first, last = run[0][0], run[0][1], run[-1][1]
    return f"{chapter}.{first}" if first == last else f"{chapter}.{first}-{last}"


def config_for(options: dict):
    """按运行设置构造与主控相同的 RunConfig（只用于控制台的提示词预览，不连游戏、不建容器）。"""
    from .config import RunConfig
    from .modes import KNOWLEDGE_OF_MODE
    from .tuning import apply
    o = normalize(options)
    cfg = RunConfig(agent=o["agent"], save=o["save"]["name"], model=o["model"], effort=o["effort"], provider=o["provider"])
    cfg.engine, cfg.runtime, cfg.mode = o["engine"], o["runtime"], o["mode"]
    cfg.knowledge_profile = KNOWLEDGE_OF_MODE[o["mode"]]
    capabilities = workspace_capabilities(cfg)
    cfg.sim = o["mode"] == SPEEDRUN and o["sim"] and capabilities.programs
    cfg.sim_assist = o["mode"] == EXPLORE and o["sim_assist"] and capabilities.programs
    cfg.stiffness = o["mode"] == EXPLORE and o["stiffness"]
    cfg.formal_thought = bool(o["formal_thought"])
    cfg.save_context = bool(o["save_context"])
    cfg.collision_image = o["collision_image"]
    cfg.live, cfg.playback, cfg.cutscene_mode = o["live"], o["playback"], o["cutscene_mode"]
    cfg.allow_chapter_reset = o["allow_chapter_reset"]
    cfg.action_permissions = o['action_permissions']
    cfg.pause_limit = o["limit_planning"] and cfg.sim
    cfg.rounds_per_room, cfg.room_scale = o["rounds_per_room"], o["room_scale"]
    cfg.chapters, cfg.chapter = list(o["chapters"]), o["chapters"][0]
    if o.get("segments"):
        from .segments import load_table, pieces_of
        cfg.checkpoints = load_table()
        cfg.plan = pieces_of([tuple(s) for s in o["segments"]], cfg.checkpoints)
    cfg.study, cfg.study_minutes = o["study"], o["study_minutes"]
    cfg.kb_deny = list(o["kb_deny"])
    apply(cfg, o["tune"])
    cfg.live_remind_s = cfg.remind_live_s if cfg.live else cfg.remind_s
    return cfg


def resume_args(run_dir: Path, fresh_session: bool = False, new_recording: bool = False) -> list[str]:
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
    for key in ('bridge_port', 'mcp_port'):
        if key in state:
            argv += ['--' + key.replace('_', '-'), str(state[key])]
    if fresh_session:
        argv.append("--fresh-session")
    if new_recording:
        argv.append("--new-recording")      # 原录像接不上时放弃它，另起一段录像
    return argv


def command_line(argv: list[str]) -> str:
    """给人看的等价命令（控制台的命令预览）。"""
    import shlex
    return "python -m orchestrator.run " + " ".join(shlex.quote(a) for a in argv)
