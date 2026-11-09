"""控制台（/console）的后端：开局选项、游戏与主控状态、运行记录、启动 / 续跑 / 交还控制。

运行中的介入（发消息、暂停继续、推理强度、剧情处理、停止、中断）走 HUD 服务原有的 /api/control（RunControl），
这里只负责"开一次运行之前"与"运行结束之后"的事。

注意：桥接只接受一个连接，新连接会顶掉旧的（主控的连接就断了）。所以控制台平时不连桥接，游戏状态从桥接写进共享内存的
状态字段读（overlay/modhud.py 的 bridge_status）；只有确认没有主控在运行时，"交还控制"才会连一次桥接。
"""
from __future__ import annotations

import json
import re
import socket
import sys
import time
from pathlib import Path

from orchestrator.config import CHAPTER_SIDS, REPO_ROOT
from orchestrator.recording_lifecycle import pending_recording as is_recording_pending
from orchestrator.session_store import load_json

CHAPTER_LABELS = {0: "序章", 1: "1A 被遗弃之城", 2: "2A 旧址", 3: "3A 天空度假村", 4: "4A 黄金山脊", 5: "5A 镜之神殿",
                  6: "6A 映像", 7: "7A 顶峰", 8: "终章", 9: "8A 核心", 10: "第九章 告别"}
LAUNCH_RECORD = REPO_ROOT / "local" / "console" / "last_launch.json"
CONTROLLER_MUTEX = "Local\\CelesteAIPlayController"


