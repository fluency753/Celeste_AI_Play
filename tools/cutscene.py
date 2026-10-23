"""像玩家一样跳过剧情：按暂停键 → 在暂停菜单里移到"跳过剧情" → 确认。

只依据桥接报告的暂停菜单（按钮文本已在桥接里反查成对话键）做决定，每一步都真实推进游戏帧，
所以录像里会和手动操作一样出现暂停菜单与跳过时的转场。不能暂停或菜单里没有"跳过"时返回 False，
由调用方按正常速度继续播放这段剧情。
"""
from __future__ import annotations

from typing import Callable

SKIP_IDS = ("menu_pause_skip_cutscene", "intro_vignette_skip")
QUIET = dict(image=False, entities=False, tiles=False)

StepHook = Callable[[list[str], dict], None]


def cutscene_skippable(obs: dict) -> bool:
    """当前是否处在可以尝试跳过的剧情里（序章开场，或关卡内的剧情且尚未在跳过中）。"""
    scene = obs.get("scene")
    if scene == "IntroVignette":
        return True
    level = obs.get("level") or {}
    return scene == "Level" and bool(level.get("inCutscene")) and not level.get("skippingCutscene")


def skip_by_pause_menu(client, on_step: StepHook | None = None, max_wait_frames: int = 60,
                       open_delay: int = 18, move_delay: int = 10) -> tuple[bool, dict, int, str]:
    """返回 (是否已选中跳过, 最新观测, 消耗的游戏帧数, 原因)。

    原因：skipped 已跳过；not_cutscene 当前不在剧情里；no_pause 这一刻打不开暂停菜单（稍后可再试）；
    no_skip 暂停菜单里没有"跳过"（这段剧情不能跳过）。

    open_delay / move_delay：菜单打开后、每次移动后停留的帧数，让录像里的操作节奏接近真人
    （默认约 0.3 秒与 0.17 秒）。
    """
    used = 0

    def step(inputs: list[str]) -> dict:
        nonlocal used
        reply = client.call("step", inputs=inputs, speed=1, **QUIET)
        used += reply["result"]["consumed"] + reply["result"].get("loadingFrames", 0)
        if on_step:
            on_step(inputs, reply["result"])
        return reply["obs"]

    obs = client.call("observe", **QUIET)["obs"]
    if not cutscene_skippable(obs):
        return False, obs, used, "not_cutscene"

    menu = obs.get("pauseMenu")
    if menu is None:
        level = obs.get("level") or {}
        if obs.get("scene") == "Level" and not level.get("canPause", True):
            return False, obs, used, "no_pause"  # 这一刻不允许暂停（例如剧情锁定了暂停）
        obs = step(["1,P"])
        waited = 0
        while not (obs.get("pauseMenu") or {}).get("focused") and waited < max_wait_frames:
            obs = step(["2"])
            waited += 2
        menu = obs.get("pauseMenu")
        if not menu or not menu.get("focused"):
            return False, obs, used, "no_pause"
        if open_delay > 0:
            obs = step([str(open_delay)])
            menu = obs.get("pauseMenu") or menu

    target = next((it["index"] for it in menu["items"]
                   if it.get("id") in SKIP_IDS and it["selectable"] and not it["disabled"]), None)
    if target is None:
        obs = step(["1,B", "10"])  # 没有"跳过"：关掉菜单，回到剧情
        return False, obs, used, "no_skip"

    for _ in range(len(menu["items"]) + 2):
        selection = menu["selection"]
        if selection == target:
            break
        obs = step([f"1,{'D' if target > selection else 'U'}", str(max(1, move_delay))])
        menu = obs.get("pauseMenu") or menu
    else:
        obs = step(["1,B", "10"])
        return False, obs, used, "no_skip"

    obs = step(["1,C", "1"])
    return True, obs, used, "skipped"


def leave_level_by_pause_menu(client, max_wait_frames: int = 60) -> dict:
    """在关卡里用暂停菜单"返回地图"（没有就选"保存并退出"），回到选章页。返回最新观测。"""
    def step(inputs: list[str]) -> dict:
        return client.call("step", inputs=inputs, speed=1, **QUIET)["obs"]

    obs = client.call("observe", **QUIET)["obs"]
    menu = obs.get("pauseMenu")
    if menu is not None and not any(it.get("id") in ("menu_pause_return", "menu_pause_savequit")
                                     for it in menu["items"] if it["selectable"]):
        obs = step(["1,B", "20"])  # 停在别的子菜单里：先退回去
        menu = obs.get("pauseMenu")
    if menu is None:
        if (obs.get("level") or {}).get("paused"):
            obs = step(["1,B", "20"])  # 暂停中但没有可识别的菜单：先取消暂停
        obs = step(["1,P"])
        waited = 0
        while not (obs.get("pauseMenu") or {}).get("focused") and waited < max_wait_frames:
            obs = step(["2"])
            waited += 2
        menu = obs.get("pauseMenu")
        if not menu:
            raise RuntimeError("打不开暂停菜单，无法离开关卡")
    ids = [it.get("id") for it in menu["items"]]
    wanted = "menu_pause_return" if "menu_pause_return" in ids else "menu_pause_savequit"
    target = next(it["index"] for it in menu["items"] if it.get("id") == wanted)
    for _ in range(len(menu["items"]) + 2):
        if menu["selection"] == target:
            break
        obs = step([f"1,{'D' if target > menu['selection'] else 'U'}", "6"])
        menu = obs.get("pauseMenu") or menu
    obs = step(["1,C", "20"])
    # 选中后会弹出确认菜单：标题是同一个按钮文字（不可选），下面是"是 / 取消"，默认选中"是"
    for _ in range(3):
        menu = obs.get("pauseMenu")
        if not menu or not menu.get("focused") or not menu["items"]:
            break
        head = menu["items"][0]
        if head.get("id") != wanted or head["selectable"]:
            break
        yes = next((it["index"] for it in menu["items"] if it["selectable"] and not it["disabled"]), None)
        if yes is None:
            break
        while menu["selection"] != yes:
            obs = step([f"1,{'D' if yes > menu['selection'] else 'U'}", "6"])
            menu = obs.get("pauseMenu") or menu
        obs = step(["1,C", "30"])
    # 等关卡真正退出（擦除转场、保存），避免调用方在退出途中又按暂停
    for _ in range(60):
        if obs.get("scene") != "Level":
            break
        obs = step(["10"])
    return obs
