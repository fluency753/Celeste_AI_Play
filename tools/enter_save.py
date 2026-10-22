"""菜单宏：从标题画面一路进入指定存档的关卡，跳过开场，停在 Madeline 可操作的第一帧。

只依据桥接报告的菜单状态做决定，不看画面。用法：
    python tools/enter_save.py test
    python tools/enter_save.py opus5.5 --leave-active
"""
from __future__ import annotations

import argparse
import sys

from bridge_client import BridgeClient, summarize_obs

QUIET = dict(image=False, entities=False, tiles=False, trajectory=False)


def step(c: BridgeClient, inputs: list[str], **extra) -> dict:
    return c.call("step", inputs=inputs, **{**QUIET, **extra})


def press(c: BridgeClient, key: str, settle: int = 12) -> dict:
    return step(c, [f"1,{key}", str(settle)])


def enter_save(c: BridgeClient, save_name: str, max_actions: int = 120) -> dict:
    obs = c.call("activate", entities=False, tiles=False)["obs"]
    for _ in range(max_actions):
        scene = obs.get("scene")
        menu = obs.get("menu") or {}

        if scene == "Level":
            level = obs["level"]
            if obs.get("controllable"):
                return obs
            if level["inCutscene"] and not level["skippingCutscene"]:
                c.call("skip_cutscene")
            obs = step(c, ["600"], stop_on=["controllable"], speed=4)["obs"]
            continue

        if scene == "IntroVignette":
            c.call("skip_cutscene")
            obs = step(c, ["30"], speed=4)["obs"]
            continue

        if scene != "Overworld" or menu.get("next"):
            obs = step(c, ["10"], speed=4)["obs"]  # 加载或菜单切换中
            continue

        current = menu.get("current")
        if current in ("OuiTitleScreen", "OuiMainMenu", "OuiChapterSelect", "OuiChapterPanel"):
            obs = press(c, "C", settle=30)["obs"]
        elif current == "OuiFileSelect":
            obs = handle_file_select(c, obs, save_name)
        else:
            obs = step(c, ["10"])["obs"]
    raise RuntimeError(f"未能在 {max_actions} 步内进入存档 {save_name}：\n{summarize_obs(obs)}")


def handle_file_select(c: BridgeClient, obs: dict, save_name: str) -> dict:
    menu = obs["menu"]
    targets = [s["index"] for s in menu["slots"] if s["exists"] and s["name"] == save_name]
    if not targets:
        raise RuntimeError(f"选档页里找不到名为 {save_name} 的存档")
    target = targets[0]

    if menu["slotSelected"]:
        if menu["slotIndex"] != target:
            return press(c, "B", settle=20)["obs"]  # 选错了槽位，退回
        if menu.get("buttonIndex", 0) != 0:
            return press(c, "U")["obs"]  # 回到第一个按钮（继续/开始）
        return press(c, "C", settle=30)["obs"]

    diff = target - menu["slotIndex"]
    if diff:
        key = "D" if diff > 0 else "U"
        return step(c, [f"1,{key}", "10"] * abs(diff) + ["10"])["obs"]
    return press(c, "C", settle=30)["obs"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("save", help="存档名，如 test、opus5.5")
    ap.add_argument("--port", type=int, default=47774)
    ap.add_argument("--leave-active", action="store_true", help="完成后保持接管（默认交还控制）")
    args = ap.parse_args()

    c = BridgeClient(port=args.port)
    try:
        obs = enter_save(c, args.save)
        print(summarize_obs(obs))
        if not args.leave_active:
            c.call("deactivate")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
