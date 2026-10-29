"""Validated map identities for a save-bound exploration permission snapshot."""
from .config import chapter_name


def validate_maps(value):
    if not isinstance(value, list):
        raise ValueError('已到访地图清单缺失，请更新桥接后重新建立许可')
    result = set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'sid', 'side'}
                or not isinstance(item['sid'], str) or not item['sid'].strip()
                or item['side'] not in ('A', 'B', 'C')):
            raise ValueError('地图许可须包含有效 SID 与 A/B/C 面')
        result.add((item['sid'], item['side']))
    return [{'sid': sid, 'side': side} for sid, side in sorted(result)]


def describe_maps(maps):
    return '、'.join(f'{chapter_name(m["sid"])} {m["side"]}面' for m in maps) or '无（没有可证明已进入的地图）'


def instructions(cfg):
    maps = getattr(cfg, 'visited_maps', None)
    return ('自由探索：允许反复进出的地图：' + (describe_maps(maps) if maps is not None else '连接后由存档记录核定')
            + '。可自行通过原生菜单选择已解锁的检查点；返回地图、重启和保存退出仍按各项权限执行。'
            + '结算、选章由你操作，主控不代按；'
            + ('通关后继续探索。' if cfg.completion_policy == 'continue' else '完成一张地图后停止。')
            + '时长目标独立。')
