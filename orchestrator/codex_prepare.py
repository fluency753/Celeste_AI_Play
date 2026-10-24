"""只准备独立 GPT 的知识库与 HUD，不连接游戏、不改变当前存档或关卡。"""
from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from .codex_agent import CodexRunner
from .config import REPO_ROOT, RunConfig
from .container import ensure_container
from .kb_study import KbStudy
from .live import LiveStatus
from .metrics import Metrics
from overlay.server import start_in_thread


async def prepare(args):
    cfg = RunConfig(agent=args.agent, save="", model=args.model, provider="codex", engine="codex",
                    provider_info={"images": True, "allowed_domains": ["api.openai.com"]}, effort=args.effort,
                    knowledge_profile=args.knowledge, sim=args.knowledge == "full", sim_assist=args.knowledge == "explore", live=True,
                    max_minutes=args.max_minutes)
    directory = Path(args.output).resolve() if args.output else REPO_ROOT / "runs" / cfg.agent / datetime.now().strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    print(f"[codex] 准备日志：{directory}", flush=True)
    container = await asyncio.to_thread(ensure_container, cfg)
    game = cfg.workspace_dir / "game"
    game.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "orchestrator/workspace_files/show.py", game / "show.py")
    study = KbStudy(cfg.kb_dir, cfg.max_minutes,
                    {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None)
    if args.quick_check:
        # 联调仅检查索引文件；不能把联调结果记成全库已读。
        study.files = ["README.md"]
        study.total = {"README.md": study.total["README.md"]}
        study.read = {"README.md": set()}
    metrics = Metrics(directory / "metrics.json", cfg.agent, cfg.model, cfg.effort,
                      cfg.budget_seconds, study=True)
    metrics.update(telemetry_source="codex_app_server", live=True, tool_calls=0)
    if not args.hud_managed:
        start_in_thread(args.hud_port, pinned=directory)
    print(f"[codex] 独立 HUD：http://127.0.0.1:{args.hud_port}/hud（游戏保持原样）", flush=True)
    live = LiveStatus(game / "hud_status.json", metrics).start()
    runner = CodexRunner(cfg, directory, None, metrics, container, study, prepare_only=True)
    if args.fast is not None:
        runner.fast_mode = args.fast == "on"
    reason = "中断"
    try:
        reason = await runner.run()
    finally:
        live.close()
        metrics.update(status="done" if study.complete() else "stopped")
        hashes = {rel: hashlib.sha256((study.extra.get(rel) or (study.kb_dir / rel)).read_bytes()).hexdigest()
                  for rel in study.files}
        result = {"reason": reason, "quick_check": args.quick_check, "native_game_tools": True,
                  "knowledge_profile": cfg.knowledge_profile,
                  "kb_study": study.summary(), "kb_hashes": hashes,
                  "session_id": runner.session_id, "turns": runner.turns, "metrics": metrics.snapshot()}
        (directory / "prepare_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[codex] {reason}；实际阅读 {study.progress():.1%}，工具调用 {len(runner.called_tools)} 次", flush=True)
    return 0 if study.complete() else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", default="gpt-native")
    ap.add_argument("--knowledge", choices=["explore", "full"], default="explore")
    ap.add_argument("--model", default="", help="留空沿用 Codex 已登录配置，由服务回报实际模型")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--max-minutes", type=float, default=60)
    ap.add_argument("--hud-port", type=int, default=47792)
    ap.add_argument("--output", help="由 HUD 后台分配的准备目录")
    ap.add_argument("--hud-managed", action="store_true", help="复用既有 HUD 服务")
    ap.add_argument("--fast", choices=["on", "off"], default=None)
    ap.add_argument("--quick-check", action="store_true", help="仅测试知识库索引的读取与遥测，不代表全库读完")
    args = ap.parse_args()
    return asyncio.run(prepare(args))


if __name__ == "__main__":
    raise SystemExit(main())
