"""Small semantic policy shared by configuration, controls and bridge dispatch."""
from dataclasses import asdict, dataclass, replace
from contextlib import contextmanager

VERSION = 1
LABELS = {'restart_chapter': '重启章节', 'return_to_map': '返回地图', 'save_quit': '保存并退出'}
MENU_WHITELIST = '权限受限时，关卡／序章菜单只能选继续、重试、跳过剧情或取消，或执行已明确获准的原生菜单动作；未知按钮不放行'
SETTINGS_MENUS = '暂停菜单里的「选项」「Mod 选项」（以及辅助、变体）始终由主控拦截：它们会改按键绑定、抓取方式和本系统依赖的 Mod 设置'
FULL_CONTROL = ('操作权限：速通模式开放全部游戏操作。可以使用全部按键（含 P 暂停、B 返回、C 确认、N 日志），'
                '以及暂停菜单里的继续、重试、跳过剧情、重启章节、返回地图、保存并退出；'
                '整章速通还可以用房间直达与练习存读档（开启时）练习。' + SETTINGS_MENUS + '。'
                '整章速通的成绩按一次从章首开始、原生计时有效的通关计算（按检查点分段时以主控的分段核验为准）：'
                '房间直达、从检查点进入和读档会让本趟计时变灰，要取得成绩就用「重启章节」回章首重新跑。'
                '用一切可用手段争取最短章节计时；无法继续时用 stop_run 报告。')
BLOCK_STOPS = frozenset({'entry_blocked', 'chapter_reset_blocked', 'unsupported_scene_blocked'})


@dataclass(frozen=True)
class ActionPermissions:
    restart_chapter: bool = False
    return_to_map: bool = False
    save_quit: bool = False

    @classmethod
    def legacy(cls, value=False):
        if type(value) is not bool:
            raise ValueError('旧章节重置开关须为布尔值')
        return cls(value, value, value)

    @classmethod
    def read(cls, data):
        if not isinstance(data, dict) or set(data) != set(LABELS):
            raise ValueError('操作权限须完整包含 restart_chapter、return_to_map、save_quit')
        validate_update(data)
        return cls(**data)

    def update(self, data):
        validate_update(data)
        return replace(self, **data)

    def snapshot(self):
        return asdict(self)

    def instructions(self, *, experiments=False, full=False):
        if full:
            return FULL_CONTROL
        return '操作权限：' + '；'.join(f'{label}={"允许" if getattr(self, key) else "禁止"}' for key, label in LABELS.items()) + \
            '。禁止项由主控在实际操作前拦截' + ('' if all(self.snapshot().values()) else f'，{MENU_WHITELIST}（选项、辅助等其他菜单项也会被拦）') + \
            '。正常死亡和房间重试允许；已核验完成后的退出由主控授权。' + \
            ('可在获准范围与权限内重启或往返进行实验。' if experiments else '不得用重置代替路线求解，') + '无法继续时用 stop_run 报告。'


def block_note(block, *, detour=False):
    """桥接 result.block 的中文说明，附在 act 结果后面给智能体。detour：本次用的是计划外地图的官方地图集合门禁。"""
    action, rule = block.get('action'), block.get('rule_id') or ''
    if action == 'enter_pico8':
        return ('主控拦下了小游戏入口：这是内置的 PICO-8 小游戏，暂不支持。未进入小游戏，'
                '本次输入在触发入口的这一帧停止，后续按键没有执行；请继续本体关卡。')
    if action == 'enter_map' and detour and rule in {'target.maps', 'target.new_save'}:
        actual = block.get('actual') or {}
        what = ('新建存档' if rule == 'target.new_save' else
                f'进入 {actual.get("sid")} {actual.get("side")}面：计划外地图只限蔚蓝官方章节')
        return (f'主控拦下了{what}。本次没有进入地图或创建存档，触发入口后的剩余输入未执行；'
                '请在原存档的选章界面选择官方章节已解锁的面与检查点。')
    if action == 'enter_map':
        if rule == 'target.new_save':
            return ('主控拦下了新建存档：已到访地图自由探索只允许使用原有存档。'
                    '本次没有创建或切换存档，后续输入未执行；请返回并选择已获许可的原存档。')
        if rule == 'target.maps':
            from .free_roam import describe_maps, validate_maps
            expected = describe_maps(validate_maps(block.get('expected')))
            actual = block.get('actual') or {}
            return (f'主控拦下了这次进入地图：{actual.get("sid")} {actual.get("side")}面不在许可清单内。'
                    f'允许反复进入：{expected}。本次没有进入地图，触发入口后的剩余输入未执行；'
                    '请返回选章界面选择许可地图及已解锁的检查点，不要重试未授权入口。')
        what = '存档槽位' if rule == 'target.save_slot' else '地图'
        return (f'主控拦下了这次进入地图：{what}与任务不符（要求 {block.get("expected")}，实际 {block.get("actual")}），'
                '没有进入。请退回去选对存档和章节再进。')
    if action == 'settings_menu':
        return (f'主控拦下了「{block.get("actual") or "选项"}」：{SETTINGS_MENUS}，不对智能体开放。'
                '这一帧之后的按键没有执行，暂停菜单仍开着：先按 B 返回或选「继续」关掉菜单再接着玩。')
    if action in LABELS:
        head = f'主控拦下了「{LABELS[action]}」：本次没有这项权限'
    else:
        head = f'主控拦下了这次菜单确认（{block.get("actual") or "未知选项"}）：{MENU_WHITELIST}'
    return (head + '。这一帧之后的按键没有执行，暂停菜单仍开着：先按 B 返回或选「继续」关掉菜单再接着玩。'
            '不要用退出重进代替找路；确实无法继续时用 stop_run 报告。')


