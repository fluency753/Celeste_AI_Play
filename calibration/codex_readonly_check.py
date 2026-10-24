"""恢复已准备的原生 GPT，只读查看真实游戏；不登记任何能推进游戏的回调。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.server.mcpserver import Image, MCPServer
from orchestrator.codex_agent import CodexRunner
from orchestrator.prepared import load_preparation
from orchestrator.config import REPO_ROOT, RunConfig
from orchestrator.container import ensure_container
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from tools.bridge_client import BridgeClient


async def check(args):
    manifest = json.loads((Path(args.prepared) / "prepare_summary.json").read_text(encoding="utf-8"))
    knowledge = manifest.get("knowledge_profile", "full")
    cfg = RunConfig(agent="gpt-native", save="test", model="", provider="codex", engine="codex",
                    provider_info={"images": True, "allowed_domains": ["api.openai.com"]},
                    knowledge_profile=knowledge, sim=knowledge == "full",
                    effort="medium", max_minutes=5, live=True)
    study = KbStudy(cfg.kb_dir, 60, {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None)
    prepared = load_preparation(args.prepared, cfg, study)
    directory = REPO_ROOT / "runs/gpt-native" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-readonly")
    directory.mkdir(parents=True)
    metrics = Metrics(directory / "metrics.json", cfg.agent, "", "medium", cfg.budget_seconds, study=True)
    metrics.kb_finished()
    metrics.update(kb_progress=1.0, tool_calls=prepared["metrics"].get("tool_calls", 0))
    container = await asyncio.to_thread(ensure_container, cfg)
    session = SimpleNamespace(done=False, done_reason="真实游戏只读联调完成")
    server = MCPServer(name="celeste-readonly-check")
    observation = {}
    client = BridgeClient(timeout=30)

    @server.tool()
    async def observe():
        reply = await asyncio.to_thread(client.call, "observe", image=True, entities=False, tiles=False)
        observation.update(reply["obs"])
        session.done = True
        result = [json.dumps(reply["obs"], ensure_ascii=False)]
        if reply.get("image"):
            (directory / "game.png").write_bytes(base64.b64decode(reply["image"]))
            result.append(Image(data=base64.b64decode(reply["image"]), format="png"))
        return result

    @server.tool()
    async def narrate(thought: str):
        metrics.set_thought(thought[:120])
        metrics.add(narrates=1)
        return "公开解说已更新"

    runner = CodexRunner(cfg, directory, session, metrics, container, study, game_server=server,
                         session_id=prepared["session_id"], resume=True,
                         resume_note="（系统）本次是只读联调，不是试玩。游戏应停在第一章结算画面。"
                         "先发一句公开解说，再调用 celeste_observe 一次，查看真实返回的截图与场景，简短回复后结束。"
                         "禁止调用 act、Bash 或其他会改变游戏的操作，不要进入第二章。")
    try:
        reason = await runner.run()
    finally:
        client.close()
    assert session.done and observation.get("scene") == "AreaComplete", observation.get("scene")
    metrics.update(status="done")
    result = {"reason": reason, "scene": observation.get("scene"), "frame": observation.get("frame"),
              "game_actions": 0, "tools": runner.called_tools, "session_id": runner.session_id,
              "model": metrics.state.model, "effort": metrics.state.effort}
    (directory / "check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS:", json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    asyncio.run(check(parser.parse_args()))
