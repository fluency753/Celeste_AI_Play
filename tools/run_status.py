"""查看一次运行的实时状态：统计数据与最近的决策。

用法（仓库根目录）：
    python tools/run_status.py            # 最近一次运行
    python tools/run_status.py -n 30      # 显示最近 30 条决策
    python tools/run_status.py runs/opus5.5/20261022-200717
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="运行目录；默认读取 runs/latest.txt")
    ap.add_argument("-n", type=int, default=12, help="显示最近几条决策")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    run_dir = Path(args.run_dir) if args.run_dir else Path((REPO_ROOT / "runs" / "latest.txt").read_text(encoding="utf-8").strip())
    m = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    print(f"{run_dir}")
    print(f"状态 {m['status']}  用时 {m['elapsed_s']:.0f}s / {m['budget_s']:.0f}s  思考 {m['thinking_s']:.0f}s  工具 {m['tool_s']:.1f}s  "
          f"游戏推进 {m['play_game_s'] + m['auto_game_s']:.1f}s")
    print(f"决策 {m['decisions']}  帧 {m['frames_played']}+{m['frames_auto']}(自动)  死亡 {m['deaths']}  "
          f"上下文 {m['context_tokens']:,}  压缩 {m['compactions']}  章节 {m['chapter']}  房间 {m['room']}")
    print(f"当前想法：{m['thought']}")

    path = run_dir / "decisions.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if rows:
        print(f"\n最近 {min(args.n, len(rows))} 条决策（共 {len(rows)} 条）：")
        prev_t = None
        for line in rows[-args.n:]:
            d = json.loads(line)
            gap = "" if prev_t is None else f" +{d['t'] - prev_t:.0f}s"
            prev_t = d["t"]
            print(f"#{d['i']:>3} t={d['t']:.0f}s{gap} {d['stop']:<8} {d['consumed']}/{d['planned']}帧 "
                  f"{' '.join(d['inputs'])[:70]}  | {d['thought']}")
    summary = run_dir / "summary.json"
    if summary.exists():
        s = json.loads(summary.read_text(encoding="utf-8"))
        print(f"\n已结束：{s['stop_reason']}（通关 {s['completed']}，共 {s['turns']} 轮）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
