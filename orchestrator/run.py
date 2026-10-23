"""主控入口：进入存档 → 接管游戏 → 启动游戏工具服务 → 启动智能体 → 收尾。

用法（在仓库根目录）：
    python -m orchestrator.run --agent dev-opus --save test --model opus
    python -m orchestrator.run --agent dev-opus --save test --model opus --max-minutes 20 --effort medium
    python -m orchestrator.run --agent dev --save test --no-agent      # 只启动工具服务，用于手动调试
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import uvicorn

from .agent import ClaudeRunner
from .config import REPO_ROOT, RunConfig, load_dotenv
from .game_session import GameSession
from .metrics import Metrics
from .server import build_server


def parse_args() -> tuple[RunConfig, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="智能体标签，用于日志目录与独立配置目录，如 opus5.5、dev-opus")
    ap.add_argument("--save", required=True, help="游戏内存档名，如 test、opus5.5、fable5.1")
    ap.add_argument("--model", default="opus", help="Claude Code 模型别名：opus / fable 等（默认 opus）")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-minutes", type=float, default=120.0, help="现实时间预算（分钟）")
    ap.add_argument("--bridge-port", type=int, default=47774)
    ap.add_argument("--mcp-port", type=int, default=47780)
    ap.add_argument("--play-speed", type=int, default=1, help="智能体动作段的推进速度（每次渲染推进几帧）")
    ap.add_argument("--auto-speed", type=int, default=4, help="转场、剧情、重生段的推进速度")
    ap.add_argument("--skip-enter", action="store_true", help="已在关卡内且可操作时，跳过菜单宏")
    ap.add_argument("--no-agent", action="store_true", help="不启动智能体，只运行工具服务直到 Ctrl+C")
    args = ap.parse_args()
    cfg = RunConfig(agent=args.agent, save=args.save, model=args.model, effort=args.effort,
                    bridge_port=args.bridge_port, mcp_port=args.mcp_port, max_minutes=args.max_minutes,
                    play_speed=args.play_speed, auto_speed=args.auto_speed, skip_enter=args.skip_enter)
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
        obs = macro(client, cfg.save)
        print(f"[run] 已进入存档 {cfg.save}：{obs['level']['sid']} 房间 {obs['level']['room']}"
              f"（菜单宏用时 {time.monotonic() - t0:.1f} 秒）", flush=True)
    finally:
        client.close()


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def run_async(cfg: RunConfig, args: argparse.Namespace, run_dir: Path, token: str | None) -> dict:
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    session = GameSession(cfg, run_dir, metrics)
    obs = session.connect()
    print(f"[run] 已接管游戏：{obs['level']['sid']} 房间 {obs['level']['room']}，存档 {cfg.save}", flush=True)

    server = build_server(session)
    app = server.streamable_http_app(streamable_http_path="/mcp", session_idle_timeout=None, host="127.0.0.1")
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
            runner = ClaudeRunner(cfg, run_dir, session, metrics, token or "", server.reset_clock)
            print(f"[run] 启动智能体：model={cfg.model} effort={cfg.effort} 预算 {cfg.max_minutes:g} 分钟", flush=True)
            reason = await runner.run()
    finally:
        metrics.update(status="done" if session.done else "stopped")
        web.should_exit = True
        if not web_task.done():
            try:
                await asyncio.wait_for(web_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                web_task.cancel()
        session.close()  # 交还游戏控制
        summary = metrics.snapshot()
        summary.update(stop_reason=reason, completed=session.done and session.done_reason == "章节完成",
                       start_sid=session.start_sid, turns=runner.turns if runner else 0,
                       session_id=runner.session_id if runner else None, run_dir=str(run_dir))
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
    print(f"[run] 结束：{summary['stop_reason']}；决策 {summary['decisions']} 次，死亡 {summary['deaths']} 次，"
          f"用时 {summary['elapsed_s']:.0f} 秒（思考 {summary['thinking_s']:.0f}，工具 {summary['tool_s']:.0f}，"
          f"游戏推进 {summary['play_game_s'] + summary['auto_game_s']:.0f}）", flush=True)
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
