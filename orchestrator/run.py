"""主控入口：进入存档 → 接管游戏 → 启动游戏工具服务 → 启动智能体 → 收尾。

用法（在仓库根目录）：
    python -m orchestrator.run --agent dev-opus --save test --model opus
    python -m orchestrator.run --agent dev-opus --save test --model opus --max-minutes 20 --effort medium
    python -m orchestrator.run --agent dev --save test --no-agent      # 只启动工具服务，用于手动调试
    python -m orchestrator.run --agent opus5.5 --save opus5.5 --record # 同时用 OBS 录制（先 python tools/obs_setup.py launch）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import uvicorn

from .agent import ClaudeRunner
from .config import REPO_ROOT, RunConfig, load_dotenv
from .container import ensure_container
from .game_session import GameSession
from .kb_study import KbStudy
from .metrics import Metrics
from .recording import Recorder, RecordingError, options_from
from .server import TRANSPORT_SECURITY, build_server


def parse_args() -> tuple[RunConfig, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="智能体标签，用于日志目录与独立配置目录，如 opus5.5、dev-opus")
    ap.add_argument("--save", required=True, help="游戏内存档名，如 test、opus5.5、fable5.1")
    ap.add_argument("--model", default="opus", help="Claude Code 模型别名：opus / fable 等（默认 opus）")
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
                    help="准备阶段（读知识库、写笔记）的上限分钟数，不计入现实用时；到时自动开始计时")
    ap.add_argument("--max-usage", type=float, default=0.9,
                    help="Max 套餐 5 小时或 7 天窗口的已用比例达到该值（0~1）就停止本次运行，默认 0.9")
    ap.add_argument("--hud-port", type=int, default=47790, help="HUD 页面与数据的端口（OBS 浏览器源与置顶窗口都连它）")
    ap.add_argument("--no-hud", action="store_true", help="不在主控进程里启动 HUD 服务")
    ap.add_argument("--record", action="store_true",
                    help="用 OBS 锁步录制：只录游戏真正推进的帧，成片在日志目录的 recording/ 下（需要先启动 OBS）")
    ap.add_argument("--record-marker", action="store_true", help="录制时在画面左下角画帧号条码（只用于校准）")
    ap.add_argument("--ffmpeg", help="ffmpeg 路径（默认：configs/local.recording.json → PATH → winget 安装位置）")
    ap.add_argument("--record-video-args", help="ffmpeg 视频编码参数（默认见 configs/recording.json）")
    ap.add_argument("--audio-offset-ms", type=float, help="声音相对画面的偏移毫秒数（默认用校准结果，见 configs/local.recording.json）")
    ap.add_argument("--sim", action="store_true",
                    help="开放预置模拟器：本机 sim/dist 只读挂到容器的 /sim，每次停下把当前帧的完整场景写到 /workspace/game/sim_state.json")
    ap.add_argument("--runtime", choices=["container", "host"], default="container",
                    help="container：智能体在 Docker 容器里运行（默认）；host：在本机运行且只开放游戏工具（M2 的过渡方式）")
    args = ap.parse_args()
    cfg = RunConfig(agent=args.agent, save=args.save, model=args.model, effort=args.effort,
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
    cfg.sim = args.sim
    if cfg.sim:
        if cfg.runtime != "container":
            raise SystemExit("--sim 只能用于容器运行方式")
        if not (cfg.sim_dir / "celeste-sim").is_file():
            raise SystemExit(f"找不到预置模拟器：{cfg.sim_dir / 'celeste-sim'}（模拟器只在本机，见使用说明的预置模拟器一节）")
    return cfg, args


def load_token() -> str:
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


def enter_save(cfg: RunConfig) -> None:
    from tools.enter_save import enter_save as macro  # noqa: WPS433（延迟导入，避免 --help 时连接游戏）
    from tools.bridge_client import BridgeClient

    client = BridgeClient(port=cfg.bridge_port, timeout=120)
    try:
        t0 = time.monotonic()
        obs = macro(client, cfg.save, chapter=cfg.chapter)
        print(f"[run] 已进入存档 {cfg.save}：{obs['level']['sid']} 房间 {obs['level']['room']}"
              f"（菜单宏用时 {time.monotonic() - t0:.1f} 秒）", flush=True)
    finally:
        client.close()


def backup_save(obs: dict, run_dir: Path) -> None:
    """开跑前把当前存档文件（与 Everest 的附加存档）复制到本次运行的目录，出问题时可以手动还原。"""
    path = (obs.get("save") or {}).get("file")
    if not path:
        print("[run] 提示：桥接没有报告存档文件的位置（桥接 0.10.2 以上才有），跳过存档备份", flush=True)
        return
    src = Path(path)
    dest = run_dir / "save_backup"
    copied = []
    for candidate in (src, src.with_name(src.stem + "-modsavedata" + src.suffix)):
        if candidate.is_file():
            dest.mkdir(exist_ok=True)
            shutil.copy2(candidate, dest / candidate.name)
            copied.append(candidate.name)
    print(f"[run] 已备份存档：{', '.join(copied) or '（存档文件还不存在）'} → {dest}", flush=True)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def run_async(cfg: RunConfig, args: argparse.Namespace, run_dir: Path, token: str | None) -> dict:
    study = None
    if cfg.study and cfg.runtime == "container" and not args.no_agent:
        extra = {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None
        study = KbStudy(REPO_ROOT / "kb", cfg.study_minutes, extra)
        if not study.has_reference:
            print("[run] 提示：kb/reference/ 不存在，准备阶段只读知识库本身（见使用说明 2.5 节生成参考资料原文）", flush=True)
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds,
                      study=study is not None)
    if not args.no_hud:
        from overlay.server import start_in_thread  # noqa: WPS433
        hud = start_in_thread(args.hud_port)
        print(f"[run] HUD：http://127.0.0.1:{args.hud_port}/hud"
              + ("" if hud else "（端口已被占用，沿用已在运行的 HUD 服务）"), flush=True)
    session = GameSession(cfg, run_dir, metrics)
    obs = session.connect()
    print(f"[run] 已接管游戏：{obs['level']['sid']} 房间 {obs['level']['room']}，存档 {cfg.save}", flush=True)
    backup_save(obs, run_dir)
    recorder = None
    if args.record:
        recorder = Recorder(session.bridge_call,
                            options_from(run_dir, args.ffmpeg, args.record_video_args, args.audio_offset_ms, args.record_marker),
                            log=lambda m: print(m, flush=True))
        try:
            recorder.start()
        except RecordingError:
            session.close()
            raise

    server = build_server(session, study)
    app = server.streamable_http_app(streamable_http_path="/mcp", session_idle_timeout=None, host="127.0.0.1",
                                     transport_security=TRANSPORT_SECURITY)
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
            runner = ClaudeRunner(cfg, run_dir, session, metrics, token or "", server.reset_clock, container, study)
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
        recording = None
        if recorder:
            try:
                recording = recorder.stop()
            except Exception as exc:  # noqa: BLE001（录制收尾失败不影响交还游戏控制与写总结）
                print(f"[record] 停止录制失败：{exc}", flush=True)
        session.close()  # 交还游戏控制
        summary = metrics.snapshot()
        summary.update(stop_reason=reason, completed=session.done and session.done_reason == "章节完成",
                       chapters=cfg.chapters, splits=metrics.state.splits or [],
                       total_time_ticks=metrics.state.completed_ticks + (0 if session.done else metrics.state.chapter_time_ticks),
                       start_sid=session.start_sid, turns=runner.turns if runner else 0,
                       session_id=runner.session_id if runner else None, run_dir=str(run_dir),
                       kb_study=study.summary() if study else None, recording=recording)
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    cfg, args = parse_args()
    if not port_free(cfg.mcp_port):
        raise SystemExit(f"端口 {cfg.mcp_port} 已被占用：可能有另一个主控在运行，或用 --mcp-port 换一个端口")
    token = None if args.no_agent else load_token()
    run_dir = make_run_dir(cfg.agent)
    print(f"[run] 日志目录：{run_dir}", flush=True)
    if not cfg.skip_enter:
        enter_save(cfg)
    try:
        summary = asyncio.run(run_async(cfg, args, run_dir, token))
    except KeyboardInterrupt:
        print("[run] 已中断", flush=True)
        return 130
    except RecordingError as exc:
        print(f"[record] {exc}", flush=True)
        return 2
    print(f"[run] 结束：{summary['stop_reason']}；决策 {summary['decisions']} 次，死亡 {summary['deaths']} 次，"
          f"用时 {summary['elapsed_s']:.0f} 秒（思考 {summary['thinking_s']:.0f}，工具 {summary['tool_s']:.0f}，"
          f"游戏推进 {summary['play_game_s'] + summary['auto_game_s']:.0f}）"
          + (f"；准备阶段 {summary['study_s']:.0f} 秒，知识库已读 {summary['kb_study']['progress']:.0%}"
             if summary.get("kb_study") else ""), flush=True)
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
