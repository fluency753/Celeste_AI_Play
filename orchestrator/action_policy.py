"""Small semantic policy shared by configuration, controls and bridge dispatch."""
from dataclasses import asdict, dataclass, replace
from contextlib import contextmanager

VERSION = 1
LABELS = {'restart_chapter': '重启章节', 'return_to_map': '返回地图', 'save_quit': '保存并退出'}
MENU_WHITELIST = '未通关时暂停菜单里只能选继续、重试、跳过剧情或取消'
BLOCK_STOPS = frozenset({'entry_blocked', 'chapter_reset_blocked'})


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

    def instructions(self):
        return '操作权限：' + '；'.join(f'{label}={"允许" if getattr(self, key) else "禁止"}' for key, label in LABELS.items()) + \
            '。禁止项由主控在实际操作前拦截' + ('' if all(self.snapshot().values()) else f'，{MENU_WHITELIST}（选项、辅助等其他菜单项也会被拦）') + \
            '。正常死亡和房间重试允许；已核验完成后的退出由主控授权。不得用重置代替路线求解，无法继续时用 stop_run 报告。'


def block_note(block):
    """桥接 result.block 的中文说明，附在 act 结果后面给智能体。"""
    action, rule = block.get('action'), block.get('rule_id') or ''
    if action == 'enter_map':
        what = '存档槽位' if rule == 'target.save_slot' else '地图'
        return (f'主控拦下了这次进入地图：{what}与任务不符（要求 {block.get("expected")}，实际 {block.get("actual")}），'
                '没有进入。请退回去选对存档和章节再进。')
    if action in LABELS:
        head = f'主控拦下了「{LABELS[action]}」：本次没有这项权限'
    else:
        head = f'主控拦下了这次菜单确认（{block.get("actual") or "未知选项"}）：{MENU_WHITELIST}'
    return (head + '。这一帧之后的按键没有执行，暂停菜单仍开着：先按 B 返回或选「继续」关掉菜单再接着玩。'
            '不要用退出重进代替找路；确实无法继续时用 stop_run 报告。')


def validate_update(data):
    if not isinstance(data, dict) or not data or set(data) - set(LABELS) or any(type(v) is not bool for v in data.values()):
        raise ValueError('权限更新须为已知操作名与布尔值组成的非空对象')


def permissions_for(cfg):
    data = getattr(cfg, 'action_permissions', None)
    return ActionPermissions.read(data) if data is not None else ActionPermissions.legacy(getattr(cfg, 'allow_chapter_reset', False))


def configure(cfg, permissions):
    cfg.action_permissions = permissions.snapshot()
    # Compatibility summary only; all policy consumers read canonical permissions.
    cfg.allow_chapter_reset = all(cfg.action_permissions.values())


def permissions_from_state(state, preference=None):
    data = preference if preference is not None else state.get('action_permissions')
    return ActionPermissions.read(data) if data is not None else ActionPermissions.legacy(state.get('allow_chapter_reset', False))


def step_options(cfg, *, sid=None, slot=None, departure=False):
    permissions = permissions_for(cfg).snapshot()
    target = {'sid': sid, 'slot': slot}
    return {'action_policy': {'version': VERSION, 'permissions': permissions,
                              'entry_target': target, 'departure': 'current_level' if departure else 'none'},
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
