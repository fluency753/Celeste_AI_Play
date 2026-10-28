"""只准备独立 agent 的知识库与 HUD，不连接游戏、不改变当前存档或关卡。"""
from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
from datetime import datetime
from pathlib import Path

from .lifecycle import host_runner
from .runtimes import RUNTIMES, runtime
from .config import REPO_ROOT, RunConfig
from .container import ensure_container
from .kb_study import KbStudy
from .workspace_setup import install_game_helpers
from .live import LiveStatus
from .metrics import Metrics
from overlay.server import start_in_thread


async def prepare(args):
    adapter = runtime(args.engine)
    cfg = RunConfig(agent=args.agent or args.engine+'-agent', save="", model=args.model, effort=args.effort,
                    knowledge_profile=args.knowledge, sim=args.knowledge == "full", sim_assist=False,
                    live=True, max_minutes=args.max_minutes)
    adapter.configure(cfg, args.provider)
    cfg.model, cfg.effort = await asyncio.to_thread(adapter.resolve, cfg.model, cfg.effort, cfg.provider)
    adapter.token(cfg)  # Fail before creating containers or changing the selected session.
    cfg.mcp_port = getattr(args, 'mcp_port', cfg.mcp_port)
    directory = Path(args.output).resolve() if args.output else REPO_ROOT / "runs" / cfg.agent / datetime.now().strftime("%Y%m%d-%H%M%S")
    if (directory/'prepare_summary.json').exists():
        raise ValueError('准备目录已有结果，请选择新的输出目录。')
    directory.mkdir(parents=True, exist_ok=True)
    print(f"[{cfg.engine}] 准备日志：{directory}", flush=True)
    container = await asyncio.to_thread(ensure_container, cfg)
    game = cfg.workspace_dir / "game"
    game.mkdir(parents=True, exist_ok=True)
    install_game_helpers(cfg.workspace_dir)
    study = KbStudy(cfg.kb_dir, cfg.max_minutes,
                    {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None)
    if args.quick_check:
        # 联调仅检查索引文件；不能把联调结果记成全库已读。
        study.files = ["README.md"]
        study.total = {"README.md": study.total["README.md"]}
        study.read = {"README.md": set()}
    metrics = Metrics(directory / "metrics.json", cfg.agent, cfg.model, cfg.effort,
                      cfg.budget_seconds, study=True)
    metrics.update(telemetry_source=adapter.telemetry_source, live=True, tool_calls=0)
    if not args.hud_managed:
        start_in_thread(args.hud_port, pinned=directory)
    print(f"[{cfg.engine}] 独立 HUD：http://127.0.0.1:{args.hud_port}/hud（游戏保持原样）", flush=True)
    live = LiveStatus(game / "hud_status.json", metrics).start()
    reason = "中断"
    try:
        async with host_runner(cfg, directory, metrics, container, study, prepare_only=True) as runner:
            if args.fast is not None and adapter.fast:
                runner.fast_mode = args.fast == 'on'
            try:
                reason = await runner.run()
            finally:
                metrics.update(status="done" if runner.completed_preparation else "stopped")
                hashes = {rel: hashlib.sha256((study.extra.get(rel) or (study.kb_dir / rel)).read_bytes()).hexdigest()
                          for rel in study.files}
                result = {"reason": reason, "quick_check": args.quick_check, "game_tools_registered": True, "success":runner.completed_preparation,
                          "engine":cfg.engine, "provider":cfg.provider, "runtime":cfg.runtime, "requested_model":cfg.model,
                          "knowledge_profile": cfg.knowledge_profile,
                          "kb_study": study.summary(), "kb_hashes": hashes,
                          "session_id": runner.session_id, "turns": runner.turns, "metrics": metrics.snapshot()}
                (directory / "prepare_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[{cfg.engine}] {reason}；实际阅读 {study.progress():.1%}", flush=True)
    finally:
        live.close()
    if runner.completed_preparation and not args.quick_check:
        from .session_store import write_json
        from .control import RunControl
        write_json(directory/'run_state.json', {
            'engine':cfg.engine, 'provider':cfg.provider, 'runtime':cfg.runtime, 'mcp_port':cfg.mcp_port,
            'agent':cfg.agent, 'save':'', 'save_slot':None, 'model':metrics.state.model,
            'requested_model':cfg.model, 'effort':metrics.state.effort, 'knowledge_profile':cfg.knowledge_profile,
            'session_id':runner.session_id, 'session_kind':'ready', 'prepared_dir':str(directory),
            'sim':cfg.sim, 'sim_assist':False, 'cutscene_mode':cfg.cutscene_mode, 'max_minutes':240})
        if adapter.fast and args.fast is not None:
            RunControl(directory).set_fast(args.fast == 'on')
    return 0 if runner.completed_preparation else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", choices=list(RUNTIMES), default="codex")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--agent", default=None)
    ap.add_argument("--knowledge", choices=["explore", "full"], default="explore")
    ap.add_argument("--model", default="", help="留空沿用 Codex 已登录配置，由服务回报实际模型")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--max-minutes", type=float, default=60)
    ap.add_argument("--mcp-port", type=int, default=47780)
    ap.add_argument("--hud-port", type=int, default=47792)
    ap.add_argument("--output", help="由 HUD 后台分配的准备目录")
    ap.add_argument("--hud-managed", action="store_true", help="复用既有 HUD 服务")
    ap.add_argument("--fast", choices=["on", "off"], default=None)
    ap.add_argument("--quick-check", action="store_true", help="仅测试知识库索引的读取与遥测，不代表全库读完")
    args = ap.parse_args()
    return asyncio.run(prepare(args))


if __name__ == "__main__":
    raise SystemExit(main())
