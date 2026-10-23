"""按某次运行记录下的决策重放（不调用模型），用于复现问题、检查主控与桥接的改动。

用法（仓库根目录）：
    python -m orchestrator.replay runs/dev-opus/20261022-200717 --save test --chapter 0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import RunConfig
from .game_session import ActError, GameSession
from .metrics import Metrics
from .run import enter_save, make_run_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="要重放的运行目录（含 decisions.jsonl）")
    ap.add_argument("--save", required=True)
    ap.add_argument("--chapter", type=int)
    ap.add_argument("--skip-enter", action="store_true")
    ap.add_argument("--bridge-port", type=int, default=47774)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    decisions = [json.loads(line) for line in
                 (Path(args.source) / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    cfg = RunConfig(agent="replay", save=args.save, model="replay", bridge_port=args.bridge_port, chapter=args.chapter)
    run_dir = make_run_dir("replay")
    print(f"[replay] {len(decisions)} 条决策，日志目录 {run_dir}", flush=True)
    if not args.skip_enter:
        enter_save(cfg)

    metrics = Metrics(run_dir / "metrics.json", "replay", "replay", "-", cfg.budget_seconds)
    session = GameSession(cfg, run_dir, metrics)
    t0 = time.monotonic()
    try:
        obs = session.connect()
        print(f"[replay] 起点：{obs['level']['sid']} 房间 {obs['level']['room']}", flush=True)
        for d in decisions:
            if session.done:
                break
            try:
                out = session.act(d["inputs"], d["thought"], d.get("stop_on"))
            except ActError as err:
                print(f"#{d['i']} 未执行：{err}")
                continue
            same = "一致" if out.result["stop"] == d["stop"] and out.result["consumed"] == d["consumed"] else "不同"
            room = (out.obs.get("level") or {}).get("room")
            print(f"#{d['i']:>3} {same}  停止 {out.result['stop']}（原 {d['stop']}） 帧 {out.result['consumed']}（原 {d['consumed']}）"
                  f"  房间 {room}  {'；'.join(out.auto_notes)}", flush=True)
    finally:
        session.close()
    s = metrics.snapshot()
    print(f"[replay] 结束：{'章节完成' if session.done else '未完成'}；用时 {time.monotonic() - t0:.0f} 秒，"
          f"推进 {s['frames_played']} + {s['frames_auto']}（自动）帧，死亡 {s['deaths']}", flush=True)
    return 0 if session.done else 1


if __name__ == "__main__":
    sys.exit(main())
