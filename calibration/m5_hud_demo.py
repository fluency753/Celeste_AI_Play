"""M5 验收：不调用模型，模拟"思考 → 执行"的节奏驱动游戏，检查 HUD 的实时数据。

前提：游戏开着（桥接 0.7.0），test 存档停在关卡里 Madeline 可操作、站在平地上；
另开一个窗口运行 `python -m overlay.window` 观察置顶窗口（或在浏览器打开 http://127.0.0.1:47790/hud）。

用法（仓库根目录）：
    python calibration/m5_hud_demo.py            # 默认 3 轮，每轮"思考" 3 秒
检查项：执行中桥接按设定间隔推送进度；章节计时、冲刺次数随之更新；思考与执行状态正确切换。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import RunConfig  # noqa: E402
from orchestrator.game_session import GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402
from overlay.server import start_in_thread  # noqa: E402

FAILED = 0


def check(ok: bool, name: str, detail: str = "") -> None:
    global FAILED
    if not ok:
        FAILED += 1
    print(f"  [{'通过' if ok else '不通过'}] {name}{('：' + detail) if detail else ''}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", default="test")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--think", type=float, default=3.0, help="每轮模拟思考的秒数")
    ap.add_argument("--port", type=int, default=47790)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = RunConfig(agent="hud-demo", save=args.save, model="opus")
    run_dir = REPO_ROOT / "runs" / "hud-demo" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "runs" / "latest.txt").write_text(str(run_dir), encoding="utf-8")
    if start_in_thread(args.port) is None:
        print(f"端口 {args.port} 上已有 HUD 服务（它跟随 runs/latest.txt，会显示本次演示）", flush=True)
    metrics = Metrics(run_dir / "metrics.json", "hud-demo", "opus", "high", 600)
    session = GameSession(cfg, run_dir, metrics)
    session.workspace = None          # 演示不写智能体工作区
    session.connect()

    progress: list[dict] = []
    original = session._on_progress

    def spy(event: dict) -> None:
        progress.append(event)
        original(event)
    session._on_progress = spy

    plans = [
        (["1,J", "20,J", "40"], "原地跳一下，看主计时逐帧走动。"),
        (["1,U,X", "3,U", "10,U", "45"], "向上冲刺：冲刺用尽时左侧色条变成冰蓝，落地后恢复绯红。"),
        (["8,R", "8,L", "30"], "左右走两步，回到原地。"),
    ]
    try:
        for i in range(args.rounds):
            inputs, thought = plans[i % len(plans)]
            metrics.mark_thinking()
            metrics.update(status="thinking")
            t0 = time.monotonic()
            time.sleep(args.think)
            metrics.add(thinking_s=time.monotonic() - t0)
            before = len(progress)
            ticks_before = metrics.state.chapter_time_ticks
            outcome = session.act(inputs, thought, None)
            events = progress[before:]
            frames = outcome.result["consumed"]
            check(len(events) >= frames // 6, f"第 {i + 1} 轮：执行中推送了进度",
                  f"{frames} 帧，收到 {len(events)} 次进度")
            dashes = [e["dashes"] for e in events]
            check(metrics.state.chapter_time_ticks > ticks_before, f"第 {i + 1} 轮：章节计时前进",
                  f"{ticks_before / 1e7:.3f} → {metrics.state.chapter_time_ticks / 1e7:.3f} 秒；进度里的冲刺次数 {sorted(set(dashes))}")
            metrics.mark_thinking()
            metrics.update(status="thinking")
        data = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        check(data["chapter_time_wall"] > 0 and data["thinking_since_wall"] > 0, "metrics.json 带有 HUD 走表用的时间点")
    finally:
        metrics.update(status="done")
        session.close()
    print(f"\n结果：{'全部通过' if FAILED == 0 else f'{FAILED} 项不通过'}（日志 {run_dir}）", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
