"""模拟器校准：在 test 存档里逐个房间录下"起始状态 + 随机操作 + 游戏的逐帧结果"，供本机模拟器逐帧对照。

不调用模型。每个用例：
  1. sim_state 取当前帧的完整场景（起始状态）；
  2. 生成一段随机但像人类的操作（跑、跳、蹬墙、攀爬、八方向冲刺、蹲冲、超级冲刺等），用 step 执行并记下逐帧轨迹；
  3. 再取一次 sim_state（结束状态），模拟器推演完可以逐字段对照。
死亡或离开房间时这一段提前结束；死亡后等到可以操作再继续，房间换了就重新载入目标房间。

前提：游戏开着（桥接 0.9.0 以上），test 存档停在要测的章节里、Madeline 可操作。

用法（仓库根目录）：
    python calibration/sim_capture.py --rooms 1,2,3 --cases 6 --frames 90 --out runs/simcases/1a
    python calibration/sim_capture.py --rooms current --cases 3
    python calibration/sim_capture.py --rooms 4,5 --targets --cases 2     # 逐个实体：把 Madeline 放到弹簧、水晶、移动块等旁边再随机操作
"""
from __future__ import annotations

import argparse
import json
import re
import random
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.bridge_client import BridgeClient  # noqa: E402

QUIET = dict(image=False, entities=False, tiles=False)
DIRS = ["", "L", "R", "U", "D", "L,U", "R,U", "L,D", "R,D"]


def wait_controllable(c: BridgeClient, max_frames: int = 900) -> dict:
    obs = c.call("observe", image=False, entities=False, tiles=False)["obs"]
    waited = 0
    while not obs.get("controllable") and waited < max_frames:
        obs = c.call("step", inputs=["30"], stop_on=["controllable"], trajectory=False, **QUIET)["obs"]
        waited += 30
    return obs


LOAD_EXTRA: dict = {}


def load_room(c: BridgeClient, room: str, max_frames: int = 1200) -> dict:
    """载入房间并等到新房间里可以操作（载入在下一帧才生效，之前的观测还是旧场景）。"""
    c.call("load_room", room=room, **LOAD_EXTRA)
    obs = c.call("step", inputs=["1"], trajectory=False, **QUIET)["obs"]
    waited = 0
    while waited < max_frames and not (obs.get("scene") == "Level" and obs.get("controllable")
                                       and (obs.get("level") or {}).get("room") == room):
        obs = c.call("step", inputs=["10"], trajectory=False, **QUIET)["obs"]
        waited += 10
    if not obs.get("controllable"):
        raise RuntimeError(f"room {room}: not controllable after {waited} frames")
    return obs


TARGET_CATS = {"spring", "refill", "dream_block", "dynamic_solid", "interactive", "hazard", "jumpthru", "booster", "feather", "solid"}
# 观测里归为"其他"、但会影响运动或判定的实体，也定点测
EXTRA_TARGETS = {"IceBlock", "FireBarrier", "CoreModeToggle", "Trapdoor", "WallBooster", "BadelineBoost", "ClutterSwitch",
                 "ClutterDoor", "ClutterCabinet", "Door", "LightningBreakerBox", "SeekerStatue", "Torch", "HeartGemDoor",
                 "TheoCrystalPedestal", "FlingBird", "Cloud", "RidgeGate", "Gondola", "SummitCheckpoint"}
# 地形、无形墙等基础的实心块和已经大量覆盖的类型不定点测
NOT_TARGETS = {"Player", "Spikes", "JumpthruPlatform", "SolidTiles", "InvisibleBarrier", "ExitBlock", "FakeWall", "CoverupWall",
               "Solid", "Platform"}


def target_spots(c: BridgeClient) -> list[tuple[str, float, float, str]]:
    """当前房间里值得专门测试的实体，以及放置 Madeline 的位置（脚下中点）：上方、左侧、右侧。
    返回 (名字, x, y, 覆盖键)；覆盖键是实体类型加放置方位，同一类型的不同个体逻辑相同，测够几次就不再重复。"""
    obs = c.call("observe", image=False, entities=True, tiles=False, scope="room")["obs"]
    spots = []
    for e in obs.get("entities") or []:
        if (e.get("cat") not in TARGET_CATS and e.get("type") not in EXTRA_TARGETS) or e.get("type") in NOT_TARGETS:
            # 尖刺和单向平台随机用例里已经覆盖得很多，不再逐个定点测
            continue
        x, y, w, h = e["x"], e["y"], e["w"], e["h"]
        name = f"{e['type']}@{int(x)},{int(y)}"
        kind = e["type"]
        spots.append((name, x + w / 2, y - 1, kind))
        if e.get("cat") in ("dynamic_solid", "spring", "dream_block", "solid"):
            spots.append((name + "/L", x - 6, y + h, kind + "/L"))
            spots.append((name + "/R", x + w + 6, y + h, kind + "/R"))
    return spots


