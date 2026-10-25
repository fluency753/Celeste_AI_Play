"""控制台（/console）的后端：开局选项、游戏与主控状态、运行记录、启动 / 续跑 / 交还控制。

运行中的介入（发消息、暂停继续、推理强度、剧情处理、停止、中断）走 HUD 服务原有的 /api/control（RunControl），
这里只负责"开一次运行之前"与"运行结束之后"的事。

注意：桥接只接受一个连接，新连接会顶掉旧的（主控的连接就断了）。所以控制台平时不连桥接，游戏状态从桥接写进共享内存的
状态字段读（overlay/modhud.py 的 bridge_status）；只有确认没有主控在运行时，"交还控制"才会连一次桥接。
"""
from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

from orchestrator.config import CHAPTER_SIDS, REPO_ROOT

CHAPTER_LABELS = {0: "序章", 1: "1A 被遗弃之城", 2: "2A 旧址", 3: "3A 天空度假村", 4: "4A 黄金山脊", 5: "5A 镜之神殿",
                  6: "6A 映像", 7: "7A 顶峰", 8: "终章", 9: "8A 核心", 10: "第九章 告别"}
LAUNCH_RECORD = REPO_ROOT / "local" / "console" / "last_launch.json"
CONTROLLER_MUTEX = "Local\\CelesteAIPlayController"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
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
    return {"runtimes": configured_runtimes(), "chapters": [{"id": i, "label": CHAPTER_LABELS.get(i, sid)}
                                                           for i, sid in sorted(CHAPTER_SIDS.items())],
            "checkpoints": checkpoints,
            "saves": saves, "saves_error": saves_error, "sim_available": sim_available(REPO_ROOT),
            "defaults": {**DEFAULTS, "hud_mod": None}, "deploy": {k: deploy.get(k) for k in ("agent", "save", "chapters")},
            "maps": [{"id": k, "label": v, "enabled": True} for k, v in MAPS.items()]
                    + [{"id": "mod", "label": "MOD 地图", "enabled": False, "note": "以后支持"}],
            "tunables": catalog(), "tunable_groups": list(GROUPS), "prefs": load_prefs(), "game": celeste_dir_info()}


# ---------------------------------------------------------------- 控制台的本机设置（configs/local.console.json，不入库）

PREFS = REPO_ROOT / "configs" / "local.console.json"


def load_prefs() -> dict:
    data = _read_json(PREFS)
    deny = data.get("kb_deny") if isinstance(data.get("kb_deny"), dict) else {}
    return {"celeste_dir": data.get("celeste_dir") or "",
            "kb_deny": {mode: list(deny.get(mode) or []) for mode in ("speedrun", "explore")}}


def save_prefs(update: dict) -> dict:
    """保存控制台的本机设置：游戏位置（空 = 自动查找）与各模式不给智能体看的知识库文件。"""
    from orchestrator.gamepaths import is_game_dir
    from orchestrator.kb_access import normalize_deny
    if not isinstance(update, dict) or set(update) - {"celeste_dir", "kb_deny"}:
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
    if "kb_deny" in update:
        deny = update["kb_deny"]
        if not isinstance(deny, dict) or set(deny) - {"speedrun", "explore"}:
            raise ValueError("知识库权限设置无效")
        current = data.get("kb_deny") if isinstance(data.get("kb_deny"), dict) else {}
        for mode, files in deny.items():
            current[mode] = normalize_deny(mode, files)
        data["kb_deny"] = current
    PREFS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PREFS)
    return load_prefs()


# ---------------------------------------------------------------- 知识库页

def kb_list(mode: str) -> dict:
    from orchestrator.kb_access import documents, root_of
    root = root_of(mode)
    docs = documents(mode)
    known = {d["path"] for d in docs}
    deny = [x for x in load_prefs()["kb_deny"].get(mode, []) if x in known]
    sim_readme = REPO_ROOT / "sim" / "dist" / "README.md"
    return {"mode": mode, "root": root.relative_to(REPO_ROOT).as_posix(), "documents": docs, "deny": deny,
            "sim_readme": mode == "speedrun" and sim_readme.is_file()}


def kb_read(mode: str, path: str) -> dict:
    from orchestrator.kb_access import read_document
    return read_document(mode, path)


def kb_write(mode: str, path: str, text: str, mtime=None) -> dict:
    from orchestrator.kb_access import write_document
    if orchestrator_status()["running"]:
        raise ValueError("有主控在运行：知识库正挂在智能体的容器里，等运行结束再保存修改")
    return write_document(mode, path, text, mtime)


# ---------------------------------------------------------------- 提示词页

