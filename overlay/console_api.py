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
    from orchestrator.launch_options import DEFAULTS
    from orchestrator.play_scope import sim_available
    from orchestrator.runtimes import configured_runtimes
    saves, saves_error = [], None
    try:
        from orchestrator.saves import inventory
        saves = inventory()
    except Exception as exc:  # noqa: BLE001（没有部署配置时也要能打开控制台）
        saves_error = str(exc)
    deploy = _read_json(REPO_ROOT / "configs" / "local.deploy.json")
    return {"runtimes": configured_runtimes(), "chapters": [{"id": i, "label": CHAPTER_LABELS.get(i, sid)}
                                                           for i, sid in sorted(CHAPTER_SIDS.items())],
            "saves": saves, "saves_error": saves_error, "sim_available": sim_available(REPO_ROOT),
            "defaults": {**DEFAULTS, "hud_mod": None}, "deploy": {k: deploy.get(k) for k in ("agent", "save", "chapters")}}


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
                         and latest.resolve() == directory.resolve(),
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
