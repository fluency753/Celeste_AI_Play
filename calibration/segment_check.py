"""按检查点分段的自检（不启动大模型，只用 test 存档）：用主控的同一套代码检查
1. 菜单宏从任意画面进入 test 存档、直接进入某章的某个检查点（桥接 enter_checkpoint，没到过的也能进）；
2. 一截在进入终点检查点的房间时完成（这里用 load_room 把 Madeline 放进终点房间，再推进几帧），记下 from/to 分段成绩；
3. 不相邻的下一截：主控直接把 Madeline 送到它的起点检查点；
4. 最后一截在检查点结束：本次运行完成，用暂停菜单返回地图；
5. 在选章地图上也能直接进入检查点。

    python calibration\\segment_check.py                        # 默认 1A 第 1 段，再 2A 第 1 段
    python calibration\\segment_check.py --segments 1.1,2.1

只用 test 存档（load_room 只在 test 存档上可用）；会让 test 存档记下到过的检查点，不会碰正式存档。
需要桥接 1.0.6 与 configs/checkpoints.json（tools/dump_checkpoints.py 导出）。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from orchestrator.config import CHAPTER_SIDS, REPO_ROOT, RunConfig  # noqa: E402
from orchestrator.game_session import GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402
from orchestrator.observation import fmt_ticks  # noqa: E402
from orchestrator.segments import checkpoint_room, fetch_table, load_table, parse_segments, piece_label, pieces_of  # noqa: E402
from tools.bridge_client import BridgeClient  # noqa: E402
from tools.enter_save import enter_save  # noqa: E402

QUIET = dict(image=False, entities=False, tiles=False)


def walk_into(session: GameSession, chapter: int, room: str) -> None:
    """（test 存档）把 Madeline 直接放进终点检查点的房间，等到可操作（代替一路玩过去）。"""
    with session.lock:
        c = session.client
        c.call("load_room", room=room, chapter=chapter)
        for _ in range(60):
            obs = c.call("step", inputs=["10"], **QUIET)["obs"]
            if obs.get("controllable") and (obs.get("level") or {}).get("room") == room:
                return
    raise RuntimeError(f"没能载入房间 {room}")


def check(cond: bool, text: str) -> bool:
    print(("  通过：" if cond else "  失败：") + text)
    return cond


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segments", default="1.1,2.1", help="要检查的段（两段、不相邻，每段都在检查点结束）")
    ap.add_argument("--port", type=int, default=47774)
    args = ap.parse_args()
    table = load_table() or fetch_table(args.port)
    if not table:
        print("没有检查点表：先运行 tools\\dump_checkpoints.py（需要桥接 1.0.6）", file=sys.stderr)
        return 2
    pieces = pieces_of(parse_segments(args.segments, table), table)
    if len(pieces) != 2 or any(p.end is None for p in pieces):
        print("--segments 要给两截，每截都在检查点结束（不能是一章的最后一段）", file=sys.stderr)
        return 2
    first, second = pieces
    ok = True

    cfg = RunConfig(agent="dev-segments", save="test", model="opus", runtime="host")
    cfg.plan, cfg.checkpoints = list(pieces), table
    cfg.chapters, cfg.chapter = [first.chapter, second.chapter], first.chapter
    run_dir = REPO_ROOT / "runs" / cfg.agent / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text("{}", encoding="utf-8")

    print(f"计划：{'；'.join(piece_label(p, table) for p in pieces)}")
    client = BridgeClient(port=args.port, timeout=120)
    try:
        t0 = time.monotonic()
        obs = enter_save(client, cfg.save, chapter=first.chapter, checkpoint=first.start)
        level = obs.get("level") or {}
        print(f"1. 进入 {level.get('sid')} 房间 {level.get('room')}（{time.monotonic() - t0:.1f} 秒）")
        ok &= check(level.get("sid") == CHAPTER_SIDS[first.chapter]
                    and level.get("room") == checkpoint_room(table, first.chapter, first.start) and obs.get("controllable"),
                    "停在起点检查点的房间，可操作")
    finally:
        client.close()

    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    session = GameSession(cfg, run_dir, metrics)
    session.connect()
    try:
        ok &= check(session._stop_rooms() == {"stop_rooms": [checkpoint_room(table, first.chapter, first.end)]},
                    "推进时带上终点房间（stop_rooms）")
        walk_into(session, first.chapter, checkpoint_room(table, first.chapter, first.end))
        t0 = time.monotonic()
        outcome = session.act(["10"], "分段自检：到达第一截的终点", None)
        level = outcome.obs.get("level") or {}
        print(f"2-3. 第一截完成并换到第二截，用时 {time.monotonic() - t0:.1f} 秒；现在 {level.get('sid')} 房间 {level.get('room')}")
        for note in outcome.auto_notes:
            print("    ", note)
        split = (metrics.state.splits or [{}])[0]
        ok &= check(split.get("from") == first.start and split.get("to") == first.end,
                    f"记下第一截的分段成绩（{fmt_ticks(split.get('ticks', 0))}，from {split.get('from')} to {split.get('to')}）")
        ok &= check(level.get("sid") == CHAPTER_SIDS[second.chapter]
                    and level.get("room") == checkpoint_room(table, second.chapter, second.start)
                    and outcome.obs.get("controllable") and session.piece == second,
                    "直接进入第二截的起点检查点，可操作")
        walk_into(session, second.chapter, checkpoint_room(table, second.chapter, second.end))
        outcome = session.act(["10"], "分段自检：到达最后一截的终点", None)
        ok &= check(session.done and session.done_reason == "分段完成", f"最后一截完成，本次运行结束（{session.done_reason}）")
        session.finish_at_checkpoint()
        ok &= check(session.end_scene == "Overworld", f"用暂停菜单返回地图（{session.end_scene}）")
    finally:
        session.close()

    client = BridgeClient(port=args.port, timeout=120)
    try:
        obs = enter_save(client, cfg.save, chapter=first.chapter, checkpoint=first.end)
        level = obs.get("level") or {}
        ok &= check(level.get("room") == checkpoint_room(table, first.chapter, first.end) and obs.get("controllable"),
                    f"5. 在选章地图上直接进入检查点（{level.get('sid')} 房间 {level.get('room')}）")
        client.call("deactivate")
    finally:
        client.close()
    print("分段正常" if ok else "分段有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
