"""菜单宏：从标题画面一路进入指定存档的关卡，用暂停菜单跳过开场，停在 Madeline 可操作的第一帧。

只依据桥接报告的菜单状态做决定，不看画面。用法：
    python tools/enter_save.py test
    python tools/enter_save.py opus5.5 --leave-active
"""
from __future__ import annotations

import argparse
import sys

try:
    from bridge_client import BridgeClient, summarize_obs
    from cutscene import leave_level_by_pause_menu, skip_by_pause_menu
except ImportError:  # 作为 tools 包导入时
    from tools.bridge_client import BridgeClient, summarize_obs
    from tools.cutscene import leave_level_by_pause_menu, skip_by_pause_menu

QUIET = dict(image=False, entities=False, tiles=False, trajectory=False)


def step(c: BridgeClient, inputs: list[str], **extra) -> dict:
    return c.call("step", inputs=inputs, **{**QUIET, **extra})


def press(c: BridgeClient, key: str, settle: int = 12) -> dict:
    return step(c, [f"1,{key}", str(settle)])


def area_id(sid: str | None) -> int | None:
    """官方章节的编号：Celeste/0-Intro → 0，Celeste/1-ForsakenCity → 1。"""
    try:
        return int(str(sid).split("/", 1)[1].split("-", 1)[0])
    except (IndexError, ValueError):
        return None


def enter_save(c: BridgeClient, save_name: str, max_actions: int = 120, chapter: int | None = None) -> dict:
    """chapter 为章节编号（0 序章、1 被遗弃之城、2 旧址）；None 表示进入选章页当前选中的章节。"""
    obs = c.call("activate", entities=False, tiles=False)["obs"]
    tried_skip: set[str] = set()
    vignette_tries = 0
    for _ in range(max_actions):
        scene = obs.get("scene")
        menu = obs.get("menu") or {}

        # 进入关卡之后的部分会被录像：一律按正常速度推进，剧情用暂停菜单跳过
        if scene == "Level":
            level = obs["level"]
            if chapter is not None and area_id(level.get("sid")) != chapter and (obs.get("controllable") or level.get("paused")):
                obs = leave_level_by_pause_menu(c)  # 在别的章节里：先返回地图
                continue
            if obs.get("controllable"):
                return obs
            if level["inCutscene"] and not level["skippingCutscene"] and level["room"] not in tried_skip:
                tried_skip.add(level["room"])
                skipped, obs, _, _ = skip_by_pause_menu(c)
                if skipped:
                    continue
            obs = step(c, ["120"], stop_on=["controllable"])["obs"]
            continue

        if scene == "IntroVignette":
            if vignette_tries < 3:  # 开场刚淡入时可能还不接受暂停，隔半秒再试
                vignette_tries += 1
                skipped, obs, _, _ = skip_by_pause_menu(c)
                if skipped:
                    continue
            obs = step(c, ["30"])["obs"]
            continue

        if scene == "LevelEnter":
            obs = press(c, "C", settle=30)["obs"]  # 第一次进入章节时的明信片：按确认继续
            continue

        if scene != "Overworld" or menu.get("next"):
            obs = step(c, ["10"], speed=4)["obs"]  # 加载或菜单切换中
            continue

        current = menu.get("current")
        if current == "OuiChapterSelect" and chapter is not None and menu.get("area", chapter) != chapter:
            obs = press(c, "R" if chapter > menu["area"] else "L", settle=20)["obs"]
        elif current == "OuiChapterPanel" and chapter is not None and menu.get("panelArea", chapter) != chapter:
            obs = press(c, "B", settle=30)["obs"]  # 打开的是别的章节面板：退回选章页
        elif current in ("OuiTitleScreen", "OuiMainMenu", "OuiChapterSelect", "OuiChapterPanel"):
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
    ap.add_argument("--chapter", type=int, help="章节编号：0 序章、1 被遗弃之城、2 旧址；默认进入选章页当前选中的章节")
    ap.add_argument("--leave-active", action="store_true", help="完成后保持接管（默认交还控制）")
    args = ap.parse_args()

    c = BridgeClient(port=args.port)
    try:
        obs = enter_save(c, args.save, chapter=args.chapter)
        print(summarize_obs(obs))
        if not args.leave_active:
            c.call("deactivate")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
