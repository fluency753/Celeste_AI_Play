"""原生存档载入与菜单导航；默认载入后保持冻结，不退出或重启章节。

只依据桥接报告的菜单状态做决定，不看画面。用法：
    python tools/enter_save.py test --save-slot 10
    python tools/enter_save.py test --destination play --chapter 3 --leave-active
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.bridge_client import BridgeClient, summarize_obs
from tools.cutscene import leave_level_by_pause_menu, skip_by_pause_menu, story_pending
from tools.menu_navigation import checked_step, MenuActionBlocked

QUIET = dict(image=False, entities=False, tiles=False, trajectory=False)


def step(c: BridgeClient, inputs: list[str], **extra) -> dict:
    return checked_step(c, inputs, **{**QUIET, **extra})


def press(c: BridgeClient, key: str, settle: int = 12) -> dict:
    return step(c, [f"1,{key}", str(settle)])


def area_id(sid: str | None) -> int | None:
    """官方章节的编号：Celeste/0-Intro → 0，Celeste/1-ForsakenCity → 1。"""
    if sid == "Celeste/LostLevels":
        return 10
    try:
        return int(str(sid).split("/", 1)[1].split("-", 1)[0])
    except (IndexError, ValueError):
        return None


def _enter_save(c: BridgeClient, save_name: str, max_actions: int = 120, chapter: int | None = None,
               menu_speed: int = 4, should_stop=None, create_if_missing: bool = False,
               save_slot: int | None = None, cutscene_mode: str = 'auto', checkpoint: int = 0,
               destination: str = 'play') -> dict:
    """chapter 为章节编号（0 序章、1 被遗弃之城、2 旧址）；None 表示进入选章页当前选中的章节。
    也可以从通关之后接着用：结算画面、回到地图、选下一章、进入，一路处理到可操作的第一帧。
    checkpoint > 0（按段通关）：载入存档后，在关卡里或选章地图上用桥接的 enter_checkpoint 直接进入这一章的
    第 checkpoint 个检查点（与在选章面板里选它相同，没到过的检查点也能进；需要桥接 1.0.6）。
    menu_speed：等待菜单切换与载入时每次渲染推进几帧（开跑前用 4 省时间；正式运行中途换章时用 1，录像不快进）。"""
    if create_if_missing and not c.call("hello").get("create_save"):
        raise RuntimeError("新建存档需要 CelesteAIBridge 0.12.4 或更新版本，请更新桥接 Mod。")
    if checkpoint and chapter is None:
        raise ValueError("进入检查点需要指定章节")
    stop_at_chapter_select = destination == 'chapter_select'
    if destination != 'play' and (chapter is not None or checkpoint):
        raise ValueError('仅进入存档时不指定章节或检查点，由智能体自行导航')
    obs = c.call("activate", entities=False, tiles=False)["obs"]
    teleport = bool(checkpoint)     # 还要传送到检查点
    if teleport and not c.call("hello").get("checkpoints"):
        raise RuntimeError("按段通关（进入任意检查点）需要 CelesteAIBridge 1.0.6 或更新版本，请更新桥接 Mod。")
    native_started = False
    tried_skip: set[str] = set()
    vignette_tries = 0
    selected_save_slot = save_slot
    for _ in range(max_actions):
        if should_stop is not None and should_stop():
            return obs
        scene = obs.get("scene")
        menu = obs.get("menu") or {}
        loaded = (obs.get("save") or {}).get("name")
        wrong_save = loaded is not None and loaded != save_name  # 游戏里当前载入的是别的存档
        wrong_save = wrong_save or (selected_save_slot is not None and (obs.get('save') or {}).get('slot') != selected_save_slot)
        if create_if_missing and not native_started:
            wrong_save = True  # 同名的已加载存档也不能代替显式新建
        if destination == 'loaded':
            if scene in {'Level', 'IntroVignette', 'LevelEnter'}:
                if wrong_save:
                    raise RuntimeError('载入已停止：当前关卡属于其他存档，不自动退出或切换')
                return obs
            if not wrong_save and scene == 'Overworld' and not menu.get('next') and menu.get('current') in {'OuiChapterSelect','OuiChapterPanel'}:
                return obs  # 原生载入没有在玩章节；保留地图，不代选一章。
        if teleport and not wrong_save and loaded is not None and (
                scene == "Level" or (scene == "Overworld" and not menu.get("next")
                                     and menu.get("current") in ("OuiChapterSelect", "OuiChapterPanel"))):
            c.call("enter_checkpoint", chapter=chapter, checkpoint=checkpoint)
            teleport = False
            obs = step(c, ["10"], speed=menu_speed)["obs"]  # 开始载入：之后按下面的流程等到可操作
            continue
        if cutscene_mode == 'agent' and not stop_at_chapter_select and not wrong_save and not teleport and story_pending(obs):
            if scene != 'Level' or chapter is None or area_id((obs.get('level') or {}).get('sid')) == chapter:
                return obs

        # 进入关卡之后的部分会被录像：一律按正常速度推进，剧情用暂停菜单跳过
        if scene == "Level":
            level = obs["level"]
            elsewhere = stop_at_chapter_select or wrong_save or (chapter is not None and area_id(level.get("sid")) != chapter)
            if elsewhere and (obs.get("controllable") or level.get("paused")):
                obs = leave_level_by_pause_menu(c)  # 在别的存档或别的章节里：先返回地图
                continue
            # 桥接 1.0.3 起"可操作"只看角色的真实状态，剧情刚开始的几帧也算可操作：自动跳过剧情时先试着跳过再交出
            if obs.get("controllable") and (cutscene_mode == 'agent' or not level.get("inCutscene")
                                            or level.get("skippingCutscene") or level["room"] in tried_skip):
                return obs
            if level["inCutscene"] and not level["skippingCutscene"] and level["room"] not in tried_skip:
                skipped, obs, _, reason = skip_by_pause_menu(c)
                # no_pause 是入场动画期间的暂时状态，不能记成该房间已处理。
                if skipped or reason == "no_skip":
                    tried_skip.add(level["room"])
                if skipped:
                    continue
                if (obs.get('level') or {}).get('dialogChoices'):
                    return obs
                if reason == "no_pause":
                    # 这一刻打不开暂停菜单：推进几帧再试。已经可操作时不能用"直到可操作"截断——桥接的截断条件
                    # 开始时就满足就一帧都不推进，关卡又是冻结的，会原地空转到步数用完
                    stops = ["uncontrollable"] if obs.get("controllable") else ["controllable"]
                    obs = step(c, ["10"], stop_on=stops)["obs"]
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

        if scene == "AreaComplete":
            obs = press(c, "C", settle=30)["obs"]  # 章节结算画面：等动画走完后按确认回到地图
            continue

        if scene != "Overworld" or menu.get("next"):
            obs = step(c, ["10"], speed=menu_speed)["obs"]  # 加载或菜单切换中
            continue

        current = menu.get("current")
        if stop_at_chapter_select and not wrong_save and current == 'OuiChapterSelect':
            return obs
        if stop_at_chapter_select and not wrong_save and current == 'OuiChapterPanel':
            obs = press(c, 'B', settle=30)['obs']
            continue
        if wrong_save and current in ("OuiChapterSelect", "OuiChapterPanel"):
            obs = press(c, "B", settle=30)["obs"]  # 别的存档的地图：退回主菜单，再从选档页选
        elif current == "OuiChapterSelect" and chapter is not None and menu.get("area", chapter) != chapter:
            obs = press(c, "R" if chapter > menu["area"] else "L", settle=20)["obs"]
        elif current == "OuiChapterPanel" and chapter is not None and menu.get("panelArea", chapter) != chapter:
            obs = press(c, "B", settle=30)["obs"]  # 打开的是别的章节面板：退回选章页
        elif current in ("OuiTitleScreen", "OuiMainMenu", "OuiChapterSelect", "OuiChapterPanel"):
            obs = press(c, "C", settle=30)["obs"]
        elif current == "OuiFileSelect":
            if native_started:
                obs = step(c, ['10'], speed=menu_speed)['obs']
                continue
            if create_if_missing:
                created = c.call('create_save', name=save_name)
                selected_save_slot = created['slot']
                native_started = True
                obs = c.call('observe', **QUIET)['obs']
                continue
            obs = handle_file_select(c, obs, save_name, save_slot=save_slot)
        else:
            obs = step(c, ["10"])["obs"]
    raise RuntimeError(f"未能在 {max_actions} 步内进入存档 {save_name}：\n{summarize_obs(obs)}")


def enter_save(c: BridgeClient, save_name: str, max_actions: int = 120, chapter: int | None = None,
               menu_speed: int = 4, should_stop=None, create_if_missing: bool = False,
               save_slot: int | None = None, cutscene_mode: str = 'auto', checkpoint: int = 0,
               stop_at_chapter_select: bool = False) -> dict:
    """进入并准备游玩章节；恢复存档请用 load_save_and_freeze，打开选章页请用 open_chapter_select。"""
    if stop_at_chapter_select:
        raise ValueError('stop_at_chapter_select 已停用：打开选章地图请用 open_chapter_select；'
                         '恢复存档并立即冻结请用 load_save_and_freeze，不能先退出关卡')
    return _enter_save(c, save_name, max_actions, chapter, menu_speed, should_stop, create_if_missing,
                       save_slot, cutscene_mode, checkpoint)


def open_chapter_select(c: BridgeClient, save_name: str, *, save_slot: int | None = None,
                        max_actions: int = 120, menu_speed: int = 4, should_stop=None) -> dict:
    """明确退出当前章节并打开指定存档的选章地图；不得用于恢复关卡进度。"""
    return _enter_save(c, save_name, max_actions=max_actions, save_slot=save_slot, menu_speed=menu_speed,
                       should_stop=should_stop, cutscene_mode='agent', destination='chapter_select')


class _LoadOnlyClient:
    """Decorate only this macro's requests; do not replace the connection's authoritative permissions."""
    def __init__(self, client):
        self.client = client

    def call(self, cmd, **options):
        if cmd == 'step':
            options['stop_on'] = list(dict.fromkeys([*options.get('stop_on', []), 'chapter_loaded']))
        return self.client.call(cmd, **options)


def load_save_and_freeze(c: BridgeClient, save_name: str, *, save_slot: int,
                         max_actions: int = 120, menu_speed: int = 4, should_stop=None) -> dict:
    """只按原生继续载入指定存档，首次进入章节即冻结；不退出、不重启、不选章或跳剧情。

    存档没有进行中的章节时，返回原生选章菜单，不自行选择章节。返回值保留实际 scene。
    """
    if type(save_slot) is not int or save_slot < 0:
        raise ValueError('载入后冻结须指定非负整数存档槽位')
    if c.call('hello').get('chapter_loaded_stop') is not True:
        raise RuntimeError('载入后立即冻结需要支持 chapter_loaded_stop 的新版桥接；未发送游戏输入')
    return _enter_save(_LoadOnlyClient(c), save_name, max_actions=max_actions, save_slot=save_slot,
                       menu_speed=menu_speed, should_stop=should_stop, cutscene_mode='agent', destination='loaded')


def to_file_select(c: BridgeClient, max_actions: int = 120, menu_speed: int = 4) -> dict:
    """从任意画面退回选档页（光标停在存档列表上，没有选中任何存档）。
    录制开局之前用：这一段不进录像，所以可以快进；之后从选档页开始录，选存档、进章节都按正常速度。"""
    obs = c.call("activate", entities=False, tiles=False)["obs"]
    for _ in range(max_actions):
        scene = obs.get("scene")
        menu = obs.get("menu") or {}
        if scene == "Level":
            level = obs["level"]
            if obs.get("controllable") or level.get("paused"):
                obs = leave_level_by_pause_menu(c)  # 先返回地图
                continue
            if level.get("inCutscene") and not level.get("skippingCutscene"):
                skipped, obs, _, _ = skip_by_pause_menu(c)
                if skipped:
                    continue
            obs = step(c, ["120"], stop_on=["controllable"], speed=menu_speed)["obs"]
            continue
        if scene == "IntroVignette":
            skipped, obs, _, _ = skip_by_pause_menu(c)
            if not skipped:
                obs = step(c, ["30"])["obs"]
            continue
        if scene in ("LevelEnter", "AreaComplete"):
            obs = press(c, "C", settle=30)["obs"]
            continue
        if scene != "Overworld" or menu.get("next"):
            obs = step(c, ["10"], speed=menu_speed)["obs"]
            continue
        current = menu.get("current")
        if current == "OuiFileSelect":
            if not menu.get("slotSelected"):
                return obs
            obs = press(c, "B", settle=20)["obs"]
        elif current in ("OuiChapterSelect", "OuiChapterPanel"):
            obs = press(c, "B", settle=30)["obs"]  # 退回选章页 / 主菜单
        elif current in ("OuiTitleScreen", "OuiMainMenu"):
            obs = press(c, "C", settle=30)["obs"]  # 标题画面 → 主菜单 → "攀登"进选档页
        else:
            obs = step(c, ["10"], speed=menu_speed)["obs"]
    raise RuntimeError(f"未能在 {max_actions} 步内退回选档页：\n{summarize_obs(obs)}")


def handle_file_select(c: BridgeClient, obs: dict, save_name: str, *, save_slot: int | None = None) -> dict:
    menu = obs["menu"]
    targets = [s["index"] for s in menu["slots"] if s["exists"] and s["name"] == save_name
               and (save_slot is None or s.get('fileSlot',s['index']) == save_slot)]
    if len(targets) != 1:
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
    ap.add_argument("--destination", choices=('load', 'play', 'chapter-select'), default='load',
                    help="load（默认）原生载入并冻结；play 进入章节；chapter-select 明确退出关卡到选章地图")
    ap.add_argument("--save-slot", type=int, help="存档槽位；load 必填，避免选错同名存档")
    ap.add_argument("--chapter", type=int, help="章节编号：0 序章、1 被遗弃之城、2 旧址；默认进入选章页当前选中的章节")
    ap.add_argument("--leave-active", action="store_true", help="play/chapter-select 完成后保持接管；load 始终保持接管")
    args = ap.parse_args()
    if args.destination == 'load' and args.save_slot is None:
        ap.error('载入后冻结必须指定 --save-slot')
    if args.destination != 'play' and args.chapter is not None:
        ap.error('--chapter 只用于 --destination play')

    c = BridgeClient(port=args.port)
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from orchestrator.action_policy import controller_navigation
    try:
        with controller_navigation(c, slot=args.save_slot):
            if args.destination == 'load':
                obs = load_save_and_freeze(c, args.save, save_slot=args.save_slot)
            elif args.destination == 'chapter-select':
                obs = open_chapter_select(c, args.save, save_slot=args.save_slot)
            else:
                obs = enter_save(c, args.save, chapter=args.chapter, save_slot=args.save_slot)
            print(summarize_obs(obs))
            if args.destination != 'load' and not args.leave_active:
                c.call("deactivate")
    except MenuActionBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
