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


def instructions(cfg, *, travel: bool | None = None) -> str:
    """提示词里"计划外地图"一节（开了才加）。travel：本次会话注册了 travel 工具（开关在会话开始时打开着；
    运行中才打开时工具列表已经定了，仍按原来的自己操作菜单写）。"""
    if travel is None:
        travel = enabled(cfg)
    if travel:
        text = ('本次允许你自行离开计划，去别的已解锁地图：用 travel 工具请主控代为换图（给出章节编号与检查点），'
                '主控替你按玩家的那几步：关卡里用暂停菜单返回地图，在选章地图上选章节、打开面板、选 A 面和检查点、进入，停在第一帧可操作时交回；只限存档里已解锁章节的 A 面'
                '与已到过的检查点。不要自己用暂停菜单返回地图或在选章界面操作：地图上不冻结，你在那里思考的时间都会录进录像。'
                '在计划外地图办完事就用 travel 离开，不必通关它。'
                '计划章节仍是本次任务：计划外地图通关不算完成计划，不计入分段成绩与总计时；办完后用 travel 回到计划章节'
                '（开头或已到过的检查点；计划章节的计时按每次进入累计，重新进入不清零），计划章节通关后本次任务才结束。')
        if getattr(cfg, 'cutscene_mode', 'auto') != 'agent':
            text += ('auto 剧情模式下剧情与结算仍由主控自动跳过和确认；计划外地图若通关，结算后停在选章地图交给你，'
                     '这时也用 travel 继续。')
        return text
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


def notice(cfg, allowed: bool, permissions_note: str = '', *, travel: bool = False) -> str:
    """运行中切换开关时通知 agent 的事实部分。工具说明在会话开始时就定了，这里写明现在实际生效的规则。
    travel：本次会话注册了 travel 工具（会话开始时开关就开着）。"""
    if allowed:
        return ('用户开启了计划外地图：' + instructions(cfg, travel=travel)
                + '工具说明里关于返回地图、换图与按键的旧说法以这里为准。' + permissions_note)
    if travel:
        return ('用户关闭了计划外地图：之后只能进入当前计划的地图，工具说明里允许绕行的说法作废。正在计划外地图时可以继续玩完，'
                '也可以用 travel 回到计划章节（travel 现在只接受计划章节）。计划与已有成绩不变。' + permissions_note)
    keys = '离开前 B、P、N 仍可用，' if getattr(cfg, 'cutscene_mode', 'auto') != 'agent' else ''
    return ('用户关闭了计划外地图：之后只能进入当前计划的地图，工具说明里允许绕行的说法作废。正在计划外地图时可以继续玩完，'
            f'也可以用 act 按 P 在暂停菜单选「返回地图」（{keys}返回地图在离开它之前始终可用），再进入计划章节。'
            '计划与已有成绩不变。' + permissions_note)


def act_note(cfg) -> str:
    """act 工具说明末尾的一句（开了才加；auto 剧情模式本来没有 B、P、N）。"""
    if enabled(cfg):
        return ('\n本次开了计划外地图：换图用 travel 请主控代办，不要自己用暂停菜单返回地图或操作选章界面'
                + ('（auto 剧情模式下 B、P、N 只在主控交回的结算或菜单界面里用）。' if getattr(cfg, 'cutscene_mode', 'auto') != 'agent' else '。'))
    return ''


SCOPE_FROM = '只玩已配置的章节或分段；'
SCOPE_TO = '只以已配置的章节或分段为任务，计划外地图只作绕行（见“计划外地图”一节）；'