def _read_json(path: Path) -> dict:
    try:
        return load_json(path, encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _mutex_exists(name: str) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel.OpenMutexW(0x00100000, False, name)    # SYNCHRONIZE
    if handle:
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def latest_run() -> Path | None:
    try:
        value = (REPO_ROOT / "runs" / "latest.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value) if value and Path(value).is_dir() else None


def orchestrator_status(mcp_port: int = 47780) -> dict:
    """有没有主控在运行：独立宿主的互斥量、游戏工具端口、最新运行目录里还在更新且没写总结的统计，任一成立就算在运行。"""
    launch = _read_json(LAUNCH_RECORD)
    host = _read_json(Path(launch["host"]) / "status.json") if launch.get("host") else {}
    run = latest_run()
    metrics = _read_json(run / "metrics.json") if run else {}
    fresh = bool(metrics) and time.time() - (metrics.get("written_wall") or 0) < 15 and not (run / "summary.json").exists()
    host_running = host.get("state") in {"starting", "running"}
    running = _mutex_exists(CONTROLLER_MUTEX) or _port_open(mcp_port) or fresh or host_running
    # 启动失败：控制台启动的主控退出了，而且没有为这次启动写出运行总结（中断、预算用完等正常结束都会写 summary.json，
    # 退出码也可能是 1，那不算失败）
    started_run = run is not None and str(run) != launch.get("latest_before")
    failed = (bool(launch.get("host")) and not running and host.get("state") in {"exited", "failed"}
              and (host.get("state") == "failed" or host.get("exit_code") not in (0, None))
              and not (started_run and (run / "summary.json").exists()))
    return {"running": running, "run": str(run) if run else None, "host": launch.get("host"),
            "host_state": host.get("state"), "host_exit_code": host.get("exit_code"), "failed": failed,
            "launched": launch.get("argv"), "launched_wall": launch.get("wall"),
            "log_tail": _tail(Path(launch["host"]) / "output.log") if failed else ""}


def _tail(path: Path, limit: int = 1600) -> str:
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def obs_status() -> dict:
    if sys.platform != "win32":
        return {"running": None}
    try:
        from tools.obs_setup import obs_running
        return {"running": obs_running()}
    except Exception as exc:  # noqa: BLE001
        return {"running": None, "error": str(exc)}


def game_status(bridge_port: int = 47774) -> dict:
    from overlay.modhud import bridge_status
    return bridge_status(bridge_port)


def status() -> dict:
    orchestrator = orchestrator_status()
    run = Path(orchestrator["run"]) if orchestrator["run"] else None
    state = _read_json(run / "run_state.json") if run else {}
    summary = _read_json(run / "summary.json") if run else {}
    return {"now": time.time(), "orchestrator": orchestrator, "game": game_status(), "obs": obs_status(),
            "run": {"dir": str(run) if run else None, "state": state,
                    "summary": {k: summary.get(k) for k in ("stop_reason", "completed", "game_released", "total_time_ticks",
                                                            "deaths", "decisions", "elapsed_s")} if summary else None}}


def options() -> dict:
    from orchestrator.gamepaths import celeste_dir_info
    from orchestrator.launch_options import DEFAULTS, MAPS
    from orchestrator.play_scope import sim_available
    from orchestrator.runtimes import configured_runtimes
    from orchestrator.tuning import GROUPS, catalog
    saves, saves_error = [], None
    try:
        from orchestrator.saves import inventory
        saves = inventory()
    except Exception as exc:  # noqa: BLE001（没有部署配置时也要能打开控制台）
        saves_error = str(exc)
    deploy = _read_json(REPO_ROOT / "configs" / "local.deploy.json")
    from orchestrator.segments import load_table
    checkpoints = {str(c): [{"index": int(p.get("index", i)), "level": p.get("level"),
                             "name": p.get("name_zh") or p.get("name") or p.get("level")} for i, p in enumerate(points)]
                   for c, points in load_table().items()}
    from orchestrator.config import SIDE_CHAPTERS, side_code, side_label
    return {"runtimes": configured_runtimes(), "chapters": [{"id": i, "label": CHAPTER_LABELS.get(i, sid)}
                                                           for i, sid in sorted(CHAPTER_SIDS.items())],
            # 有 B／C 面的章节（第 1～7 章与核心）：控制台按 A／B／C 分行选；B／C 面只整章游玩
            "sides": [{"id": i, "side": side, "code": side_code(i, side), "label": side_label(i, side)}
                      for side in ("B", "C") for i in SIDE_CHAPTERS],
            "checkpoints": checkpoints,
            "saves": saves, "saves_error": saves_error, "sim_available": sim_available(REPO_ROOT),
            "defaults": {**DEFAULTS, "hud_mod": None}, "deploy": {k: deploy.get(k) for k in ("agent", "save", "chapters")},
            "maps": [{"id": k, "label": v, "enabled": True} for k, v in MAPS.items()]
                    + [{"id": "mod", "label": "MOD 地图", "enabled": False, "note": "未来版本中支持"}],
            "tunables": catalog(), "tunable_groups": list(GROUPS), "prefs": load_prefs(), "game": celeste_dir_info(),
            "profiles": profiles_payload(), **machine_payload()}


def machine_payload() -> dict:
    """本机设置与各 Claude 提供方的用量设置（docs/开发/设置分层.md 第二步）。第一次读取时按当前配置与最近一次运行迁移。"""
    from orchestrator import machine_settings, profiles
    from orchestrator.runtimes import runtime
    machine = machine_settings.load()
    try:
        machine_settings.seed_usage(profiles.meta(profiles.selected()).get("form"))
    except (OSError, ValueError):
        pass
    usage = {row["id"]: machine_settings.usage_for(row["id"]) for row in runtime("claude").providers()}
    return {"machine": machine, "provider_usage": usage, "last": machine_settings.load_last(),
            "run_groups": {group: list(keys) for group, keys in machine_settings.RUN_GROUPS.items()}}


def save_last(values) -> dict:
    from orchestrator import machine_settings
    return {"last": machine_settings.save_last(values)}


def save_machine(update) -> dict:
    from orchestrator import machine_settings
    return {"machine": machine_settings.save(update)}


def save_provider_usage(data) -> dict:
    from orchestrator import machine_settings
    if not isinstance(data, dict) or set(data) - {"provider", "usage"} or not isinstance(data.get("provider"), str):
        raise ValueError("用量设置的参数无效")
    return {"provider": data["provider"], "usage": machine_settings.save_usage(data["provider"], data.get("usage") or {})}


# ---------------------------------------------------------------- 控制台的本机设置（configs/local.console.json，不入库）

PREFS = REPO_ROOT / "configs" / "local.console.json"


def _kb_permissions(pid: str, data: dict | None = None) -> dict:
    from orchestrator import profiles
    if pid == profiles.LEGACY and data is None:
        data = _read_json(PREFS)
    return profiles.kb_permissions(pid, data)


def load_prefs() -> dict:
    """游戏位置（本机环境，所有配置共用）与当前配置的知识库阅读权限。"""
    from orchestrator import profiles
    data = _read_json(PREFS)
    return {"celeste_dir": data.get("celeste_dir") or "", **_kb_permissions(profiles.selected(), data)}


def save_prefs(update: dict) -> dict:
    """保存控制台的本机设置：游戏位置（空 = 自动查找）与各模式不给智能体看的知识库文件。"""
    from orchestrator.gamepaths import is_game_dir
    from orchestrator.kb_access import normalize_allow, normalize_deny
    if not isinstance(update, dict) or set(update) - {"celeste_dir", "kb_deny", "kb_allow"}:
        raise ValueError("设置无效")
    data = _read_json(PREFS)
    if "celeste_dir" in update:
        value = update["celeste_dir"]
        if not isinstance(value, str) or len(value) > 400 or any(c in value for c in "\r\n\0"):
            raise ValueError("游戏位置格式无效")
        value = value.strip().strip('"')
        if value and not is_game_dir(value):
            raise ValueError(f"{value} 里没有 Celeste.exe：请填《蔚蓝》的安装目录（含 Celeste.exe 的那一层）")
        data["celeste_dir"] = str(Path(value)) if value else ""
    deny_update = allow_update = None
    if "kb_deny" in update:
        deny = update["kb_deny"]
        if not isinstance(deny, dict) or set(deny) - {"speedrun", "explore"}:
            raise ValueError("知识库权限设置无效")
        deny_update = {mode: normalize_deny(mode, files) for mode, files in deny.items()}
    if "kb_allow" in update:
        allow = update["kb_allow"]
        if not isinstance(allow, dict) or set(allow) - {"explore"}:
            raise ValueError("开放速通资料的设置无效（只有探索模式）")
        allow_update = {"explore": normalize_allow("explore", allow.get("explore"))}
    if deny_update is not None or allow_update is not None:
        from orchestrator import profiles
        pid = _editable_profile("知识库的阅读权限")
        if pid != profiles.LEGACY:        # 新建配置的权限存在它自己的 profile.json
            profiles.set_kb_permissions(pid, deny_update, allow_update)
            deny_update = allow_update = None
    if deny_update is not None:
        current = data.get("kb_deny") if isinstance(data.get("kb_deny"), dict) else {}
        current.update(deny_update)
        data["kb_deny"] = current
    if allow_update is not None:
        current = data.get("kb_allow") if isinstance(data.get("kb_allow"), dict) else {}
        current.update(allow_update)
        data["kb_allow"] = current
    PREFS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PREFS)
    return load_prefs()


# ---------------------------------------------------------------- 配置（顶部的"配置"：表单设置、知识库、提示词）

def _profile() -> str:
    from orchestrator import profiles
    return profiles.selected()


def _editable_profile(what: str) -> str:
    from orchestrator import profiles
    pid = profiles.selected()
    if profiles.readonly(pid):
        raise ValueError(f"现在选的是内置配置“{profiles.meta(pid)['name']}”，不能修改{what}：先在顶部“配置”里点“新建…”，再改")
    return pid


def _profile_usage() -> dict:
    """每份新建配置被几次运行记着用过（续跑要用它）。"""
    counts = {}
    root = REPO_ROOT / "runs"
    for state in root.glob("*/*/run_state.json") if root.is_dir() else []:
        pid = _read_json(state).get("profile")
        if isinstance(pid, str):
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def profiles_payload() -> dict:
    from orchestrator import profiles
    listing = profiles.listing()
    usage = _profile_usage()
    for row in listing["profiles"]:
        row["runs"] = usage.get(row["id"], 0)
    pid = listing["selected"]
    m = profiles.meta(pid)
    return {**listing, "current": {"id": pid, "name": m["name"], "readonly": profiles.readonly(pid),
                                   "builtin": profiles.builtin(pid), "preset": profiles.is_preset(pid),
                                   "description": m["description"], "form": m["form"]}}


def profile_select(pid) -> dict:
    from orchestrator import profiles
    profiles.select(pid)
    return profiles_payload()


def profile_create(data) -> dict:
    """新建配置：基于当前配置（复制它的提示词修改、个人追加要求、知识库修改与权限，表单设置用运行页现在的）或基于默认。"""
    from orchestrator import profiles
    if not isinstance(data, dict) or set(data) - {"name", "base", "form"}:
        raise ValueError("新建配置的参数无效")
    base = data.get("base", "current")
    if base not in ("current", "default"):
        raise ValueError("新建配置须基于当前配置或默认配置")
    if base == "current":
        source = profiles.selected()
        pid = profiles.create(data.get("name"), source, data.get("form"), _kb_permissions(source))
    else:
        pid = profiles.create(data.get("name"), profiles.DEFAULT)
    profiles.select(pid)
    return profiles_payload()


def profile_save(data) -> dict:
    """保存配置：把运行页现在的表单设置存进当前配置（知识库与提示词的修改在各自的页面保存时就存进去了）。"""
    from orchestrator import profiles
    if not isinstance(data, dict) or set(data) - {"form"}:
        raise ValueError("保存配置的参数无效")
    profiles.save_form(_editable_profile("配置"), data.get("form"))
    return profiles_payload()


def profile_rename(data) -> dict:
    from orchestrator import profiles
    if not isinstance(data, dict) or set(data) - {"id", "name"}:
        raise ValueError("改名的参数无效")
    profiles.rename(data.get("id"), data.get("name"))
    return profiles_payload()


def profile_delete(data) -> dict:
    from orchestrator import profiles
    if not isinstance(data, dict) or set(data) - {"id"}:
        raise ValueError("删除配置的参数无效")
    pid = profiles.check(data.get("id"))
    orchestrator = orchestrator_status()
    if orchestrator["running"] and orchestrator.get("run"):
        if _read_json(Path(orchestrator["run"]) / "run_state.json").get("profile") == pid:
            raise ValueError("正在运行的主控用着这份配置：等运行结束再删除")
    profiles.delete(pid)
    return profiles_payload()


# ---------------------------------------------------------------- 模型提供方（运行页"管理提供方"）

def _providers_in_use() -> tuple[bool, frozenset | None]:
    """(有没有主控在运行, 它用着的 Claude Code 提供方)。在运行但读不出用的是哪个时返回 None：全部不许改。
    来源：控制台这次启动的命令行（--engine / --provider），以及最新运行目录里还没写总结的 run_state.json。"""
    orchestrator = orchestrator_status()
    if not orchestrator["running"]:
        return False, frozenset()
    found, known = set(), False
    argv = orchestrator.get("launched") if orchestrator.get("host_state") in {"starting", "running"} else None
    if isinstance(argv, list) and "--resume" not in argv:
        values = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i] in ("--engine", "--provider")}
        if values.get("--engine", "claude") == "claude":
            found.add(values.get("--provider", "anthropic"))
        known = True
    run = Path(orchestrator["run"]) if orchestrator.get("run") else None
    state = _read_json(run / "run_state.json") if run else {}
    if state and not (run / "summary.json").exists():
        if state.get("engine", "claude") == "claude":
            found.add(state.get("provider") or "anthropic")
        known = True
    return True, frozenset(found) if known else None


def providers_list() -> dict:
    from orchestrator import provider_settings
    running, in_use = _providers_in_use()
    return provider_settings.listing(running=running, in_use=in_use)


def _provider_call(action, data):
    """保存 / 删除提供方：意外的格式错误不带原始内容（请求里可能有粘贴的密钥）。"""
    from orchestrator import provider_settings
    running, in_use = _providers_in_use()
    try:
        ident = action(data, in_use=in_use)
    except ValueError:
        raise
    except (TypeError, KeyError, AttributeError):
        raise ValueError("提供方设置的格式无效") from None
    return {**provider_settings.listing(running=running, in_use=in_use), "saved": ident}


def providers_save(data) -> dict:
    from orchestrator import provider_settings
    return _provider_call(provider_settings.save, data)


def providers_remove(data) -> dict:
    from orchestrator import provider_settings
    return _provider_call(provider_settings.remove, data)


def providers_test(data) -> dict:
    from orchestrator import provider_settings
    try:
        return provider_settings.connectivity(data)
    except ValueError:
        raise
    except (TypeError, KeyError, AttributeError):
        raise ValueError("请求格式无效") from None


