"""章节衔接的自检（不启动大模型，只用 test 存档）：用主控的同一套代码，从选章页进入第一章，在每一章把 Madeline
直接放到终点前、走进去通关，确认主控自动走完结算画面（序章没有结算画面，直接回地图）、进入下一章，最后停在
最后一章第一个房间可操作的第一帧。

    python calibration\\chain_check.py                  # 序章 → 1A → 2A（与正式挑战相同）
    python calibration\\chain_check.py --chapters 1,2   # 1A → 2A

想连"第一次通关才有的画面"（第一次进入章节的明信片、通关后解锁下一章的动画）一起彩排，先用
calibration\\reset_test_save.py --like opus5.5 把 test 存档重置成正式存档的初始进度。
注意：会让 test 存档记下这些章节已通关；不会碰正式存档。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from orchestrator.config import CHAPTER_SIDS, REPO_ROOT, RunConfig, chapter_name_by_id  # noqa: E402
from orchestrator.game_session import GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402
from orchestrator.observation import fmt_ticks  # noqa: E402
from tools.bridge_client import BridgeClient  # noqa: E402
from tools.enter_save import enter_save  # noqa: E402

# 每章的终点：载入哪个房间、把 Madeline 放到哪里（世界坐标）、再按什么键走进终点
ENDINGS = {
    # 序章：最后一个房间 3，桥尽头那只鸟（2104, 40）。Madeline 越过 x = 2012、且离地 10–20 像素时触发结尾剧情
    0: ("3", (2015, 40), ["1,R,J", "20,R,J"]),
    # 1A：最后一个房间 end 的终点触发器在房间里 (240, 120)，宽 64 高 32
    1: ("end", (4050, -3056), ["90,R"]),
}


def put_at_ending(session: GameSession, chapter: int) -> list[str]:
    room, (x, y), keys = ENDINGS[chapter]
    with session.lock:
        c = session.client
        c.call("load_room", room=room, chapter=chapter)
        for _ in range(40):  # 等房间载入完、恢复可操作
            obs = c.call("step", inputs=["10"], image=False, entities=False, tiles=False)["obs"]
            if obs.get("controllable") and (obs.get("level") or {}).get("room") == room:
                break
        c.call("teleport", x=x, y=y)
        c.call("step", inputs=["20"], image=False, entities=False, tiles=False)
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chapters", default="0,1,2", help="依次通关的章节编号（最后一章只检查能否进入）")
    args = ap.parse_args()
    chapters = [int(x) for x in args.chapters.split(",") if x.strip()]
    missing = [c for c in chapters[:-1] if c not in ENDINGS]
    if len(chapters) < 2 or missing:
        print(f"至少两章，且除最后一章外都要有终点配方（现有：{sorted(ENDINGS)}）", file=sys.stderr)
        return 2

    cfg = RunConfig(agent="dev-chain", save="test", model="opus")
    cfg.chapters = chapters
    cfg.chapter = chapters[0]
    run_dir = REPO_ROOT / "runs" / cfg.agent / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    # 与主控相同：先用菜单宏从当前画面（标题、地图或别的章节）进入第一章
    client = BridgeClient(port=cfg.bridge_port, timeout=120)
    try:
        t0 = time.monotonic()
        obs = enter_save(client, cfg.save, chapter=cfg.chapter)
        print(f"进入 {obs['level']['sid']} 房间 {obs['level']['room']}（菜单宏 {time.monotonic() - t0:.1f} 秒）")
    finally:
        client.close()

    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    session = GameSession(cfg, run_dir, metrics)
    session.connect()
    ok = True
    try:
        for chapter, following in zip(chapters, chapters[1:]):
            keys = put_at_ending(session, chapter)
            t0 = time.monotonic()
            outcome = session.act(keys, f"衔接自检：走进{chapter_name_by_id(chapter)}的终点", ["death", "room"])
            level = outcome.obs.get("level") or {}
            print(f"【{chapter_name_by_id(chapter)}】停止原因 {outcome.result['stop']}；用时 {time.monotonic() - t0:.0f} 秒")
            for note in outcome.auto_notes:
                print("   ", note)
            print(f"   现在：{level.get('sid')} 房间 {level.get('room')}，{'可操作' if outcome.obs.get('controllable') else '不可操作'}")
            step_ok = (level.get("sid") == CHAPTER_SIDS[following] and outcome.obs.get("controllable")
                       and not session.done)
            if not step_ok:
                print(f"   没有进入{chapter_name_by_id(following)}：衔接失败")
                ok = False
                break
        splits = [(s["chapter"], fmt_ticks(s["ticks"]), s["deaths"]) for s in metrics.state.splits or []]
        print("分段成绩：", splits)
        if ok and len(splits) != len(chapters) - 1:
            print("分段成绩的数目不对")
            ok = False
        print("衔接正常" if ok else "衔接失败")
        return 0 if ok else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
