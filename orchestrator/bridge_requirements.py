"""Fail before recording or selecting a save when the bridge is too old."""
import re


def validate_bridge(hello):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(hello.get('bridge', '')))
    if (not match or tuple(map(int, match.groups())) < (1, 0, 22)
            or not all(hello.get(key) for key in ('create_save', 'dialog_choices', 'story_text', 'entry_guard'))):
        raise RuntimeError('需要 CelesteAIBridge 1.0.22 或更新版本，包含逐帧地图进入核验；请更新桥接 Mod。')


def preflight_bridge(port, *, allow_chapter_reset=False):
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=port)
    try:
        hello = client.call('hello')
        validate_bridge(hello)
        if not allow_chapter_reset and hello.get('chapter_reset_guard') is not True:
            raise RuntimeError('默认禁止章节重置需要 CelesteAIBridge 1.0.28 的逐帧菜单门禁；请先更新桥接 Mod，未开始录制或进档。')
        return hello
    finally:
        client.close()
