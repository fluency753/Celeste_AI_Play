"""Fail before recording or selecting a save when the bridge is too old."""
import re


def validate_bridge(hello):
    from .bridge_reload import hold_message
    if held := hold_message(hello):
        raise RuntimeError(held)
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(hello.get('bridge', '')))
    if (not match or tuple(map(int, match.groups())) < (1, 4, 0)
            or not all(hello.get(key) for key in ('create_save', 'dialog_choices', 'story_text', 'entry_guard'))):
        raise RuntimeError('需要 CelesteAIBridge 1.4.0 或更新版本，包含 PICO-8 入口拦截和逐帧地图进入核验；'
                           '请更新桥接 Mod。未开始录制或进档。')
    from .action_policy import require_pico8_guard
    try:
        require_pico8_guard(hello)
    except ValueError as exc:
        raise RuntimeError(str(exc) + '未开始录制或进档。') from exc
    # AI 专用副本模式：只接管副本里的游戏（个人的《蔚蓝》同时开着、也装了 AI 模组时可能先占了端口），并改好窗口标题
    from .game_instance import check_identity
    check_identity(hello)


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
        # Steam 在游戏启动后重启过或已退出：游戏调用 Steam 时可能被直接结束，先请用户重启游戏。
        from .game_watch import require_live_steam
        require_live_steam(hello, log=lambda line: print(line, flush=True))
        return hello
    finally:
        client.close()
