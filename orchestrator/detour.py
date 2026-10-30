"""计划外地图（绕行）：章节或顺序范围下，agent 可以自行回到地图去别的已解锁官方地图，再回到计划章节。

这里只放开关、许可集合与说明文字；绕行状态由 GameSession 按实际观测维护，入场门禁由桥接的地图集合
（map_entry_set，桥接 1.10.0）执行。计划本身不变：计划外地图的通关不完成计划、不计入分段成绩与总计时。
"""
from .config import CHAPTER_SIDS, chapter_name

LABEL = '允许去计划外的已解锁地图'
# 序章、终章与告别只有 A 面。游戏的选章界面本来就只提供已解锁的章节、面与检查点，这里只把入场范围
# 限定在官方关卡集（不进入其他 MOD 关卡集），未解锁的内容由游戏自己拦着。
SINGLE_SIDE = {0, 8, 10}
OFFICIAL_MAPS = sorted(({'sid': CHAPTER_SIDS[i], 'side': side} for i in CHAPTER_SIDS
                        for side in (('A',) if i in SINGLE_SIDE else ('A', 'B', 'C'))),
                       key=lambda item: (item['sid'], item['side']))


def enabled(cfg) -> bool:
    """开关打开且不是已到访地图自由探索（那里有自己的地图许可，不使用计划）。"""
    return bool(getattr(cfg, 'allow_detour', False)) and getattr(cfg, 'play_scope', 'chapter') != 'visited'


def map_label(sid, side) -> str:
    return f'{chapter_name(sid)} {side or "A"}面'


def instructions(cfg) -> str:
    """提示词里"计划外地图"一节（开了才加）。"""
    text = ('本次允许你自行离开计划，去别的已解锁地图：用 act 按 P 打开暂停菜单选「返回地图」，'
            '再在原生选章界面自己选择其他已解锁的官方章节、面（A/B/C）和检查点；菜单只提供已解锁内容，主控不代选、不代按。'
            '计划章节仍是本次任务：计划外地图通关不算完成计划，不计入分段成绩与总计时；办完后经暂停菜单「返回地图」或通关结算'
            '回到选章界面，再进入计划章节（开头或已解锁的检查点；计划章节的计时按每次进入累计，重新进入不清零），'
            '计划章节通关后本次任务才结束。')
    if getattr(cfg, 'cutscene_mode', 'auto') != 'agent':
        text += ('auto 剧情模式下剧情与结算仍由主控自动跳过和确认，计划外地图结算后停在选章地图交给你；'
                 '本次 auto 模式也可用 B 返回、P 暂停、N 日志。')
    return text


def map_rule(cfg) -> str:
    """exit_level 与"返回地图"说明里关于换图的一句：关时由用户在控制台调整范围，开时可自行绕行。"""
    if enabled(cfg):
        return '本次允许自行绕行到其他已解锁的官方地图，计划不变，计划章节通关才算完成任务'
    return '换到计划外的章节须由用户在控制台调整范围'


def notice(cfg, allowed: bool, permissions_note: str = '') -> str:
    """运行中切换开关时通知 agent 的事实部分。工具说明在会话开始时就定了，这里写明现在实际生效的规则。"""
    if allowed:
        return ('用户开启了计划外地图：' + instructions(cfg)
                + '工具说明里关于返回地图、换图与按键的旧说法以这里为准。' + permissions_note)
    keys = '离开前 B、P、N 仍可用，' if getattr(cfg, 'cutscene_mode', 'auto') != 'agent' else ''
    return ('用户关闭了计划外地图：之后只能进入当前计划的地图，工具说明里允许绕行的说法作废。正在计划外地图时可以继续玩完，'
            f'也可以用 act 按 P 在暂停菜单选「返回地图」（{keys}返回地图在离开它之前始终可用），再进入计划章节。'
            '计划与已有成绩不变。' + permissions_note)


def act_note(cfg) -> str:
    """act 工具说明末尾的一句（开了才加；auto 剧情模式本来没有 B、P、N）。"""
    if enabled(cfg) and getattr(cfg, 'cutscene_mode', 'auto') != 'agent':
        return '\n本次开了计划外地图：auto 剧情模式也可用 B 返回、P 暂停、N 日志，自己操作暂停菜单与选章界面。'
    return ''


SCOPE_FROM = '只玩已配置的章节或分段；'
SCOPE_TO = '只以已配置的章节或分段为任务，计划外地图只作绕行（见“计划外地图”一节）；'
