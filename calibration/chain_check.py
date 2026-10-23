"""章节衔接的自检（不启动大模型，只用 test 存档）：用主控的同一套代码，把 Madeline 直接放到 1A 最后一个房间的
终点触发器前，走进去通关，确认主控自动走完结算画面并进入 2A、停在可操作的第一帧。

    python calibration\\chain_check.py

注意：会让 test 存档记下 1A 已通关（解锁 2A）；不会碰正式存档。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import REPO_ROOT, RunConfig  # noqa: E402
from orchestrator.game_session import GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402
from orchestrator.observation import fmt_ticks  # noqa: E402


def main() -> int:
    cfg = RunConfig(agent="dev-chain", save="test", model="opus")
    cfg.chapters = [1, 2]
    run_dir = REPO_ROOT / "runs" / cfg.agent / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    session = GameSession(cfg, run_dir, metrics)
    session.connect()
    try:
        # 1A 最后一个房间 end：终点触发器在房间里 (240, 120)，宽 64 高 32
        with session.lock:
            session.client.call("load_room", room="end", chapter=1)
            session.client.call("teleport", x=4050, y=-3056)
            session.client.call("step", inputs=["20"], image=False, entities=False, tiles=False)
        outcome = session.act(["90,R"], "衔接自检：走进终点", ["death", "room"])
        level = outcome.obs.get("level") or {}
        print("停止原因：", outcome.result["stop"])
        print("自动推进说明：", " / ".join(outcome.auto_notes))
        print("现在：", level.get("sid"), "房间", level.get("room"), "可操作" if outcome.obs.get("controllable") else "不可操作")
        print("分段成绩：", [(s["chapter"], fmt_ticks(s["ticks"]), s["deaths"]) for s in metrics.state.splits or []])
        ok = level.get("sid") == "Celeste/2-OldSite" and outcome.obs.get("controllable") and not session.done
        print("衔接正常" if ok else "衔接失败")
        return 0 if ok else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