PLACEHOLDERS = {
    "{interface}": "在这里插入 interface_zh.md（工具接口）",
    "{act_max_frames}": "单次 act 最多推进的帧数（可调参数：速通 max_frames_per_act，探索 explore_act_frames）",
    "{explore_search_rule}": "探索模式自写模拟器与搜索程序推演帧数的一句（可调参数 explore_search_frames；不限时为空）",
    "{cpu_rule}": "最多可以用几个 CPU 核的一句（可调参数 agent_cpus；不限时是本机的核数，容器运行时按 Docker 实际能用的核数）",
    "{bash_default}": "Bash 不写 timeout 时的上限（可调参数 bash_default_s）",
    "{bash_max}": "Bash 最长时长（可调参数 bash_max_s）",
    "{explore_program}": "探索模式单次规划程序最长（可调参数 explore_program_s；不限时写\"不限\"）",
    "{explore_program_rule}": "探索模式单次规划程序时长的一句（\"单次规划程序最多 15 秒；\"，不限时为\"单次规划程序不限时长；\"）",
    "{playback_ratio}": "规划回放每多少条推演画一条（可调参数 playback_ratio）",
    "{playback_min_tracks}": "规划回放每轮至少画几条（可调参数 playback_min_tracks）",
    "{thought_max}": "思考语句最长字数（可调参数 thought_max_chars）",
    "{remind}": "解说提醒间隔（可调参数 remind_s / remind_live_s）",
    "{rounds}": "一屏大的房间存入的规划轮次（--rounds-per-room）",
    "{rounds4}": "两倍长、两倍高的房间的规划轮次",
    "{death}": "死亡补的规划轮次（可调参数 death_bonus）",
    "{game_tools}": "本次注册的游戏工具",
    "{game_tool_list}": "\"游戏工具有 N 个：……\"一句，按本次注册的游戏工具生成",
    "{preset_sim}": "探索模式里预设模拟器的开放情况（关限额预测时为\"禁用预设模拟器\"，开时为\"预设模拟器只能通过 simulate 按额度使用\"）",
    "{auto_handling}": "系统自动处理哪些画面（剧情是否交给智能体，按本次设置生成）",
}


def _prompt_name(name) -> str:
    from orchestrator.prompting import prompt_files
    if not isinstance(name, str) or name not in prompt_files():
        raise ValueError("没有这个提示词文件")
    return name


