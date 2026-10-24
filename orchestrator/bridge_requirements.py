"""Fail before recording or selecting a save when the bridge is too old."""
import re


def validate_bridge(hello):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(hello.get('bridge', '')))
    if (not match or tuple(map(int, match.groups())) < (0, 12, 4)
            or not all(hello.get(key) for key in ('create_save', 'dialog_choices', 'story_text'))):
        raise RuntimeError('需要 CelesteAIBridge 0.12.4 或更新版本，包含存档创建、剧情文字和对话选项接口；请更新桥接 Mod。')


def preflight_bridge(port):
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=port)
    try:
        hello = client.call('hello')
        validate_bridge(hello)
        return hello
    finally:
        client.close()
