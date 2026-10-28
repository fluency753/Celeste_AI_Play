"""Fail before recording or selecting a save when the bridge is too old."""
import re


def validate_bridge(hello):
    from .bridge_reload import hold_message
    if held := hold_message(hello):
        raise RuntimeError(held)
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(hello.get('bridge', '')))
    if (not match or tuple(map(int, match.groups())) < (1, 0, 22)
            or not all(hello.get(key) for key in ('create_save', 'dialog_choices', 'story_text', 'entry_guard'))):
        raise RuntimeError('需要 CelesteAIBridge 1.0.22 或更新版本，包含逐帧地图进入核验；请更新桥接 Mod。')


def preflight_bridge(port):
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=port)
    try:
        hello = client.call('hello')
        validate_bridge(hello)
        from .action_policy import require_capability
        try:
            require_capability(hello)
        except ValueError as exc:
            raise RuntimeError(str(exc) + '未开始录制或进档。') from exc
        return hello
    finally:
        client.close()