def _version(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prompt_preview(options: dict) -> dict:
    """按运行页的当前设置，生成智能体会收到的提示词（分块，标出来源文件与本机修改）和准备阶段的第一条消息。"""
    from types import SimpleNamespace
    from orchestrator.kb_access import root_of
    from orchestrator.kb_study import CONTAINER_KB, required_files
    from orchestrator.launch_options import config_for
    from orchestrator.prompting import overridden_parts, prompt_parts, study_prompt
    # 预览不需要真的起名字或选存档：没填的用占位值，免得运行页还没填完就看不了提示词
    options = dict(options or {})
    if not options.get("agent"):
        options["agent"] = "preview"
    if not (options.get("save") or {}).get("name"):
        options["save"] = {"name": "preview", "slot": None, "create": False}
    cfg = config_for(options)
    overridden = set(overridden_parts())
    parts = [{"label": p.label, "text": p.text, "source": p.source, "sep": p.sep,
              "overridden": p.source in overridden} for p in prompt_parts(cfg)]
    if cfg.engine == "codex":
        from orchestrator.codex_agent import codex_tool_mapping
        for part in parts:
            part["text"] = part["text"].replace("调用 simulate 工具", "调用 celeste_simulate 工具")
        parts.append({"label": "原生 Codex 工具映射（自动加）", "text": codex_tool_mapping(cfg), "source": None,
                      "sep": "\n\n", "overridden": False})
    first = None
    if cfg.study and cfg.runtime == "container":
        files = [rel for rel in required_files(root_of(cfg.mode)) if rel not in set(cfg.kb_deny)]
        paths = [f"{CONTAINER_KB}/{rel}" for rel in files]
        if cfg.sim:
            k = next((i for i, rel in enumerate(files) if rel.startswith("reference/")), len(files))
            paths.insert(k, "/sim/README.md")
        first = study_prompt(SimpleNamespace(container_paths=lambda: paths, minutes=cfg.study_minutes), cfg.sim)
    executor = ("原生 GPT（Codex 的开发者指令）" if cfg.engine == "codex"
                else "Claude Code（接在 Claude Code 自带的系统提示词之后）")
    return {"parts": parts, "text": "".join(p["sep"] + p["text"] for p in parts), "executor": executor,
            "first_message": first, "overridden": sorted(overridden)}


def prompt_read(name: str) -> dict:
    from orchestrator.prompting import OVERRIDES, PROMPTS, read_part
    name = _prompt_name(name)
    text = read_part(name)
    repo = (PROMPTS / name).read_text(encoding="utf-8")
    return {"name": name, "text": text, "version": _version(text), "overridden": (OVERRIDES / name).is_file(),
            "repo_text": repo, "placeholders": [{"name": k, "meaning": v} for k, v in PLACEHOLDERS.items() if k in repo]}


def prompt_write(name: str, text, version) -> dict:
    """保存提示词的本机修改（local/prompts/，不改仓库里的文件）；改回与仓库相同时删掉本机修改。"""
    from orchestrator.prompting import OVERRIDES, PROMPTS, read_part
    name = _prompt_name(name)
    if orchestrator_status()["running"]:
        raise ValueError("有主控在运行：等运行结束再保存提示词")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 300_000:
        raise ValueError("内容须为文本，最多 300 KB")
    if version != _version(read_part(name)):
        raise ValueError("这个文件在你打开之后被改过，请重新打开再编辑")
    text = text.replace("\r\n", "\n")
    repo = (PROMPTS / name).read_text(encoding="utf-8")
    override = OVERRIDES / name
    if text == repo:
        override.unlink(missing_ok=True)
    else:
        OVERRIDES.mkdir(parents=True, exist_ok=True)
        tmp = override.with_name(override.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(override)
    missing = [k for k in PLACEHOLDERS if k in repo and k not in text]
    return {**prompt_read(name), "missing_placeholders": missing}


def prompt_reset(name: str) -> dict:
    from orchestrator.prompting import OVERRIDES
    name = _prompt_name(name)
    if orchestrator_status()["running"]:
        raise ValueError("有主控在运行：等运行结束再恢复")
    (OVERRIDES / name).unlink(missing_ok=True)
    return prompt_read(name)


# ---------------------------------------------------------------- 关于页

CONSOLE_VERSION = "1.0.1"     # 控制台的版本（启动器 CelesteAIConsole.exe 的文件版本相同）
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


def _bridge_source_version() -> str | None:
    import re
    try:
        text = (REPO_ROOT / "game-bridge" / "Source" / "Bridge.cs").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'Version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else None


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
    obs_dir, plugin = None, False
    try:
        from tools.obs_setup import find_obs_dir
        obs_dir = find_obs_dir(None)
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
        from orchestrator.codex_rpc import codex_executable
        codex = codex_executable()
    except Exception:  # noqa: BLE001（没装 Codex 时它会报错）
        codex = ""

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
             "note": "只在本机，不入库", "ok": (REPO_ROOT / "sim" / "dist" / "celeste-sim").is_file()},
            {"name": "知识库参考资料", "value": (f"{sum(1 for p in reference.rglob('*') if p.is_file())} 个文件"
                                          if reference.is_dir() else "没有"), "note": "kb/reference，只在本机",
             "ok": reference.is_dir()},
            {"name": "Docker", "value": docker or "没找到", "ok": bool(docker)},
            {"name": "OBS", "value": str(obs_dir) if obs_dir else "没找到",
             "note": "锁步插件已安装" if plugin else ("锁步插件没装" if obs_dir else ""), "ok": bool(obs_dir and plugin)},
            {"name": "Codex", "value": codex or "没找到", "note": "原生 GPT 执行器用", "ok": bool(codex)},
        ],
        "changelog": read("CHANGELOG.md"), "todo": read("TODO.md"),
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
    frozen_now = bool(game.get("running") and game.get("active"))   # 游戏此刻仍被接管、冻结着
    rows = []
    for directory in found[:limit]:
        state = _read_json(directory / "run_state.json")
        summary = _read_json(directory / "summary.json")
        metrics = _read_json(directory / "metrics.json")
        recording = directory / "recording"
        rows.append({
            "dir": str(directory), "agent": state.get("agent") or directory.parent.name, "stamp": directory.name,
            "engine": state.get("engine", "claude"), "provider": state.get("provider"), "model": state.get("model"),
            "mode": state.get("mode") or ("explore" if state.get("knowledge_profile") == "explore" else "speedrun"),
            "live": bool(state.get("live_mode")), "save": state.get("save"), "chapters": state.get("all_chapters"),
            "stop_reason": summary.get("stop_reason"), "completed": summary.get("completed"),
            "game_released": summary.get("game_released"), "total_time_ticks": summary.get("total_time_ticks"),
            "deaths": summary.get("deaths", metrics.get("deaths")), "finished": bool(summary),
            "recording": (recording / "gameplay.mkv").exists(), "pure": (recording / "gameplay_pure.mkv").exists(),
            "resumable": bool(summary) and summary.get("game_released") is False and latest is not None
                         and latest.resolve() == directory.resolve() and frozen_now,
        })
    return rows


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
    from orchestrator.launch_options import build, command_line
    argv = build(options)
    return {**_launch(argv), "command": command_line(argv)}


def preview(options: dict) -> dict:
    from orchestrator.launch_options import build, command_line, normalize
    return {"command": command_line(build(options)), "options": normalize(options)}


def resume(run_dir: str, fresh_session: bool = False) -> dict:
    from orchestrator.launch_options import command_line, resume_args
    directory = Path(run_dir)
    row = next((r for r in runs() if Path(r["dir"]).resolve() == directory.resolve()), None)
    if not row or not row["resumable"]:
        raise ValueError("只能续跑最近一次停下时保持冻结的运行")
    argv = resume_args(directory, bool(fresh_session))
    return {**_launch(argv), "command": command_line(argv)}


def release(bridge_port: int = 47774) -> dict:
    """交还游戏控制（游戏恢复正常运行）：只在确认没有主控在运行时连一次桥接。"""
    if orchestrator_status()["running"]:
        raise ValueError("主控还在运行，不能从控制台交还控制")
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=bridge_port, timeout=10)
    try:
        client.call("deactivate")
    finally:
        client.close()
    return {"released": True}