TARGET_TAIL = re.compile(r'"target":\s*"([^"]+)"')


def load_coverage(path: Path, root: Path) -> Counter:
    """已经定点测过的（类型/方位 → 个体数）。第一次用时从已有用例文件里统计（目标名写在每个文件末尾）。"""
    if path.exists():
        return Counter(json.loads(path.read_text(encoding="utf-8")))
    cov: Counter = Counter()
    seen = set()
    for f in root.glob("*/*.json"):
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 300))
            m = TARGET_TAIL.search(fh.read().decode("utf-8", "replace"))
        if not m:
            continue
        target = m.group(1)
        key = (f.parent.name, f.name.split("_")[0], target)
        if key in seen:
            continue
        seen.add(key)
        typ, _, rest = target.partition("@")
        side = "/" + rest.rsplit("/", 1)[1] if "/" in rest else ""
        cov[typ + side] += 1
    return cov


def random_inputs(rng: random.Random, frames: int) -> list[str]:
    """一段像人类的随机操作：方向按住若干帧，期间夹杂跳、冲刺、抓。返回逐帧的按键串。"""
    out: list[str] = []
    while len(out) < frames:
        kind = rng.random()
        hold = rng.choice(["", "L", "R", "R", "L"])
        span = rng.randint(4, 24)
        grab = rng.random() < 0.25
        for i in range(span):
            keys = [k for k in hold.split(",") if k]
            if grab:
                keys.append("G")
            out.append(",".join(keys))
        if kind < 0.35:
            # 跳：按住若干帧（可变跳跃高度）
            jump_len = rng.randint(1, 14)
            d = rng.choice(["", "L", "R", "R,U", "L,U"])
            for i in range(jump_len):
                keys = [k for k in d.split(",") if k] + ["J"]
                if grab:
                    keys.append("G")
                out.append(",".join(keys))
        elif kind < 0.6:
            # 冲刺：八个方向之一，偶尔接跳（超级冲刺、蹭墙跳）
            d = rng.choice(DIRS[1:])
            keys = [k for k in d.split(",") if k] + [rng.choice(["X", "X", "Y"])]
            out.append(",".join(keys))
            for i in range(rng.randint(2, 16)):
                out.append(d)
            if rng.random() < 0.4:
                out.append(",".join([k for k in rng.choice(["L", "R", ""]).split(",") if k] + ["J"]))
        elif kind < 0.7:
            out.append("D,Z" if rng.random() < 0.5 else "R,Z")
        elif kind < 0.85:
            # 攀爬：抓住并上下移动
            d = rng.choice(["U", "D", "", "U"])
            for i in range(rng.randint(4, 30)):
                out.append(",".join([k for k in d.split(",") if k] + ["G"]))
        else:
            for i in range(rng.randint(1, 10)):
                out.append("")
    return out[:frames]


def compress(frames: list[str]) -> list[str]:
    """逐帧按键串 → 桥接的计划写法（"帧数,键,键"）。"""
    out: list[str] = []
    i = 0
    while i < len(frames):
        j = i
        while j < len(frames) and frames[j] == frames[i]:
            j += 1
        n = j - i
        out.append(f"{n},{frames[i]}" if frames[i] else str(n))
        i = j
    return out


def capture_case(c: BridgeClient, rng: random.Random, frames: int) -> dict:
    s0 = c.call("sim_state")["state"]
    inputs = random_inputs(rng, frames)
    result = c.call("step", inputs=compress(inputs), stop_on=["death", "room"], trajectory=True, **QUIET)
    s1 = c.call("sim_state")["state"]
    res = result["result"]
    return {
        "room": s0["level"]["room"],
        "inputs": inputs,
        "state0": s0,
        "result": {k: res.get(k) for k in ("stop", "consumed", "startFrame", "endFrame", "trajectoryFormat", "trajectory", "events")},
        "state1": s1,
        "obs": {k: result["obs"].get(k) for k in ("frame", "controllable")},
    }