# ---------------------------------------------------------------- 知识库页

def kb_list(mode: str) -> dict:
    """当前配置下这个模式的知识库：文件（edited = 这份配置改过）与阅读权限。"""
    from orchestrator import profiles
    from orchestrator.kb_access import documents, root_of
    pid = _profile()
    root = root_of(mode)
    with profiles.using(pid):
        docs = documents(mode)
    known = {d["path"] for d in docs}
    prefs = load_prefs()
    deny = [x for x in prefs["kb_deny"].get(mode, []) if x in known]
    allow = [x for x in prefs["kb_allow"].get(mode, []) if x in known]
    sim_readme = REPO_ROOT / "sim" / "dist" / "README.md"     # Claude 执行器开模拟器时另要读完
    from orchestrator.kb_layout import enabled
    from orchestrator.kb_access import KB_ROOT
    source_label = 'kb/shared + kb/'+mode if enabled(KB_ROOT) else root.relative_to(REPO_ROOT).as_posix()
    return {"mode": mode, "root": source_label, "documents": docs, "deny": deny, "allow": allow,
            "sim_readme": mode == "speedrun" and sim_readme.is_file(), "profile": pid, "readonly": profiles.readonly(pid),
            "edits_dir": _rel(profiles.kb_edits_dir(pid))}


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def kb_read(mode: str, path: str) -> dict:
    from orchestrator import profiles
    from orchestrator.kb_access import read_document
    with profiles.using(_profile()):
        return read_document(mode, path)


def kb_write(mode: str, path: str, text: str, mtime=None) -> dict:
    """保存知识库文件的修改：存进当前配置（仓库里的文件不变）。"""
    from orchestrator import profiles
    from orchestrator.kb_access import write_document
    pid = _editable_profile("知识库")
    if orchestrator_status()["running"]:
        raise ValueError("有主控在运行：知识库正挂在智能体的容器里，等运行结束再保存修改")
    with profiles.using(pid):
        return write_document(mode, path, text, mtime)


def kb_reset(mode: str, path: str) -> dict:
    """恢复仓库版本：删掉当前配置对这个文件的修改。"""
    from orchestrator import profiles
    from orchestrator.kb_access import reset_document
    pid = _editable_profile("知识库")
    if orchestrator_status()["running"]:
        raise ValueError("有主控在运行：等运行结束再恢复")
    with profiles.using(pid):
        return reset_document(mode, path)


# ---------------------------------------------------------------- 提示词页

PLACEHOLDERS = {
    '{tool_sections}': '按实际登记的游戏工具及文件／程序能力插入对应章节',
    '{observe_files_rule}': '观测文件入口；无文件能力时改为内联核验说明',
    '{observe_reference_rule}': '观测格式参考；无文件能力时提供必要格式',
    '{act_files_rule}': '计划、轨迹、重试和TAS文件入口（有文件能力时）',
    '{act_reference_rule}': '当前可读的完整动作参考',
    '{file_tool_names}': '当前执行器实际可用的文件读取、查找和编辑工具名',
    '{files_reference_rule}': '当前可读的工作区文件参考',
    "{task_brief}": "本次任务的简短摘要：模式、存档、计划起止与停止边界，不展开完整配置 JSON",
    "{current_settings}": "本次任务与配置的 JSON 快照（存档、范围、权限、程序额度等）",
    "{guide_index_path}": "本次已发布的工作区操作指南索引路径",
    "{guide_read_plan}": "主控给出的操作指南路径与本次需要阅读的范围",
    "{kb_reference_rule}": "本机生成了 kb/reference/ 社区资料时的一句（没有时为空）",
    "{agent_environment}": "按实际执行器与文件／程序能力生成工具名称、容器、阅读计量和图像送达约定",
    "{explore_memory_rule}": "有工作区时维护 notes.md，否则依据当前观测与对话记录进展",
    "{public_feedback_rule}": "当前执行器的公开解说入口：Codex commentary／narrate，Claude narrate",
    "{room_reference_rule}": "可读观测参考入口；无文件能力时按当前观测核验",
    "{story_reference_rule}": "可读的剧情参考入口；未开放时依据当前观测处理",
    "{program_reference_rule}": "本次可读的选读程序优化及工作区接口资料入口",
    "{program_timeout_rule}": "当前执行器的程序时限参数（Claude timeout／Codex timeout_ms）与前台约定",
    "{program_progress_rule}": "当前执行器的搜索进度说明入口（Claude description／Codex commentary）",
    "{interface}": "在这里插入 interface_zh.md（工具接口）",
    "{explore_notes}": "在这里插入 explore_notes_zh.md（记忆与公开反馈；笔记部分按工作区能力调整）",
    "{explore_programs}": "在这里插入 explore_programs_zh.md（探索模式的辅助程序；执行器能运行程序时才有）",
    "{explore_reading_rule}": "探索模式开局读什么：能读写工作区时先读必读清单并在笔记里记工具约定，否则按观测与工具说明学习",
    "{explore_reference_rule}": "探索模式基本规则、坐标格式与工具约定的出处（能读工作区时指向 /kb 的几份文件）",
    "{explore_environment_rule}": "探索模式的文件与程序环境一句（容器的读写与断网 Bash，或只有游戏工具）",
    "{tas_workspace_rule}": "TAS 输入辅助脚本 /workspace/game/tas.py 的说明（能运行程序时才有）",
    "{tas_cursor_rule}": "TAS 输入的游标与剩余输入怎么接着用（按能否运行程序给不同写法）",
    "{observation_source_rule}": "房间完整数据从哪里取（能读工作区时可读 latest_obs.json）",
    "{geometry_program_rule}": "分析房间几何的做法（能运行程序时可写程序扫描通道，否则按矩形边界核对）",
    "{geometry_record_rule}": "已验证的通道记在哪里（能写工作区时记进笔记或程序，否则在公开解说里简短总结）",
    "{thought_temp_limit}": "开了“仅正式思考”时 narrate（临时思考）的字数（可调参数 thought_temp_max_hanzi）",
    "{temp_thought_where}": "开了“仅正式思考”时临时思考显示在哪里（游戏内 HUD 时只在控制台；网页 HUD 时也在它的历史里）",
    "{narrate_target}": "探索提示词里 narrate 的用途：没开“仅正式思考”时为“告诉观众”，开了为“写下临时思考”",
    "{hud_thought_shown}": "速通 hud_zh.md 里 HUD 显示哪句思考：没开“仅正式思考”时为最新写下的一句，开了为最近一次 act 的正式思考",
    "{narrate_viewers}": "速通 hud_zh.md 里 narrate 给谁看的一句：没开“仅正式思考”时为原文，开了改为临时思考的说明",
    "{act_collision_image}": "act 说明里碰撞图什么时候附在结果里（--collision-image：always / room / off；默认 room，只在进新房间与 observe 时）",
    "{observe_collision_image}": "observe 说明里是否附碰撞图（--collision-image off 时只写进工作区）",
    "{collision_image_when}": "速通 interface_zh.md 里碰撞图什么时候返回（随 --collision-image）",
    "{formal_thought_act}": "开了“仅正式思考”时 act 工具说明里 thought 的补充；没开时为空",
    "{save_context_rule}": "开了“节省上下文”时 act 工具说明里的一段（结果省略了什么、轨迹列名、进新房间和 observe 照常给全）；没开时为空",
    "{save_context_print}": "开了“节省上下文”时 interface_zh.md 程序一节的建议（合并调用、程序只打印结论、地形不整房打印字符画、长命令写成脚本）；没开时为空",
    "{act_min_frames}": "单次 act 最少推进的帧数（可调参数 min_frames_per_act，默认 1；不超过本模式的上限）",
    "{act_max_frames}": "单次 act 最多推进的帧数（可调参数：速通 max_frames_per_act，探索 explore_act_frames）",
    "{explore_search_rule}": "探索模式自写模拟器与搜索程序推演帧数的一句（可调参数 explore_search_frames；不限时为空）",
    "{cpu_rule}": "探索模式最多可以用几个 CPU 核的一句（可调参数 agent_cpus，默认本机的核数；容器运行时不超过 Docker 实际能用的核数）",
    "{stiffness_rule}": "探索模式的思考僵直或 Token 推进说明；前者按时间、后者按新增输出 token 换算帧数（tokens_per_frame 可调），两项互斥；均未开时为空",
    "{thinking_freeze_rule}": "未启用 Token 推进时说明思考期间冻结；启用时为空",
    "{formal_thought_rule}": "开了“仅正式思考”时的说明（HUD 只显示 act 的正式思考，冻结期间 narrate 写的临时思考只显示在控制台；可调参数 thought_temp_max_hanzi）；没开时为空",
    "{bash_default}": "Bash 不写 timeout 时的上限（可调参数 bash_default_s）",
    "{bash_max}": "Bash 最长时长（可调参数 bash_max_s）",
    "{explore_program}": "探索模式单次规划程序最长（可调参数 explore_program_s；不限时写\"不限\"）",
    "{explore_program_rule}": "探索模式单次规划程序时长的一句（\"单次规划程序最多 2 分钟；\"，不限时为\"单次规划程序不限时长；\"）",
    "{playback_ratio}": "规划回放每多少条推演画一条（可调参数 playback_ratio）",
    "{playback_min_tracks}": "规划回放每轮至少画几条（可调参数 playback_min_tracks）",
    "{thought_max}": "思考语句最长字数（可调参数 thought_max_chars）",
    "{thought_mood}": "简短的自然表达约定，不提供情绪示例",
    "{thought_brief_rule}": "\"思考语句尽量简短，汉字不超过 30 个（英文术语不计）。\"一句（可调参数 thought_max_hanzi）：只给 Claude Code 执行器，Codex 执行器为空",
    "{remind}": "解说提醒间隔（可调参数 remind_s / remind_live_s）",
    "{rounds}": "一屏大的房间存入的规划轮次（--rounds-per-room）",
    "{rounds4}": "两倍长、两倍高的房间的规划轮次",
    "{death}": "死亡补的规划轮次（可调参数 death_bonus）",
    "{game_tools}": "本次注册的游戏工具",
    "{game_tool_list}": "\"游戏工具有 N 个：……\"一句，按本次注册的游戏工具生成",
    "{preset_sim}": "探索模式里预设模拟器的开放情况（关限额预测时为\"禁用预设模拟器\"，开时为\"预设模拟器只能通过 simulate 按额度使用\"）",
    "{auto_handling}": "系统自动处理哪些画面（剧情是否交给智能体，按本次设置生成）",
}

