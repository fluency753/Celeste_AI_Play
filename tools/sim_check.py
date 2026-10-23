"""预置模拟器的接入自检（不启动大模型）：在 test 存档上，用和正式运行相同的主控代码推进游戏，
每一段之前让容器里的模拟器（/sim）从工作区的 sim_state.json 预测同一段按键，再与游戏实际的逐帧轨迹对照。

    python tools\\sim_check.py                 # 当前房间，随机 5 段
    python tools\\sim_check.py --segments 10 --agent dev-sim

需要：游戏在 test 存档的关卡里、桥接 0.10 以上、智能体镜像已构建、本机 sim/dist 里有模拟器。
用的是独立的智能体标签（默认 dev-sim）与它自己的工作区，不碰正式存档与正式智能体的工作区。
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import REPO_ROOT, RunConfig  # noqa: E402
from orchestrator.container import docker, ensure_container  # noqa: E402
from orchestrator.game_session import GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402


def random_plan(rng: random.Random, frames: int) -> list[str]:
    out, total = [], 0
    while total < frames:
        n = rng.randint(3, 20)
        keys = rng.choice(["", "R", "L", "R,J", "L,J", "R,X", "L,U,X", "U,X", "R,G", "D", "R,Z"])
        out.append(f"{n},{keys}" if keys else str(n))
        total += n
    return out


def sim_predict(container: str, inputs: list[str]) -> tuple[list[list], str]:
    result = docker("exec", "-u", "agent", container, "/sim/celeste-sim", "run", "--state",
                    "/workspace/game/sim_state.json", "--inputs", *inputs, check=False, timeout=60)
    rows = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) == 8 and parts[1]:
            rows.append([int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]),
                         parts[5], int(parts[6]), int(parts[7])])
    stop = next((l[6:].strip() for l in result.stderr.splitlines() if l.startswith("stop: ")), "?")
    if result.returncode != 0:
        raise RuntimeError(f"模拟器出错：{result.stderr.strip()[-400:]}")
    return rows, stop


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default="dev-sim", help="自检用的智能体标签（独立的工作区与容器）")
    ap.add_argument("--segments", type=int, default=5)
    ap.add_argument("--frames", type=int, default=60, help="每段大约多少帧")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    cfg = RunConfig(agent=args.agent, save="test", model="opus")
    cfg.sim = True
    if not (cfg.sim_dir / "celeste-sim").is_file():
        print(f"找不到 {cfg.sim_dir / 'celeste-sim'}", file=sys.stderr)
        return 2
    run_dir = REPO_ROOT / "runs" / args.agent / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    container = ensure_container(cfg)
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    session = GameSession(cfg, run_dir, metrics)
    session.container = container
    obs = session.connect()
    print(f"已接管：{obs['level']['sid']} 房间 {obs['level']['room']}；容器 {container}")
    rng = random.Random(args.seed)
    ok = 0
    try:
        for k in range(args.segments):
            inputs = random_plan(rng, args.frames)
            t0 = time.monotonic()
            predicted, sim_stop = sim_predict(container, inputs)
            sim_s = time.monotonic() - t0
            outcome = session.act(inputs, "模拟器自检", ["death", "room"])
            actual = [r for r in (outcome.result.get("trajectory") or []) if len(r) >= 8 and r[1] is not None]
            n = min(len(actual), len(predicted))
            first_bad = next((i for i in range(n) if [round(v, 4) if isinstance(v, float) else v for v in actual[i][:8]]
                              != [round(v, 4) if isinstance(v, float) else v for v in predicted[i][:8]]), None)
            same = first_bad is None and n > 0
            ok += same
            print(f"第 {k + 1} 段：游戏 {len(actual)} 帧（{outcome.result['stop']}），模拟器 {len(predicted)} 帧（{sim_stop}，"
                  f"{sim_s:.2f} 秒）——{'逐帧一致' if same else '不一致'}")
            if not same and first_bad is not None:
                print(f"   第 {first_bad + 1} 帧：游戏 {actual[first_bad][:8]}，模拟器 {predicted[first_bad][:8]}")
            if session.done:
                break
    finally:
        session.close()
    print(f"一致 {ok}/{args.segments}")
    return 0 if ok == args.segments else 1


if __name__ == "__main__":
    sys.exit(main())