def capture_room(c: BridgeClient, args, room: str, rng: random.Random, out: Path, coverage: Counter, cov_path: Path) -> int:
    """录一个房间的用例，返回录了几个。"""
    total = 0
    obs = load_room(c, room) if args.rooms != "current" else wait_controllable(c)
    if args.targets:
        for name, x, y, kind in target_spots(c):
            if args.per_type and coverage[kind] >= args.per_type:
                continue
            for k in range(args.cases):
                obs = load_room(c, room)
                try:
                    c.call("teleport", x=x, y=y)
                except RuntimeError as ex:
                    print(f"{room} {name}: {ex}")
                    break
                settled = c.call("step", inputs=[str(args.settle)], trajectory=False, **QUIET)["obs"]
                if not settled.get("controllable") or (settled.get("level") or {}).get("room") != room:
                    # 放到了危险的地方（落地即死）或房间边缘（开始换房间）：这个位置不录
                    print(f"{room} {name}: not controllable after placement, skipped", flush=True)
                    break
                case = capture_case(c, rng, args.frames)
                case["target"] = name
                safe = name.replace("/", "_").replace(",", "_").replace("@", "_")
                path = out / f"{room}_{safe}_{k:02d}.json"
                path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
                total += 1
                res = case["result"]
                print(f"{room} {name} #{k}: {res['consumed']} 帧，{res['stop']}", flush=True)
                if k == 0:
                    coverage[kind] += 1
                    cov_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
        return total
    for k in range(args.cases):
        if not obs.get("controllable") or obs["level"]["room"] != room:
            obs = load_room(c, room)
        t0 = time.time()
        case = capture_case(c, rng, args.frames)
        path = out / f"{room}_{k:02d}.json"
        path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        total += 1
        res = case["result"]
        print(f"{room} #{k}: {res['consumed']} 帧，{res['stop']}，{path.stat().st_size // 1024} KB，"
              f"{time.time() - t0:.1f} 秒", flush=True)
        obs = wait_controllable(c)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rooms", default="current", help="逗号分隔的房间名；current 表示当前房间")
    ap.add_argument("--cases", type=int, default=5, help="每个房间的用例数")
    ap.add_argument("--frames", type=int, default=90, help="每个用例的帧数上限")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="runs/simcases")
    ap.add_argument("--port", type=int, default=47774)
    ap.add_argument("--targets", action="store_true", help="对房间里的每个可交互实体各录若干用例（先把 Madeline 放到它旁边）")
    ap.add_argument("--settle", type=int, default=10, help="放置后先等待的帧数")
    ap.add_argument("--level-flags", default="", help="载入房间前预设的房间标记（逗号分隔），例如 2A 的追逐需要 3")
    ap.add_argument("--flags", default="", help="载入房间前预设的会话标记（逗号分隔），例如 evil_maddy_intro")
    ap.add_argument("--dream-dash", choices=["on", "off"], help="载入房间时打开或关闭梦境冲刺")
    ap.add_argument("--dashes", type=int, help="载入房间时的冲刺次数上限")
    ap.add_argument("--chapter", type=int, help="章节编号（0 序章、1、2……，A 面）；默认为当前章节")
    ap.add_argument("--per-type", type=int, default=2,
                    help="定点测试时同一类实体（加放置方位）最多测几个个体，跨章节累计；0 表示不限")
    ap.add_argument("--coverage", help="定点测试覆盖记录文件，默认为输出目录上一级的 _coverage.json")
    ap.add_argument("--assets", help="只取模拟器用的资源表（贴图尺寸、精灵动画）存到这个文件，然后退出")
    ap.add_argument("--speed", type=int, default=60,
                    help="倍速：游戏循环每个节拍推进的帧数（1 为正常速度，最大 600）；只跳过中间帧的绘制，游戏逻辑逐帧照常执行")
    args = ap.parse_args()
    QUIET["speed"] = max(1, min(600, args.speed))

    if args.level_flags:
        LOAD_EXTRA["levelFlags"] = [x for x in args.level_flags.split(",") if x]
    if args.flags:
        LOAD_EXTRA["flags"] = [x for x in args.flags.split(",") if x]
    if args.dream_dash:
        LOAD_EXTRA["dreamDash"] = args.dream_dash == "on"
    if args.dashes is not None:
        LOAD_EXTRA["dashes"] = args.dashes
    if args.chapter is not None:
        LOAD_EXTRA["chapter"] = args.chapter
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    c = BridgeClient(port=args.port)
    obs = c.call("activate", entities=False, tiles=False)["obs"]
    if args.assets:
        assets = c.call("sim_assets")["assets"]
        Path(args.assets).write_text(json.dumps(assets, ensure_ascii=False), encoding="utf-8")
        print(f"资源表 → {args.assets}")
        return 0
    if (obs.get("save") or {}).get("name") != "test":
        print("需要 test 存档", file=sys.stderr)
        return 2
    cov_path = Path(args.coverage) if args.coverage else out.parent / "_coverage.json"
    coverage = load_coverage(cov_path, out.parent) if args.targets else Counter()
    rooms = [obs["level"]["room"]] if args.rooms == "current" else [r.strip() for r in args.rooms.split(",") if r.strip()]
    total = 0
    for room in rooms:
        try:
            total += capture_room(c, args, room, rng, out, coverage, cov_path)
        except (RuntimeError, OSError) as ex:
            # 某个房间载入失败或连接中断：记下来，继续下一个房间
            print(f"{room}: {ex}", flush=True)
    print(f"共 {total} 个用例 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