from orchestrator.prompt_environment import GAME_NAMES as _GAME_NAMES
PLACEHOLDERS.update({'{tool:'+name+'}': '按实际执行器插入 '+name+' 工具名'
                     for name in (*_GAME_NAMES, 'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep')})


def personal_prompt_read(scope='all') -> dict:
    from orchestrator import personal_prompts, profiles
    pid = _profile()
    with profiles.using(pid):
        return {**personal_prompts.read(scope), 'catalog': personal_prompts.catalog(),
                'max_chars': personal_prompts.MAX_TEXT_CHARS, 'readonly': profiles.readonly(pid),
                'store': _rel(profiles.personal_store(pid))}


def personal_prompt_write(scope, text, version) -> dict:
    from orchestrator import personal_prompts, profiles
    with profiles.using(_editable_profile('个人追加要求')):
        return personal_prompts.save(scope, text, version)


def _prompt_name(name) -> str:
    from orchestrator.prompting import prompt_files
    if not isinstance(name, str) or name not in prompt_files():
        raise ValueError("没有这个提示词文件")
    return name


def _version(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prompt_preview(options: dict, engine: str | None = None) -> dict:
    """按运行页的当前设置与选中的配置，生成智能体会收到的提示词（分块，标出来源文件与本机修改）和准备阶段的第一条消息。
    engine 给出时按那个执行器生成（提示词页的“Claude Code／Codex”切换，不改运行页）：两种执行器共用同一套模板，
    不同的是工具名、文件与程序工具、临时思考的写法与字数上限、图像送达方式和个人追加要求的适用范围。"""
    from orchestrator.launch_options import config_for
    # 预览不需要真的起名字或选存档：没填的用占位值，免得运行页还没填完就看不了提示词
    options = dict(options or {})
    if engine is not None:
        if engine not in ("claude", "codex"):
            raise ValueError("未知执行器：" + str(engine))
        if engine != options.get("engine", "claude"):
            # 模型与推理强度不进提示词：换执行器时用各自的默认值，免得运行页选的模型不在另一边的列表里
            options.update({"engine": engine, "provider": "codex", "model": "gpt", "effort": "medium"} if engine == "codex"
                           else {"engine": engine, "provider": "anthropic", "model": "opus", "effort": "high"})
    if not options.get("agent"):
        options["agent"] = "preview"
    if not (options.get("save") or {}).get("name"):
        options["save"] = {"name": "preview", "slot": None, "create": False}
    from orchestrator import profiles
    pid = _profile()
    with profiles.using(pid):
        return {**_prompt_preview(config_for(options, pid)), "profile": pid, "readonly": profiles.readonly(pid),
                "overrides_dir": _rel(profiles.prompts_dir(pid))}


def _prompt_preview(cfg) -> dict:
    from types import SimpleNamespace
    from orchestrator.kb_access import root_of
    from orchestrator.kb_study import CONTAINER_KB, required_files
    from orchestrator.prompting import overridden_parts, prompt_parts, study_prompt
    from orchestrator.startup_prompt import runtime_prompt_parts
    from orchestrator.button_prompts import catalog
    from orchestrator.execution import workspace_capabilities
    overridden = set(overridden_parts())
    guided = workspace_capabilities(cfg).workspace_read
    from orchestrator.prompt_guide import guide_document
    from orchestrator.personal_prompts import snapshot
    personal = snapshot(cfg)
    guide = {**(guide_document(cfg) if guided else {}), 'personal': personal}
    from orchestrator.interface_sections import PART_NAMES

    def preview_part(label, text, source, sep):
        section = PART_NAMES.get(label)
        if section:
            label = f'interface_zh.md · {section}'
            if source not in overridden:
                source = 'interface_zh.md'
            if source in PART_NAMES:
                label += '（旧单节覆盖）'
        return {"label": label, "text": text, "source": source, "sep": sep,
                "overridden": source in overridden}

    full_parts = ([preview_part(p['label'], p['text'], p['source'], '') for p in guide['sections']] if guided else
                  [preview_part(p.label, p.text, p.source, p.sep) for p in prompt_parts(cfg)])
    parts = [preview_part(p.label, p.text, p.source, p.sep) for p in runtime_prompt_parts(cfg, guide=guide)]
    first = None
    from orchestrator.execution import workspace_capabilities
    if cfg.study and workspace_capabilities(cfg).workspace_read:
        from orchestrator import kb_layout
        from orchestrator.kb_access import KB_ROOT
        files = (kb_layout.selected(cfg, KB_ROOT)[1] if kb_layout.enabled(KB_ROOT) else
                 [rel for rel in required_files(root_of(cfg.mode)) if rel not in set(cfg.kb_deny)])
        paths = [f"{CONTAINER_KB}/{rel}" for rel in files]
        from orchestrator.sim_mode import agent_sim
        if agent_sim(cfg):          # Claude 开模拟器：/sim/README.md 排在知识库正文之后、社区原文之前（与 KbStudy 相同）
            k = next((i for i, rel in enumerate(files) if rel.startswith("reference/")), len(files))
            paths.insert(k, "/sim/README.md")
        first = study_prompt(SimpleNamespace(container_paths=lambda: paths, minutes=cfg.study_minutes), cfg.sim, cfg=cfg)
    executor = ("原生 GPT（Codex 的开发者指令）" if cfg.engine == "codex"
                else "Claude Code（接在 Claude Code 自带的系统提示词之后）")
    return {"parts": parts, "text": "".join(p["sep"] + p["text"] for p in parts), "executor": executor,
            "guide_parts": full_parts if guided else [],
            "button_prompts": [{**item, 'overridden': item['name'] in overridden}
                               for item in catalog()],
            "personal_applied": [{"scope": p['scope'], "label": p['label'], "version": p['version']}
                                 for p in personal['entries']],
            "personal_version": personal['version'],
            "first_message": first, "overridden": sorted(overridden)}


def prompt_read(name: str) -> dict:
    from orchestrator import profiles
    pid = _profile()
    with profiles.using(pid):
        return _prompt_read(name, pid)


def _prompt_read(name: str, pid: str) -> dict:
    from orchestrator import profiles
    from orchestrator.prompting import overrides_dir, read_part, repo_part_path, repo_part_text, part_source
    name = _prompt_name(name)
    text = read_part(name)
    repo = repo_part_text(name)
    source = part_source(name)
    return {"name": name, "text": text, "version": _version(text), "overridden": (overrides_dir() / name).is_file(),
            "source_path": _rel(repo_part_path(name)),
            "inherited_from": source if source != name else None,
            "live_editable": _button_template(name), "readonly": profiles.readonly(pid),
            "repo_text": repo, "placeholders": [{"name": k, "meaning": v} for k, v in PLACEHOLDERS.items() if k in repo or k in text]}


def prompt_write(name: str, text, version) -> dict:
    """保存提示词的修改（当前配置的目录，本机原有设置是 local/prompts/；不改仓库里的文件）；改回与仓库相同时删掉修改。"""
    from orchestrator import profiles
    pid = _editable_profile("提示词")
    with profiles.using(pid):
        return _prompt_write(name, text, version, pid)


def _prompt_write(name: str, text, version, pid: str) -> dict:
    from orchestrator.prompting import check_program_placeholders, overrides_dir, read_part, repo_part_text
    name = _prompt_name(name)
    if not _button_template(name) and orchestrator_status()["running"]:
        raise ValueError("有主控在运行：等运行结束再保存提示词")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 300_000:
        raise ValueError("内容须为文本，最多 300 KB")
    if version != _version(read_part(name)):
        raise ValueError("这个文件在打开之后被改过，请重新打开再编辑")
    text = text.replace("\r\n", "\n")
    check_program_placeholders(text)
    if name == 'runtime_zh.md':
        from orchestrator.startup_prompt import validate_runtime_template
        validate_runtime_template(text)
    if name == 'interface_zh.md':
        from orchestrator.interface_sections import parse_interface
        parse_interface(text)
    repo = repo_part_text(name)
    root = overrides_dir()
    override = root / name
    if text == repo:
        override.unlink(missing_ok=True)
    else:
        root.mkdir(parents=True, exist_ok=True)
        tmp = override.with_name(override.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(override)
    # The startup renderer appends the controller's plan when an older template lacks this slot.
    missing = [k for k in PLACEHOLDERS if k in repo and k not in text and k != '{guide_read_plan}']
    return {**_prompt_read(name, pid), "missing_placeholders": missing}


def prompt_reset(name: str) -> dict:
    from orchestrator import profiles
    from orchestrator.prompting import overrides_dir
    pid = _editable_profile("提示词")
    name = _prompt_name(name)
    if not _button_template(name) and orchestrator_status()["running"]:
        raise ValueError("有主控在运行：等运行结束再恢复")
    with profiles.using(pid):
        (overrides_dir() / name).unlink(missing_ok=True)
        return _prompt_read(name, pid)


def _button_template(name):
    from orchestrator.button_prompts import catalog
    return any(item['name'] == name for item in catalog())


# ---------------------------------------------------------------- 关于页

CONSOLE_VERSION = "1.23.1"     # 控制台的版本（启动器 CelesteAIConsole.exe 的文件版本相同）
GITHUB = "https://github.com/fluency753/Celeste_AI_Play"
AUTHORS = [{"name": "Fluency", "url": "https://space.bilibili.com/490510003"},
           {"name": "Mechanic Pig", "url": "https://space.bilibili.com/384651096"}]


def _run(args: list[str], timeout: float = 5) -> str:
    import subprocess
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, cwd=REPO_ROOT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _bridge_source_version(text: str | None = None) -> str | None:
    import re
    if text is None:
        try:
            text = (REPO_ROOT / "game-bridge" / "Source" / "Bridge.cs").read_text(encoding="utf-8")
        except OSError:
            return None
    match = re.search(r'Version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else None


# ---------------------------------------------------------------- 检查更新与一键更新（关于页）

def _git(args: list[str], timeout: float = 30) -> tuple[int, str, str]:
    import subprocess
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, cwd=REPO_ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode, result.stdout.rstrip(), result.stderr.strip()   # 行首空格有意义（status --porcelain）
    except FileNotFoundError:
        return 127, "", "没找到 git"
    except subprocess.TimeoutExpired:
        return 124, "", "git 超时"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _console_version_of(text: str) -> str | None:
    import re
    match = re.search(r'^CONSOLE_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else None


def _version_at(ref: str) -> dict:
    """某个提交的版本信息：提交号、日期、说明、控制台与桥接 Mod 的版本。"""
    code, out, _ = _git(["log", "-1", "--format=%h|%cs|%s", ref], timeout=10)
    commit, date, subject = (out.split("|", 2) + ["", "", ""])[:3] if code == 0 and out else ("", "", "")
    _, console_text, _ = _git(["show", f"{ref}:overlay/console_api.py"], timeout=10)
    _, bridge_text, _ = _git(["show", f"{ref}:game-bridge/Source/Bridge.cs"], timeout=10)
    return {"commit": commit, "date": date, "subject": subject,
            "console": _console_version_of(console_text) if console_text else None,
            "bridge": _bridge_source_version(bridge_text) if bridge_text else None}


def _upstream() -> str:
    code, upstream, _ = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=10)
    return upstream if code == 0 and upstream else "origin/main"


def _dirty_files() -> list[str]:
    """本机改过、还没提交的程序文件（git status 的简短格式；改名的写新名字）。"""
    _, dirty, _ = _git(["-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=no"], timeout=10)
    return [line[3:].split(" -> ")[-1].strip('"') for line in dirty.splitlines() if line.strip()]


UPDATE_CHANNELS = {"release": "正式版", "prerelease": "公测版", "dev": "开发版"}


def _channel(channel) -> str:
    if channel not in UPDATE_CHANNELS:
        raise ValueError("更新通道须为正式版、公测版或开发版")
    return channel


def _tags() -> list[tuple[str, str, str]]:
    """本机已知的版本标签：(标签名, 指向的提交, release/prerelease)，按创建时间从新到旧。"""
    _, tags, _ = _git(["for-each-ref", "refs/tags", "--sort=-creatordate",
                       "--format=%(refname:short)%09%(objectname)%09%(*objectname)"], timeout=20)
    out = []
    for line in tags.splitlines():
        name, obj, peeled = (line.split("\t") + ["", "", ""])[:3]
        kind = "release" if RELEASE_TAG.match(name) else "prerelease" if PRERELEASE_TAG.match(name) else None
        if kind:
            out.append((name, peeled or obj, kind))
    return out


def _channel_target(upstream: str, channel: str) -> tuple[str | None, str | None]:
    """通道的最新版本：(提交, 标签)。开发版是 GitHub 主线的最新提交；正式版是主线上最新的正式版标签；
    公测版是主线上最新的公测版或正式版标签（谁新用谁）。还没有发布时为 (None, None)。"""
    if channel == "dev":
        return upstream, None
    _, mainline, _ = _git(["rev-list", upstream], timeout=30)
    order = {commit: i for i, commit in enumerate(mainline.split())}      # 0 = 最新
    best = None
    for name, commit, kind in _tags():
        if commit in order and (channel == "prerelease" or kind == "release"):
            if best is None or order[commit] < order[best[1]]:
                best = (name, commit)
    return (best[1], best[0]) if best else (None, None)


def update_check(fetch: bool = True, channel: str = "dev") -> dict:
    """从 GitHub 取所选通道（正式版、公测版、开发版）的最新版本，与本机比较。不改动本机文件。"""
    channel = _channel(channel)
    label = UPDATE_CHANNELS[channel]
    upstream = _upstream()
    remote, _, branch = upstream.partition("/")
    out = {"upstream": upstream, "channel": channel, "target": None, "current": _version_at("HEAD"), "latest": None,
           "behind": 0, "ahead": 0, "dirty": [], "can_update": False, "reason": ""}
    if not out["current"]["commit"]:
        out["reason"] = "这个目录不是 git 克隆（或没装 git），不能检查更新；请从 GitHub 重新下载"
        return out
    if fetch:
        code, _, err = _git(["fetch", "--quiet", "--tags", remote, branch], timeout=60)
        if code != 0:
            out["reason"] = f"连不上 GitHub：{err or '未知错误'}"
            return out
    target, tag = _channel_target(upstream, channel)
    if target is None:
        out["reason"] = f"GitHub 上还没有发布{label}；可以改选开发版"
        return out
    out["target"] = target
    out["latest"] = {**_version_at(target), "tag": tag}
    code, counts, _ = _git(["rev-list", "--left-right", "--count", f"HEAD...{target}"], timeout=10)
    if code == 0 and counts:
        ahead, behind = (int(x) for x in counts.split()[:2])
        out.update(ahead=ahead, behind=behind)
    _, local, _ = _git(["rev-list", "--count", f"{upstream}..HEAD"], timeout=10)
    local = int(local) if local.strip().isdigit() else 0
    out["dirty"] = _dirty_files()[:20]
    if out["behind"] == 0 and out["ahead"] == 0:
        out["reason"] = f"已经是最新版本（{label}）"
    elif local:
        out["reason"] = f"本机有 {local} 个 GitHub 上没有的提交，不能直接快进；请手动合并"
    elif out["behind"] == 0 or out["ahead"]:
        out["reason"] = (f"本机比最新的{label}（{tag or out['latest']['commit']}）还新，已经是最新版本；"
                         f"要换回{label}，用下面的“换版本”")
    elif out["dirty"]:
        out["reason"] = "本机改过程序文件（" + "、".join(out["dirty"][:5]) + "）；先撤销这些改动再更新"
    else:
        out["can_update"] = True
    return out


def _change_notes(before: dict, after: dict, changed: list[str]) -> list[str]:
    """换版本（更新或回退）之后还要手动做的事。"""
    notes = []
    if before.get("bridge") != after.get("bridge"):
        notes.append(f"桥接 Mod 换成了 {after.get('bridge')}：按部署说明重新编译并部署到游戏")
    if "requirements.txt" in changed:
        notes.append("Python 依赖有变化：运行 pip install -r requirements.txt")
    if any(path.startswith("agent-container/") for path in changed):
        notes.append("智能体容器有改动：按部署说明重建镜像")
    if any(path.startswith(("tools/launcher/", "CelesteAIConsole.exe", "overlay/console.py")) for path in changed):
        notes.append("控制台窗口本身有改动：关掉控制台窗口再打开")
    return notes


def update_apply(channel: str = "dev") -> dict:
    """一键更新到所选通道的最新版本：没有主控在运行、本机没有未提交的改动、只能快进时，从 GitHub 拉取
    （git merge --ff-only 到那个版本），然后重启 HUD 服务（控制台接口在里面）让新代码生效。"""
    channel = _channel(channel)
    if orchestrator_status()["running"]:
        raise ValueError("有主控在运行：等运行结束再更新（运行中换代码可能出错）")
    check = update_check(fetch=True, channel=channel)
    if not check["can_update"]:
        raise ValueError(check["reason"] or "现在不能更新")
    before = check["current"]
    code, changed, _ = _git(["diff", "--name-only", "HEAD", check["target"]], timeout=20)
    changed = changed.splitlines() if code == 0 else []
    code, _, err = _git(["merge", "--ff-only", check["target"]], timeout=120)
    if code != 0:
        raise ValueError(f"更新失败：{err or '未知错误'}")
    after = _version_at("HEAD")
    return {"updated": True, "before": before, "after": after, "files": len(changed),
            "notes": _change_notes(before, after, changed), "restart": True}


# ---------------------------------------------------------------- 回退版本（关于页）
# 可选的版本：GitHub 上发布的正式版（标签 vX.Y.Z）与公测版（vX.Y.Z-beta.N 等带后缀的标签），以及最近的开发版（每次提交）。
# 回退用 git reset --keep 把本机分支移到那个版本：本机数据（运行记录、配置、存档、local/）不动；之后关于页的
# “一键更新”照常快进回最新版本。所以只列出 GitHub 主线上、自带一键更新的版本，回退之后一定回得来。

RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
PRERELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+-[0-9A-Za-z][0-9A-Za-z.-]*$")
ROLLBACK_COMMITS = 30
HASH = re.compile(r"^[0-9a-f]{40}$")


def _blobs(specs: list[str]) -> dict[str, str | None]:
    """一次读出多个 “提交:路径” 的文件内容（git cat-file --batch）；不存在的为 None。"""
    import subprocess
    if not specs:
        return {}
    try:
        result = subprocess.run(["git", "cat-file", "--batch"], input="\n".join(specs).encode("utf-8") + b"\n",
                                capture_output=True, timeout=60, cwd=REPO_ROOT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {spec: None for spec in specs}
    data, pos, out = result.stdout, 0, {}
    for spec in specs:
        end = data.find(b"\n", pos)
        if end < 0:
            out[spec] = None
            continue
        header = data[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            out[spec] = data[pos:pos + size].decode("utf-8", errors="replace")
            pos += size + 1
        else:
            out[spec] = None
    return out


def _rollback_blockers(upstream: str) -> str:
    """现在为什么不能回退（空字符串 = 可以）。"""
    if orchestrator_status()["running"]:
        return "有主控在运行：等运行结束再换版本"
    code, _, _ = _git(["symbolic-ref", "-q", "--short", "HEAD"], timeout=10)
    if code != 0:
        return "本机不在分支上（例如手动检出过某个提交）：先用 git switch main 回到分支"
    changed = _dirty_files()
    if changed:
        return "本机改过程序文件（" + "、".join(changed[:5]) + "）；先撤销这些改动再换版本"
    code, counts, _ = _git(["rev-list", "--count", f"{upstream}..HEAD"], timeout=10)
    if code != 0:
        return f"读不到 GitHub 上的版本（{upstream}）：先在上面点“检查更新”"
    if counts.strip() not in ("", "0"):
        return f"本机有 {counts.strip()} 个 GitHub 上没有的提交：回退会丢掉它们，请手动处理"
    return ""


def versions(fetch: bool = True) -> dict:
    """回退版本的选项：正式版、公测版（GitHub 上的标签）与最近的开发版（提交），新的在前。不改动本机文件。"""
    upstream = _upstream()
    out = {"upstream": upstream, "current": _version_at("HEAD"), "versions": [], "can_rollback": False, "reason": ""}
    if not out["current"]["commit"]:
        out["reason"] = "这个目录不是 git 克隆（或没装 git），不能换版本；请从 GitHub 重新下载"
        return out
    if fetch:
        remote, _, branch = upstream.partition("/")
        code, _, err = _git(["fetch", "--quiet", "--tags", remote, branch], timeout=60)
        if code != 0:
            out["reason"] = f"连不上 GitHub：{err or '未知错误'}"
            return out
    code, mainline, _ = _git(["rev-list", upstream], timeout=30)
    if code != 0:
        out["reason"] = f"读不到 GitHub 上的版本（{upstream}）"
        return out
    mainline = set(mainline.split())
    _, head, _ = _git(["rev-parse", "HEAD"], timeout=10)
    rows, seen = [], set()
    for name, target, kind in _tags():
        if target in mainline and target not in seen:
            rows.append({"ref": target, "kind": kind, "tag": name})
            seen.add(target)
    _, recent, _ = _git(["log", "--first-parent", f"-n{ROLLBACK_COMMITS}", "--format=%H", upstream], timeout=20)
    for target in recent.split():
        if target not in seen:
            rows.append({"ref": target, "kind": "commit", "tag": None})
            seen.add(target)
    _, info, _ = _git(["show", "-s", "--format=%H%x09%h%x09%cs%x09%s", *[r["ref"] for r in rows]], timeout=30) if rows else (0, "", "")
    details = {parts[0]: parts[1:] for parts in (line.split("\t", 3) for line in info.splitlines()) if len(parts) == 4}
    blobs = _blobs([f"{r['ref']}:{path}" for r in rows for path in ("overlay/console_api.py", "game-bridge/Source/Bridge.cs")])
    for row in rows:
        console_text = blobs.get(f"{row['ref']}:overlay/console_api.py") or ""
        if "def update_apply" not in console_text:
            continue                              # 太旧：没有一键更新，回退之后在控制台里回不来
        commit, date, subject = details.get(row["ref"], ["", "", ""])
        bridge_text = blobs.get(f"{row['ref']}:game-bridge/Source/Bridge.cs")
        out["versions"].append({**row, "commit": commit, "date": date, "subject": subject,
                                "console": _console_version_of(console_text),
                                "bridge": _bridge_source_version(bridge_text) if bridge_text else None,
                                "current": row["ref"] == head})
    out["reason"] = _rollback_blockers(upstream)
    out["can_rollback"] = not out["reason"]
    return out


def rollback(data) -> dict:
    """回退到选中的版本（也可以选比本机新的版本）：git reset --keep，然后重启 HUD 服务让那个版本的代码生效。"""
    if not isinstance(data, dict) or set(data) != {"ref"} or not isinstance(data["ref"], str) or not HASH.match(data["ref"]):
        raise ValueError("要换到的版本无效")
    target = data["ref"]
    upstream = _upstream()
    reason = _rollback_blockers(upstream)
    if reason:
        raise ValueError(reason)
    listed = {row["ref"]: row for row in versions(fetch=False)["versions"]}
    if target not in listed:
        raise ValueError("这个版本不在可选列表里：刷新版本列表后再选")
    if listed[target]["current"]:
        raise ValueError("本机已经是这个版本")
    before = _version_at("HEAD")
    code, changed, _ = _git(["diff", "--name-only", "HEAD", target], timeout=20)
    changed = changed.splitlines() if code == 0 else []
    code, _, err = _git(["reset", "--keep", target], timeout=120)
    if code != 0:
        raise ValueError(f"换版本失败：{err or '未知错误'}；可以刷新版本列表看看本机现在是哪个版本")
    after = _version_at("HEAD")
    return {"switched": True, "before": before, "after": after, "files": len(changed),
            "notes": _change_notes(before, after, changed), "restart": True}


CLIENT_LOG = REPO_ROOT / "local" / "console-client.log"
CLIENT_LOG_MAX = 512 * 1024


def client_error(data: dict) -> dict:
    """控制台页面报来的脚本错误（读不出运行设置、未捕获的异常）：追加到本机 local/console-client.log，
    超过 512 KB 时只留后一半。只用于排查只在用户机器上出现的问题，不进运行记录。"""
    def text(key, limit):
        value = data.get(key)
        return value[:limit] if isinstance(value, str) else ""
    line = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "version": CONSOLE_VERSION,
                       "where": text("where", 80), "page": text("page", 20), "message": text("message", 1000),
                       "stack": text("stack", 4000)}, ensure_ascii=False)
    CLIENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if CLIENT_LOG.exists() and CLIENT_LOG.stat().st_size > CLIENT_LOG_MAX:
            kept = CLIENT_LOG.read_bytes()[-CLIENT_LOG_MAX // 2:]
            CLIENT_LOG.write_bytes(kept[kept.find(b"\n") + 1:])
        with CLIENT_LOG.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        return {"logged": False}
    return {"logged": True}


def about() -> dict:
    """版本、环境、项目地址、作者、更新记录与待办。"""
    import platform
    import re
    from orchestrator.gamepaths import celeste_dir_info
    git = _run(["git", "log", "-1", "--format=%h|%cs|%s"])
    commit, date, subject = (git.split("|", 2) + ["", "", ""])[:3] if git else ("", "", "")
    game = celeste_dir_info()
    installed = None
    if game.get("path"):
        try:
            manifest = (Path(game["path"]) / "Mods" / "CelesteAIBridge" / "everest.yaml").read_text(encoding="utf-8-sig")
            found = re.search(r"Version:\s*([\d.]+)", manifest)
            installed = found.group(1) if found else None
        except OSError:
            pass
    status_ = game_status()
    obs_dir, plugin, private_obs = None, False, False
    try:
        from tools.obs_setup import find_obs_dir, private_root
        obs_dir = find_obs_dir(None)
        private_obs = private_root() is not None
        plugin = (obs_dir / "obs-plugins" / "64bit" / "celeste-lockstep.dll").is_file()
    except (SystemExit, Exception):  # noqa: BLE001
        obs_dir = None
    docker = ""
    try:
        from orchestrator.config import find_docker_exe
        docker = _run([find_docker_exe(), "--version"])
    except (FileNotFoundError, OSError):
        docker = ""
    sim_version = ""
    try:
        sim_version = (REPO_ROOT / "sim" / "dist" / "VERSION").read_text(encoding="utf-8").strip()[:80]
    except OSError:
        pass
    reference = REPO_ROOT / "kb" / "reference"
    try:
        from orchestrator.codex_container import CODEX_VERSION
        from orchestrator.config import RunConfig, find_docker_exe
        image = RunConfig(agent='diagnostic', save='', model='').image
        codex = _run([find_docker_exe(), 'image', 'inspect', '--format',
                      '{{index .Config.Labels "org.celeste-ai-play.codex-version"}}', image])
        codex_ready = codex == CODEX_VERSION
    except (OSError, ValueError):
        codex, codex_ready = '', False

    def read(name: str) -> str:
        try:
            return (REPO_ROOT / name).read_text(encoding="utf-8")
        except OSError:
            return ""
    return {
        "project": "Celeste AI Play", "github": GITHUB, "authors": AUTHORS,
        "version": {"commit": commit, "date": date, "subject": subject, "console": CONSOLE_VERSION,
                    "bridge": _bridge_source_version()},
        "environment": [
            {"name": "系统", "value": platform.platform()},
            {"name": "Python", "value": platform.python_version()},
            {"name": "游戏目录", "value": game.get("path") or "没找到",
             "note": game.get("source_name") or "在「存档与章节」页填写", "ok": bool(game.get("path"))},
            {"name": "Everest", "value": "已安装" if game.get("everest") else "没找到", "ok": bool(game.get("everest"))},
            {"name": "桥接 Mod", "value": f"游戏里已装 {installed}" if installed else "没装到游戏里",
             "note": ("游戏运行中，已加载 " + status_["version"]) if status_.get("running") and status_.get("version") else
                     "游戏没在运行", "ok": bool(installed) and installed == _bridge_source_version()},
            {"name": "预置模拟器", "value": sim_version or ("已安装" if (REPO_ROOT / "sim" / "dist" / "celeste-sim").is_file() else "没有"),
             "note": "需另行配置，只在本机", "ok": (REPO_ROOT / "sim" / "dist" / "celeste-sim").is_file()},
            {"name": "知识库参考资料", "value": (f"{sum(1 for p in reference.rglob('*') if p.is_file())} 个文件"
                                          if reference.is_dir() else "没有"), "note": "kb/reference，只在本机",
             "ok": reference.is_dir()},
            {"name": "Docker", "value": docker or "没找到", "ok": bool(docker)},
            {"name": "OBS", "value": str(obs_dir) if obs_dir else "没找到",
             "note": ("私有便携版，与个人使用的 OBS 分开；" if private_obs else "")
             + ("锁步插件已安装" if plugin else ("锁步插件没装" if obs_dir else "")), "ok": bool(obs_dir and plugin)},
            {"name": "Codex", "value": ('镜像内 ' + codex) if codex_ready else '镜像需要更新',
             "note": '容器执行；账号需单独初始化，不检查宿主 CLI', "ok": codex_ready},
        ],
        "summary": read("版本摘要.md"), "changelog": read("CHANGELOG.md"), "todo": read("TODO.md"),
    }


def runs(limit: int = 40) -> list[dict]:
    """最近的运行（不含聊天会话）：设置、结果、录像与能否续跑。"""
    root = REPO_ROOT / "runs"
    found = []
    for state_path in root.glob("*/*/run_state.json"):
        directory = state_path.parent
        if not directory.name[:8].isdigit():
            continue
        found.append(directory)
    found.sort(key=lambda d: d.name, reverse=True)
    latest = latest_run()
    game = game_status()
    # 主控停下后仍保持接管就能续跑：游戏停在关卡里时冻结，停在地图或菜单上时照常运行，续跑用菜单宏进入（见 run.py）
    held_now = bool(game.get("running") and game.get("active"))
    rows = []
    from orchestrator.parked_run import parked_summary
    for index, directory in enumerate(found):
        parked = parked_summary(directory)
        available = bool(parked) and not parked.get("resumed_by")
        if index >= limit and not available:
            continue
        parked_ready = available and (parked.get("game_released") is True
                                     or bool(game.get("running") and game.get("active") is False))
        state = _read_json(directory / "run_state.json")
        summary = _read_json(directory / "summary.json")
        metrics = _read_json(directory / "metrics.json")
        recording = directory / "recording"
        from orchestrator.recording_files import finished_movie
        from orchestrator.recording import Recorder, RecordingError
        pending_recording = False
        chapter_videos = []
        try:
            saved = Recorder.saved_session(directory)
            if saved:
                recording = Path(saved['options']['dir'])
                pending_recording = is_recording_pending(saved)
                if saved.get('chapter_playlist'):
                    from orchestrator.chapter_recording import ChapterRecording
                    playlist=Path(saved['chapter_playlist']).resolve()
                    data=_read_json(playlist);ChapterRecording.validate(playlist,data)
                    recording=playlist.parent
                    for item in data['entries']:
                        movie=finished_movie(Path(item['dir']))
                        if movie:
                            chapter_videos.append({'id':item['id'],'sid':item.get('sid'),'side':item.get('side'),
                                                   'label':video_label(item.get('sid'),item.get('side')),
                                                   'movie':str(movie),'pure':str(finished_movie(Path(item['dir']),True) or '')})
        except RecordingError:
            pass  # 不可读的会话在接管时明确报错；不使整个运行列表不可用。
        rows.append({
            "dir": str(directory), "agent": state.get("agent") or directory.parent.name, "stamp": directory.name,
            "engine": state.get("engine", "claude"), "provider": state.get("provider"), "model": state.get("model"),
            "mode": state.get("mode") or ("explore" if state.get("knowledge_profile") == "explore" else "speedrun"),
            "live": bool(state.get("live_mode")), "save": state.get("save"), "chapters": state.get("all_chapters"),
            "stop_reason": summary.get("stop_reason"), "completed": summary.get("completed"),
            "game_released": summary.get("game_released"), "total_time_ticks": summary.get("total_time_ticks"),
            "deaths": summary.get("deaths", metrics.get("deaths")), "finished": bool(summary),
            "recording": bool(chapter_videos) or finished_movie(recording) is not None,
            "pure": bool(chapter_videos) and all(v['pure'] for v in chapter_videos) or finished_movie(recording, pure=True) is not None,
            "chapter_videos":chapter_videos,
            "recording_pending": pending_recording, "recording_dir": str(recording),
            "parked": available,
            "resumable": (not available and bool(summary) and summary.get("game_released") is False and latest is not None
                          and latest.resolve() == directory.resolve() and held_now)
                         or parked_ready,
        })
    return rows


def video_label(sid, side) -> str:
    """分章录像的显示名，区分面：4A、4B 是两段录像（"4A 黄金山脊"、"4B 黄金山脊 B 面"、"8C 核心 C 面"）。"""
    from orchestrator.config import SIDE_CHAPTERS, side_label
    ids = {s: i for i, s in CHAPTER_SIDS.items()}
    chapter = ids.get(sid)
    if chapter is None:
        return str(sid or "")
    if side in ("B", "C") and chapter in SIDE_CHAPTERS:
        return side_label(chapter, side)
    return CHAPTER_LABELS.get(chapter, sid)


def _launch(argv: list[str]) -> dict:
    from orchestrator.background import launch
    if orchestrator_status()["running"]:
        raise ValueError("已有主控在运行；先停止它再开新的运行")
    before = latest_run()
    host, status_ = launch(argv, service="play")
    LAUNCH_RECORD.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_RECORD.write_text(json.dumps({"host": str(host), "argv": argv, "wall": time.time(),
                                         "latest_before": str(before) if before else None}, ensure_ascii=False),
                             encoding="utf-8")
    return {"host": str(host), "status": status_}


def start(options: dict) -> dict:
    from orchestrator.launch_options import build, command_line, normalize
    argv = build(options, profile=_profile())
    o = normalize(options)
    if o["sides"]:
        # 计划了 B／C 面：启动前就按存档核对这些面已解锁（与主控开跑前的核对相同），没解锁时在控制台直接报错
        from orchestrator.saves import validate_sides
        from orchestrator.segments import Piece
        validate_sides([Piece(c, 0, None, side) for c, side in o["sides"]], create=o["save"]["create"],
                       slot=o["save"]["slot"], name=o["save"]["name"])
    return {**_launch(argv), "command": command_line(argv)}


def preview(options: dict) -> dict:
    from orchestrator.launch_options import build, command_line, normalize
    return {"command": command_line(build(options, profile=_profile())), "options": normalize(options)}


def resume(run_dir: str, fresh_session: bool = False, new_recording: bool = False) -> dict:
    from orchestrator.launch_options import command_line, resume_args
    directory = Path(run_dir)
    row = next((r for r in runs() if Path(r["dir"]).resolve() == directory.resolve()), None)
    if not row or not row["resumable"]:
        raise ValueError("只能续跑已挂起的试玩或最近一次仍保持冻结的运行")
    if row.get("parked"):
        if fresh_session:
            raise ValueError("挂起续跑须接回原会话，不能另起新会话")
        if orchestrator_status()["running"]:
            raise ValueError("请先挂起或结束当前试玩")
        from orchestrator.conversations import release_chat, selected_run
        current = selected_run(latest_run())
        if current is not None:
            release_chat(current)
    argv = resume_args(directory, bool(fresh_session), bool(new_recording), current_machine=True)
    return {**_launch(argv), "command": command_line(argv)}


def instance_create(data: dict | None = None) -> dict:
    """从现在用的游戏位置（个人安装）复制出 AI 专用副本，并把游戏位置改成它。"""
    from orchestrator import game_instance
    if orchestrator_status()["running"]:
        raise ValueError("主控还在运行，结束后再创建 AI 副本")
    name = (data or {}).get("name") or "main"
    marker = game_instance.create(name)
    game_instance.select(marker["dir"])
    return {**marker, "prefs": load_prefs()}


def instance_launch() -> dict:
    """启动现在选中的 AI 副本（Steam 没开时先静默启动），窗口出现后在后台改好标题。"""
    from orchestrator import game_instance
    return game_instance.launch(background=True)


def release(bridge_port: int = 47774) -> dict:
    """交还游戏控制（游戏恢复正常运行）：只在确认没有主控在运行时连一次桥接。"""
    if orchestrator_status()["running"]:
        raise ValueError("主控还在运行，不能从控制台交还控制")
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=bridge_port, timeout=900)
    note = None
    released = False
    abandoned = None

    def hand_back():
        nonlocal released
        if not released:
            Recorder.validate_release(saved, client.call('hello'))
            client.call("deactivate")
            released = True

    try:
        from orchestrator.recording import Recorder, RecordingError, RecordingOwnershipError
        directory = latest_run()
        try:
            saved = Recorder.saved_session(directory) if directory else None
        except RecordingError as exc:
            saved, note = None, f"原录制会话读不出来，没有收尾：{exc}"
        try:
            Recorder.validate_release(saved, client.call('hello'))
        except RecordingOwnershipError as exc:
            raise ValueError(str(exc)) from exc
        if is_recording_pending(saved):
            try:   # 采集一停就交还游戏，不等 ffmpeg 收尾与纯享版
                Recorder.finish_saved(client.call, saved, directory, on_capture_stopped=hand_back)
            except RecordingOwnershipError as exc:
                raise ValueError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001（录像收不了尾也要交还游戏）
                note = f"原录像无法收尾，已放弃：{exc}"
                try:
                    Recorder.abandon_saved(saved, exc)
                    abandoned = saved
                except Exception as save_error:
                    note += f"；放弃状态未能保存：{save_error}"
        else:
            try:
                Recorder.validate_resume(None, client.call('hello'))
            except RecordingError as exc:
                raise ValueError("OBS 还在录制另一段录像（不是这次运行保留的）：为免游戏画面继续被录进去，"
                                 "没有交还控制。请先在 OBS 里停止录制，再交还游戏控制") from exc
        hand_back()
    finally:
        client.close()
    if abandoned is not None:
        # Offline only, after the game is handed back: chapters whose encoder had
        # closed keep their footage and still receive their movie and pure cut.
        try:
            Recorder.finish_closed_offline(abandoned)
        except Exception as exc:  # noqa: BLE001
            note += f"；已关闭章节的离线收尾未完成，保持待收尾：{exc}"
    return {"released": True, **({"note": note} if note else {})}