def validate_update(data):
    if not isinstance(data, dict) or not data or set(data) - set(LABELS) or any(type(v) is not bool for v in data.values()):
        raise ValueError('权限更新须为已知操作名与布尔值组成的非空对象')


def full_control(cfg) -> bool:
    """速通模式：开放全部游戏操作（三项权限固定允许、全部按键），只拦设置类子菜单。由 run.py 按模式设定。"""
    return bool(getattr(cfg, 'full_control', False))


def permissions_for(cfg):
    data = getattr(cfg, 'action_permissions', None)
    return ActionPermissions.read(data) if data is not None else ActionPermissions.legacy(getattr(cfg, 'allow_chapter_reset', False))


def configure(cfg, permissions):
    if full_control(cfg):
        permissions = ActionPermissions(True, True, True)   # 速通模式固定全部允许，任何来源的设置都不改变
    cfg.action_permissions = permissions.snapshot()
    # Compatibility summary only; all policy consumers read canonical permissions.
    cfg.allow_chapter_reset = all(cfg.action_permissions.values())


def permissions_from_state(state, preference=None):
    data = preference if preference is not None else state.get('action_permissions')
    return ActionPermissions.read(data) if data is not None else ActionPermissions.legacy(state.get('allow_chapter_reset', False))


def step_options(cfg, *, sid=None, slot=None, departure=False, maps=None):
    permissions = permissions_for(cfg).snapshot()
    target = {'sid': sid, 'slot': slot}
    if maps is not None:
        from .free_roam import validate_maps
        if sid is not None:
            raise ValueError('地图集合不能与单地图目标混用')
        target['maps'] = validate_maps(maps)
    return {'action_policy': {'version': VERSION, 'permissions': permissions,
                              'entry_target': target, 'departure': 'current_level' if departure else 'none',
                              # 选项、Mod 选项、辅助、变体：任何权限下都不开放（桥接 1.18.0 起识别）
                              'settings_menus': False},
            # Older menu clients/tests can still inspect these; the new bridge
            # uses action_policy exclusively when it is present.
            'entry_guard': target, 'allow_chapter_reset': all(permissions.values()), 'allow_map_exit': bool(departure)}


@contextmanager
def controller_navigation(client, cfg=None, *, sid=None, slot=None):
    """Owner-requested startup/restore only; never wrap agent-dispatched tools.

    Departure remains bound to the current Level in each bridge job, and target
    checks remain active. Restore the connection's policy even when a macro fails.
    """
    previous = getattr(client, 'step_options', None)
    client.step_options = lambda: step_options(cfg, sid=sid, slot=slot, departure=True)
    try:
        yield
    finally:
        client.step_options = previous


def require_capability(hello):
    if type(hello.get('action_policy')) is not int or hello['action_policy'] != VERSION:
        raise ValueError('需要支持 action_policy=1 的新版桥接（含实际入场核验和分关卡授权）；未发送游戏输入。')


def require_settings_guard(cfg, hello):
    """三项权限全开时，桥接旧版会放行暂停菜单里的全部选项；要有 1.18.0 的设置菜单拦截才能开跑。"""
    if all(permissions_for(cfg).snapshot().values()) and hello.get('settings_menu_guard') is not True:
        raise ValueError('三项操作权限全部允许时（速通模式固定如此），需要 CelesteAIBridge 1.18.0 或更新版本的设置菜单拦截'
                         '（拦住选项、Mod 选项、辅助、变体）；请更新桥接。未发送游戏输入。')


def require_pico8_guard(hello):
    if hello.get('pico8_entry_guard') is not True:
        raise ValueError('需要 CelesteAIBridge 1.4.0 或更新版本且 PICO-8 入口拦截已加载；未发送游戏输入。')
