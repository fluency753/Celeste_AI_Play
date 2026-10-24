"""主控入口：进入存档 → 接管游戏 → 启动游戏工具服务 → 启动智能体 → 收尾。

用法（在仓库根目录）：
    python -m orchestrator.run --agent dev-opus --save test --model opus
    python -m orchestrator.run --agent dev-opus --save test --model opus --max-minutes 20 --effort medium
    python -m orchestrator.run --agent dev --save test --no-agent      # 只启动工具服务，用于手动调试
    python -m orchestrator.run --agent opus5.5 --save opus5.5 --record # 同时用 OBS 录制（先 python tools/obs_setup.py launch）
    python -m orchestrator.run --agent opus5.5 --save opus5.5 --model opus --chapters 0,1,2 --record   # 正式挑战（默认开预置模拟器）
    python -m orchestrator.run --agent opus5.5 --save opus5.5 --resume runs/opus5.5/<时间戳> --record     # 中断后续跑
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import importlib.util
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn

from .agent import ClaudeRunner, resume_prompt
from .config import (CHAPTER_SIDS, REPO_ROOT, RunConfig, chapter_name, chapter_name_by_id, load_api_key, load_dotenv,
                     load_provider)
from .container import ensure_container
from .game_session import GameSession
from .kb_study import KbStudy
from .metrics import Metrics
from .recording import Recorder, RecordingError, options_from
from .pausegate import PauseGate
from .live import LiveStatus
from .playback import Playback
from .server import LIVE_REMINDER, TRANSPORT_SECURITY, build_server


def parse_args() -> tuple[RunConfig, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="智能体标签，用于日志目录与独立配置目录，如 opus5.5、dev-opus")
    ap.add_argument("--save", required=True, help="游戏内存档名，如 test、opus5.5、fable5.1")
    ap.add_argument("--model", default="opus", help="Claude Code 模型别名：opus / fable 等（默认 opus）；第三方提供方写它的模型名")
    ap.add_argument("--provider", default="anthropic",
                    help="模型提供方（configs/providers.json）：anthropic（默认，Max 订阅）、deepseek 等。"
                         "第三方提供方的 API 密钥只从本机读取（configs/local.providers.json 的 key_file，或环境变量）")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-minutes", type=float, default=240.0, help="现实时间上限（分钟）；目标以游戏内计时为主，这里只防止无限运行")
    ap.add_argument("--bridge-port", type=int, default=47774)
    ap.add_argument("--mcp-port", type=int, default=47780)
    ap.add_argument("--play-speed", type=int, default=1, help="智能体动作段的推进速度（每次渲染推进几帧）")
    ap.add_argument("--auto-speed", type=int, default=1, help="转场、剧情、重生段的推进速度；1 与原版一致，大于 1 为快进（只用于调试，录像会失真）")
    ap.add_argument("--chapter", type=int, help="进入的章节编号：0 序章、1 被遗弃之城、2 旧址；默认进入选章页当前选中的章节")
    ap.add_argument("--chapters", help="依次通关的章节编号，逗号分隔，例如 0,1,2（正式挑战）：一章通关后主控自动走完结算画面、"
                                       "进入下一章，智能体接着玩；给了它就不用 --chapter")
    ap.add_argument("--skip-enter", action="store_true", help="已在关卡内且可操作时，跳过菜单宏")
    ap.add_argument("--no-agent", action="store_true", help="不启动智能体，只运行工具服务直到 Ctrl+C")
    ap.add_argument("--autocompact", help="上下文自动压缩的窗口大小：auto，或 100000~1000000 的 token 数（默认用 Claude Code 的设置）")
    ap.add_argument("--no-study", action="store_true", help="跳过开始前的读知识库阶段（调试用）")
    ap.add_argument("--study-minutes", type=float, default=60.0,
                    help="准备阶段（读知识库、写笔记）的上限分钟数，不计入现实用时；到时自动开放游戏工具")
    ap.add_argument("--max-usage", type=float, default=None,
                    help="可选：Max 套餐 5 小时或 7 天窗口的已用比例达到该值（0~1）就提前暂停智能体、等额度重置后接着玩；"
                         "默认不设，一直用到额度真正用完（用完时同样冻结游戏、等重置后接着玩）")
    ap.add_argument("--hud-port", type=int, default=47790, help="HUD 页面与数据的端口（OBS 浏览器源与置顶窗口都连它）")
    ap.add_argument("--limit-planning", action="store_true",
                    help="限制规划轮次：按章节记账，每进入一个没来过的房间按房间大小存入轮次，死亡补 1 轮，"
                         "只有调用模拟器的消息占一轮，用完后不能再跑模拟器；自动重试关闭。默认不限制思考与调用")
    ap.add_argument("--free-planning", action="store_true", help=argparse.SUPPRESS)  # 旧开关：现在不限制就是默认
    ap.add_argument("--rounds-per-room", type=int, default=3,
                    help="（--limit-planning 时）一屏大小（320×180，1A 第一个房间）的新房间存入的规划轮次（默认 3，至少 3）；大房间按 --room-scale 放大")
    ap.add_argument("--room-scale", type=float, default=2.0,
                    help="房间的长、高每变成这么多倍，存入的轮次乘这么多（默认 2，即与面积成正比），向上取整")
    ap.add_argument("--no-playback", action="store_true",
                    help="不把推演过的路线画进游戏（规划回放：开模拟器时画 /sim 的推演，关模拟器时画智能体用 show.trajectory 记下的轨迹）")
    ap.add_argument("--no-hud", action="store_true", help="不在主控进程里启动 HUD 服务（也不打开置顶 HUD 窗口）")
    ap.add_argument("--no-hud-window", action="store_true",
                    help="不在屏幕上打开置顶 HUD 窗口（默认打开：贴在游戏窗口右上角，点击穿透、不抢焦点；录像里的 HUD 不受影响）")
    ap.add_argument("--record", action="store_true",
                    help="用 OBS 锁步录制：只录游戏真正推进的帧，成片在日志目录的 recording/ 下（需要先启动 OBS）")
    ap.add_argument("--record-marker", action="store_true", help="录制时在画面左下角画帧号条码（只用于校准）")
    ap.add_argument("--ffmpeg", help="ffmpeg 路径（默认：configs/local.recording.json → PATH → winget 安装位置）")
    ap.add_argument("--record-video-args", help="ffmpeg 视频编码参数（默认见 configs/recording.json）")
    ap.add_argument("--audio-offset-ms", type=float, help="声音相对画面的偏移毫秒数（默认用校准结果，见 configs/local.recording.json）")
    ap.add_argument("--sim", dest="sim", action="store_true", default=None,
                    help="开放预置模拟器（容器运行方式下默认开启，写不写都一样）：本机 sim/dist 只读挂到容器的 /sim，"
                         "每次停下把当前帧的完整场景写到 /workspace/game/sim_state.json")
    ap.add_argument("--live", dest="live", action="store_true", default=None,
                    help="实时展示（默认开启，写不写都一样）：要求智能体思考期间用 narrate 工具随时更新 HUD 上的思考内容，"
                         "运行程序期间用 /workspace/game/show.py 的 status() 报告进度（HUD 工具栏实时显示）")
    ap.add_argument("--no-live", dest="live", action="store_false",
                    help="关闭实时展示：HUD 的思考内容只在每次 act 时更新（之前的做法）")
    ap.add_argument("--no-sim", dest="sim", action="store_false",
                    help="关模拟器的版本：不开放预置模拟器，智能体只能用自己的办法通关；提示词不教打法，只写目标、规则与工具接口"
                         "（算出的逐帧轨迹可以用工作区的 show.trajectory() 交给系统画出来）")
    ap.add_argument("--runtime", choices=["container", "host"], default="container",
                    help="container：智能体在 Docker 容器里运行（默认）；host：在本机运行且只开放游戏工具（M2 的过渡方式）")
    ap.add_argument("--no-quota-wait", action="store_true",
                    help="额度用完时直接停止（默认：游戏保持冻结，等额度重置后接着同一个会话继续）")
    ap.add_argument("--quota-wait-max-hours", type=float, default=24.0,
                    help="额度重置还要等超过这么多小时就停止本次运行（之后可用 --resume 续跑），默认 24")
    ap.add_argument("--quota-wait-cap-seconds", type=float, help=argparse.SUPPRESS)  # 只用于测试：等待额度的最长秒数
    ap.add_argument("--resume", metavar="RUN_DIR",
                    help="续跑一次中断的运行（主控退出、断电以外的意外）：游戏须仍冻结在原处；接着原来的章节队列、"
                         "统计与 Claude Code 会话。模型、推理强度、章节、--sim 等沿用原运行的设置")
    ap.add_argument("--fresh-session", action="store_true",
                    help="与 --resume 一起用：不接原来的 Claude Code 会话，开一个新会话（原会话无法恢复时用）")
    args = ap.parse_args()
    previous = args.previous = load_previous(args.resume) if args.resume else None
    if previous:
        state = previous["state"]
        for key in ("model", "effort", "max_minutes", "runtime", "provider"):
            flag = f"--{key.replace('_', '-')}"
            if not any(a == flag or a.startswith(flag + "=") for a in sys.argv):
                setattr(args, key, state.get(key, getattr(args, key)))
        if not any(a == "--effort" or a.startswith("--effort=") for a in sys.argv):
            # 推理强度沿用中断时生效的那个（智能体可能用 set_effort 调整过），而不是开局的值
            args.effort = previous["metrics"].get("effort") or args.effort
        if args.sim is None:
            args.sim = bool(state["sim"])
        if args.live is None:
            args.live = bool(state.get("live", True))
        args.skip_enter = True
        args.no_study = True
    cfg = RunConfig(agent=args.agent, save=args.save, model=args.model, effort=args.effort, provider=args.provider,
                    bridge_port=args.bridge_port, mcp_port=args.mcp_port, max_minutes=args.max_minutes,
                    play_speed=args.play_speed, auto_speed=args.auto_speed, skip_enter=args.skip_enter)
    cfg.chapter = args.chapter
    if args.chapters:
        cfg.chapters = [int(x) for x in args.chapters.split(",") if x.strip()]
        if not cfg.chapters:
            raise SystemExit("--chapters 至少要给一个章节编号")
        cfg.chapter = cfg.chapters[0]
    cfg.runtime = args.runtime
    cfg.autocompact = args.autocompact
    cfg.study = not args.no_study
    cfg.study_minutes = args.study_minutes
    cfg.max_usage = args.max_usage
    cfg.quota_wait = not args.no_quota_wait
    cfg.quota_wait_cap_s = args.quota_wait_cap_seconds
    cfg.quota_wait_max_h = args.quota_wait_max_hours
    if args.sim is None:
        args.sim = args.runtime == "container"    # 默认开预置模拟器；本机运行方式没有模拟器
    cfg.sim = args.sim
    cfg.pause_limit = bool(args.limit_planning) and not args.free_planning
    cfg.rounds_per_room = max(1, args.rounds_per_room)
    cfg.room_scale = max(1.0, args.room_scale)
    cfg.playback = not args.no_playback
    cfg.live = args.live is not False     # 默认开启
    cfg.provider_info = load_provider(cfg.provider)
    check_provider(cfg.provider_info)
    if cfg.third_party and cfg.runtime != "container":
        raise SystemExit("第三方模型提供方只支持容器运行方式")
    if previous:
        state = previous["state"]
        if (args.agent, args.save) != (state["agent"], state["save"]):
            raise SystemExit(f"--resume 的运行是 --agent {state['agent']} --save {state['save']}，与这次给的不一致")
        done = len(previous["metrics"].get("splits") or [])
        remaining = state["all_chapters"][done:] if state.get("all_chapters") else []
        if state.get("all_chapters") and not remaining:
            raise SystemExit("这次运行的章节已经全部通关，不需要续跑")
        cfg.chapters = remaining or None
        cfg.chapter = remaining[0] if remaining else None
    if cfg.pause_limit and not cfg.sim:
        raise SystemExit("--limit-planning 只用于开模拟器的版本（关模拟器的版本不限制思考与调用）")
    if cfg.sim:
        if cfg.runtime != "container":
            raise SystemExit("预置模拟器只能用于容器运行方式（本机运行方式请加 --no-sim）")
        if not (cfg.sim_dir / "celeste-sim").is_file():
            raise SystemExit(f"找不到预置模拟器：{cfg.sim_dir / 'celeste-sim'}（模拟器只在本机，见使用说明的预置模拟器一节；"
                             "没有模拟器时用 --no-sim 运行关模拟器的版本）")
    return cfg, args


def load_previous(path: str) -> dict:
    """读出要续跑的那次运行：run_state.json（设置与会话）和 metrics.json（统计）。"""
    run_dir = Path(path)
    if not run_dir.is_absolute():
        run_dir = (Path.cwd() / run_dir) if (Path.cwd() / run_dir).exists() else REPO_ROOT / run_dir
    try:
        state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"--resume：{run_dir} 里找不到 {Path(exc.filename).name}（只能续跑带 run_state.json 的运行）")
    return {"dir": run_dir, "state": state, "metrics": metrics}


def write_run_state(run_dir: Path, cfg: RunConfig, session_id: str | None, previous: dict | None) -> None:
    """记下续跑需要的设置与 Claude Code 会话编号。"""
    all_chapters = previous["state"].get("all_chapters") if previous else (list(cfg.chapters) if cfg.chapters else None)
    state = {"agent": cfg.agent, "save": cfg.save, "model": cfg.model, "provider": cfg.provider,
             "effort": cfg.effort, "max_minutes": cfg.max_minutes,
             "runtime": cfg.runtime, "sim": cfg.sim, "live": cfg.live, "all_chapters": all_chapters, "session_id": session_id,
             "resumed_from": str(previous["dir"]) if previous else None}
    (run_dir / "run_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_provider(info: dict) -> None:
    """未实测的提供方给出提示；经本机网关接入的（例如 GPT）开跑前检查网关在不在运行。"""
    status = str(info.get("status") or "ready")
    if status != "ready":
        print(f"[run] 注意：模型提供方 {info.get('label') or info.get('name')} {status}", flush=True)
    url = info.get("gateway_check")
    if url:
        import urllib.request  # noqa: WPS433
        try:
            with urllib.request.urlopen(url, timeout=5):
                pass
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"连不上 {info.get('name')} 的接口转换网关（{url}）：{exc}。先按 docs/DEPLOY.md 的说明在本机启动网关")


def load_token(cfg: RunConfig) -> str:
    """Anthropic：Max 订阅令牌（.env）；第三方提供方：本机保存的 API 密钥。都只在内存里，不打印、不写文件。"""
    if cfg.third_party:
        return load_api_key(cfg.provider_info or {})
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or load_dotenv(REPO_ROOT / ".env").get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        raise SystemExit("缺少 CLAUDE_CODE_OAUTH_TOKEN：请按使用说明 2.1 节运行 claude setup-token，并写入仓库根目录的 .env")
    return token


def make_run_dir(agent: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPO_ROOT / "runs" / agent / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "runs" / "latest.txt").write_text(str(run_dir), encoding="utf-8")
    return run_dir


def enter_save(cfg: RunConfig, recorder: Recorder | None = None) -> None:
    """菜单宏进入存档。录制时开局也录进去：先（不录、快进）退回选档页，从那里开始录制，
    再按正常速度选存档、进地图、选章节、进关卡、用暂停菜单跳过开场剧情。"""
    from tools.enter_save import enter_save as macro, to_file_select  # noqa: WPS433（延迟导入，避免 --help 时连接游戏）
    from tools.bridge_client import BridgeClient

    client = BridgeClient(port=cfg.bridge_port, timeout=120)
    try:
        t0 = time.monotonic()
        if recorder is not None:
            to_file_select(client)
            recorder.call = client.call
            recorder.start()
            print("[record] 从选档页开始录制：选存档、进章节、跳过开场都按正常速度录进录像", flush=True)
            obs = macro(client, cfg.save, chapter=cfg.chapter, menu_speed=1)
        else:
            obs = macro(client, cfg.save, chapter=cfg.chapter)
        print(f"[run] 已进入存档 {cfg.save}：{obs['level']['sid']} 房间 {obs['level']['room']}"
              f"（菜单宏用时 {time.monotonic() - t0:.1f} 秒）", flush=True)
    except BaseException:
        if recorder is not None and recorder.started:
            recorder.stop()
        raise
    finally:
        client.close()


def add_gate_route(app, gate: PauseGate, metrics=None, live_remind_s: float | None = None) -> None:
    """容器里的 PreToolUse 钩子（agent-container/celeste-gate.py）每次调用工具前 POST 到这里。
    实时展示时（live_remind_s 不为空）兼作半分钟计时器的提醒通道：思考状态累计满 live_remind_s 秒没有 narrate，
    回复里带上 context，钩子把它作为 additionalContext 交给 Claude Code，出现在这次工具结果旁边。"""
    from starlette.requests import Request  # noqa: WPS433
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def endpoint(request: Request):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        tool_name = str(data.get("tool_name") or "")
        allow, reason = await gate.decide(tool_name, data.get("tool_use_id"))
        context = ""
        if allow and live_remind_s and metrics is not None:
            if tool_name.endswith("__narrate"):
                metrics.narrated()
            elif (waited := metrics.take_live_reminder(live_remind_s)) is not None:
                context = LIVE_REMINDER.format(s=int(waited))
                print(f"[live] 提醒更新 HUD 思考（已思考 {waited:.0f} 秒，调用 {tool_name}）", flush=True)
        return JSONResponse({"decision": "allow" if allow else "deny", "reason": reason, "context": context})

    app.router.routes.insert(0, Route("/gate", endpoint, methods=["POST"]))


def write_placeholder_metrics(run_dir: Path, cfg: RunConfig) -> None:
    """开局录制期间 HUD 显示的内容：名字、推理强度与"等待"，计时都是 0（主控接管后由 Metrics 接着写）。"""
    now = time.time()
    data = {"agent": cfg.agent, "model": cfg.model, "effort": cfg.effort, "status": "starting", "started_wall": 0.0,
            "budget_s": cfg.budget_seconds, "chapter": "", "room": "", "thought": "", "written_wall": now,
            "phase_since_wall": now, "thinking_since_wall": now}
    (run_dir / "metrics.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def check_resume_position(cfg: RunConfig) -> None:
    """续跑前确认游戏停在要续跑的章节里；中断发生在两章之间（结算画面、地图）时，用菜单宏进入下一章。"""
    from tools.bridge_client import BridgeClient
    from tools.enter_save import enter_save as macro  # noqa: WPS433

    client = BridgeClient(port=cfg.bridge_port, timeout=120)
    try:
        obs = client.call("observe", image=False, entities=False, tiles=False)["obs"]
        save = (obs.get("save") or {}).get("name")
        if save != cfg.save:
            raise SystemExit(f"游戏当前的存档是 {save!r}，不是 {cfg.save!r}")
        sid = (obs.get("level") or {}).get("sid")
        want = CHAPTER_SIDS.get(cfg.chapter) if cfg.chapter is not None else sid
        if obs.get("scene") == "Level":
            if sid != want:
                raise SystemExit(f"游戏当前在 {chapter_name(sid)}，续跑的章节是 {chapter_name(want)}：请确认 --resume 的目录")
            print(f"[run] 续跑：游戏停在 {chapter_name(sid)} 房间 {(obs.get('level') or {}).get('room')}", flush=True)
            return
        if cfg.chapter is None:
            raise SystemExit(f"游戏当前不在关卡里（{obs.get('scene')}），无法续跑")
        print(f"[run] 续跑：游戏在 {obs.get('scene')}，进入{chapter_name_by_id(cfg.chapter)}", flush=True)
        obs = macro(client, cfg.save, chapter=cfg.chapter, menu_speed=1)
        print(f"[run] 已进入 {obs['level']['sid']} 房间 {obs['level']['room']}", flush=True)
    finally:
        client.close()


def backup_save(obs: dict, run_dir: Path) -> None:
    """开跑前把当前存档的全部文件（N.celeste 与 Everest 的 N-modsave-*、N-modsession-*、N-modsavedata）
    复制到本次运行的目录，出问题时可以手动还原。"""
    path = (obs.get("save") or {}).get("file")
    if not path:
        print("[run] 提示：桥接没有报告存档文件的位置（桥接 0.10.2 以上才有），跳过存档备份", flush=True)
        return
    src = Path(path)
    dest = run_dir / "save_backup"
    copied = []
    candidates = [src, *sorted(src.parent.glob(f"{src.stem}-*{src.suffix}"))] if src.parent.is_dir() else [src]
    for candidate in candidates:
        if candidate.is_file():
            dest.mkdir(exist_ok=True)
            shutil.copy2(candidate, dest / candidate.name)
            copied.append(candidate.name)
    print(f"[run] 已备份存档 {len(copied)} 个文件（{src.name} 等）→ {dest}" if copied
          else "[run] 提示：存档文件还不存在，跳过备份", flush=True)


def start_hud_window(port: int, run_dir: Path) -> subprocess.Popen | None:
    """在屏幕上打开置顶 HUD 窗口（只在 Windows）：贴在游戏窗口右上角、点击穿透、不抢焦点，游戏最小化时隐藏。
    录像里的 HUD 来自 OBS 的浏览器源，与它无关；OBS 的游戏采集也采不到这个窗口，所以录像里不会出现两份。"""
    if sys.platform != "win32":
        return None
    import ctypes  # noqa: WPS433

    if ctypes.windll.user32.FindWindowW(None, "Celeste AI HUD"):
        print("[run] 置顶 HUD 窗口已经开着，沿用", flush=True)
        return None
    if importlib.util.find_spec("webview") is None:
        print("[run] 提示：没装 pywebview，不打开置顶 HUD 窗口（.venv 里 pip install -r requirements.txt）", flush=True)
        return None
    log = open(run_dir / "hud_window.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "overlay.window", "--url", f"http://127.0.0.1:{port}/hud"],
                            cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
                            creationflags=0x08000000)  # CREATE_NO_WINDOW：不弹出控制台
    print("[run] 已打开置顶 HUD 窗口（贴在游戏窗口右上角；--no-hud-window 关闭）", flush=True)
    return proc


def stop_hud_window(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def run_async(cfg: RunConfig, args: argparse.Namespace, run_dir: Path, token: str | None,
                    previous: dict | None = None, recorder: Recorder | None = None) -> dict:
    study = None
    if cfg.study and cfg.runtime == "container" and not args.no_agent:
        extra = {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None
        study = KbStudy(REPO_ROOT / "kb", cfg.study_minutes, extra)
        if not study.has_reference:
            print("[run] 提示：kb/reference/ 不存在，准备阶段只读知识库本身（见使用说明 2.5 节生成参考资料原文）", flush=True)
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds,
                      study=study is not None)
    if previous:
        metrics.restore(previous["metrics"], str(previous["dir"]))
    if not args.no_hud:
        from overlay.server import start_in_thread  # noqa: WPS433
        hud = start_in_thread(args.hud_port)
        print(f"[run] HUD：http://127.0.0.1:{args.hud_port}/hud"
              + ("" if hud else "（已在运行，沿用）"), flush=True)
    hud_window = None if (args.no_hud or args.no_hud_window) else start_hud_window(args.hud_port, run_dir)
    try:
        return await _run_session(cfg, args, run_dir, token, previous, study, metrics, recorder)
    finally:
        stop_hud_window(hud_window)


async def _run_session(cfg: RunConfig, args: argparse.Namespace, run_dir: Path, token: str | None,
                       previous: dict | None, study: KbStudy | None, metrics: Metrics,
                       recorder: Recorder | None = None) -> dict:
    session = GameSession(cfg, run_dir, metrics)
    obs = session.connect()
    print(f"[run] 已接管游戏：{obs['level']['sid']} 房间 {obs['level']['room']}，存档 {cfg.save}", flush=True)
    if previous:
        session.decision_index = int(previous["metrics"].get("decisions") or 0)
        print(f"[run] 续跑 {previous['dir']}：已通关 {len(metrics.state.splits or [])} 章，现实用时已用 "
              f"{metrics.elapsed() / 60:.0f} 分钟" + (f"，剩余章节 {cfg.chapters}" if cfg.chapters else ""), flush=True)
    backup_save(obs, run_dir)
    resumed_session = previous["state"].get("session_id") if previous and not args.fresh_session else None
    session_id = resumed_session or str(uuid.uuid4())
    write_run_state(run_dir, cfg, None if args.no_agent else session_id, previous)
    if recorder is not None:
        recorder.call = session.bridge_call  # 开局时已经开始录制（见 enter_save），之后改用主控的连接
    elif args.record:
        recorder = Recorder(session.bridge_call,
                            options_from(run_dir, args.ffmpeg, args.record_video_args, args.audio_offset_ms, args.record_marker),
                            log=lambda m: print(m, flush=True))
        try:
            recorder.start()
        except RecordingError:
            session.close()
            raise
    if recorder is not None:
        session.before_advance = recorder.check_window

    gate = PauseGate(metrics, enabled=cfg.pause_limit and cfg.runtime == "container",
                     log=lambda m: print(m, flush=True), rounds_per_room=cfg.rounds_per_room,
                     death_bonus=cfg.death_bonus, started=previous is not None, sim_only=cfg.sim,
                     state_path=cfg.workspace_dir / "game" / "rounds.json" if cfg.runtime == "container" else None,
                     room_scale=cfg.room_scale)
    playback = Playback(cfg, session, metrics, gate, log=lambda m: print(m, flush=True))
    if playback.collect:
        session.playback = playback
    live = None
    metrics.update(live=bool(cfg.live))
    if cfg.live and session.workspace is not None:
        live = LiveStatus(session.workspace / "game" / "hud_status.json", metrics).start()
    server = build_server(session, study, gate)
    app = server.streamable_http_app(streamable_http_path="/mcp", session_idle_timeout=None, host="127.0.0.1",
                                     transport_security=TRANSPORT_SECURITY)
    add_gate_route(app, gate, metrics, cfg.live_remind_s if cfg.live and cfg.runtime == "container" else None)
    web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cfg.mcp_port, log_level="warning"))
    web_task = asyncio.create_task(web.serve())
    while not web.started:
        if web_task.done():
            raise RuntimeError(f"游戏工具服务启动失败（端口 {cfg.mcp_port}）")
        await asyncio.sleep(0.05)
    print(f"[run] 游戏工具服务：http://127.0.0.1:{cfg.mcp_port}/mcp", flush=True)

    reason = "中断"
    runner = None
    try:
        if args.no_agent:
            print("[run] --no-agent：工具服务已就绪，按 Ctrl+C 结束", flush=True)
            await web_task
            reason = "手动结束"
        else:
            container = None
            if cfg.runtime == "container":
                container = await asyncio.to_thread(ensure_container, cfg, lambda m: print(m, flush=True))
                print(f"[run] 智能体容器：{container}（工作区 {cfg.workspace_dir}）", flush=True)
                session.container = container
            note = None
            if previous:
                gap = max(0.0, time.time() - float(previous["metrics"].get("written_wall") or time.time())) / 60
                if resumed_session:
                    note = resume_prompt("主控中断后续跑", gap)
                else:
                    note = ("（系统）主控中断后续跑，这是一个新的会话：之前的对话不在了，但工作区里的笔记、程序与 game/ 文件都在。"
                            "游戏一直冻结在中断时的那一帧，章节计时没有走。先看一遍 /workspace/notes.md，再调用 observe 继续游玩。")
            runner = ClaudeRunner(cfg, run_dir, session, metrics, token or "", container, study,
                                  session_id=session_id, resume=resumed_session is not None, resume_note=note,
                                  gate=gate, playback=playback if playback.collect else None)
            print(f"[run] 启动智能体：model={cfg.model} effort={cfg.effort} 运行方式 {cfg.runtime} "
                  f"预算 {cfg.max_minutes:g} 分钟", flush=True)
            reason = await runner.run()
    finally:
        metrics.update(status="done" if session.done else "stopped")
        web.should_exit = True
        if not web_task.done():
            try:
                await asyncio.wait_for(web_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                web_task.cancel()
        playback.close()
        if live is not None:
            live.close()
        recording = None
        if recorder:
            try:
                recording = recorder.stop()
            except Exception as exc:  # noqa: BLE001（录制收尾失败不影响交还游戏控制与写总结）
                print(f"[record] 停止录制失败：{exc}", flush=True)
        # 通关了就交还游戏控制；没通关（时间预算用完、出错、Ctrl+C）就让游戏冻结在当前帧，章节计时不走，可以 --resume 续跑
        session.close(release=session.done or args.no_agent)
        if not (session.done or args.no_agent):
            print("[run] 游戏保持冻结在当前帧（章节计时不走）：可以用 --resume 续跑；"
                  "不续跑时用 python tools\\bridge_client.py deactivate 交还控制", flush=True)
        summary = metrics.snapshot()
        summary.update(stop_reason=reason, completed=session.done and session.done_reason == "章节完成",
                       chapters=cfg.chapters, splits=metrics.state.splits or [],
                       total_time_ticks=metrics.state.completed_ticks + (0 if session.done else metrics.state.chapter_time_ticks),
                       start_sid=session.start_sid, turns=runner.turns if runner else 0,
                       session_id=runner.session_id if runner else None, run_dir=str(run_dir),
                       resumed_from=str(previous["dir"]) if previous else None,
                       kb_study=study.summary() if study else None, recording=recording)
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    cfg, args = parse_args()
    previous = args.previous
    if not port_free(cfg.mcp_port):
        raise SystemExit(f"端口 {cfg.mcp_port} 已被占用：可能有另一个主控在运行，或用 --mcp-port 换一个端口")
    token = None if args.no_agent else load_token(cfg)
    run_dir = make_run_dir(cfg.agent)
    print(f"[run] 日志目录：{run_dir}", flush=True)
    recorder = None
    try:
        if previous:
            check_resume_position(cfg)
        elif not cfg.skip_enter:
            if args.record:
                # 开局（选存档、进章节）也录进录像：录制从选档页开始。HUD 服务先起来，显示这一局的名字
                if not args.no_hud:
                    from overlay.server import start_in_thread  # noqa: WPS433
                    start_in_thread(args.hud_port)
                    write_placeholder_metrics(run_dir, cfg)
                recorder = Recorder(None, options_from(run_dir, args.ffmpeg, args.record_video_args,
                                                       args.audio_offset_ms, args.record_marker),
                                    log=lambda m: print(m, flush=True))
            enter_save(cfg, recorder)
        summary = asyncio.run(run_async(cfg, args, run_dir, token, previous, recorder))
    except KeyboardInterrupt:
        print("[run] 已中断", flush=True)
        return 130
    except RecordingError as exc:
        print(f"[record] {exc}", flush=True)
        return 2
    finally:
        if recorder is not None and recorder.started:  # 没走到主控的收尾（例如启动阶段出错）：在这里停止录制
            from tools.bridge_client import BridgeClient  # noqa: WPS433
            client = BridgeClient(port=cfg.bridge_port, timeout=120)
            try:
                recorder.call = client.call
                recorder.stop()
            finally:
                client.close()
    print(f"[run] 结束：{summary['stop_reason']}；决策 {summary['decisions']} 次，死亡 {summary['deaths']} 次，"
          f"用时 {summary['elapsed_s']:.0f} 秒（思考 {summary['thinking_s']:.0f}，工具 {summary['tool_s']:.0f}，"
          f"游戏推进 {summary['play_game_s'] + summary['auto_game_s']:.0f}）"
          + (f"；准备阶段 {summary['study_s']:.0f} 秒，知识库已读 {summary['kb_study']['progress']:.0%}"
             if summary.get("kb_study") else ""), flush=True)
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
