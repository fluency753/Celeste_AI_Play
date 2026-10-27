"""Small semantic policy shared by configuration, controls and bridge dispatch."""
from dataclasses import asdict, dataclass, replace

VERSION = 1
LABELS = {'restart_chapter': '重启章节', 'return_to_map': '返回地图', 'save_quit': '保存并退出'}
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
            '。禁止项由主控在实际操作前拦截。正常死亡和房间重试允许；已核验完成后的退出由主控授权。不得用重置代替路线求解，无法继续时用 stop_run 报告。'


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


def require_capability(hello):
    if type(hello.get('action_policy')) is not int or hello['action_policy'] != VERSION:
        raise ValueError('需要支持 action_policy=1 的新版桥接（含实际入场核验和分关卡授权）；未发送游戏输入。')
