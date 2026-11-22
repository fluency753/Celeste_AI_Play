"""Fail before recording or selecting a save when the bridge is too old."""
import re


def require_summit_epilogue(cfg, hello):
    from .segments import Piece
    plan = getattr(cfg, 'plan', None) or ()
    if not any((left, right) == (Piece(7), Piece(8)) for left, right in zip(plan, plan[1:])):
        return
    version = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(hello.get('bridge', '')))
    if version is None or tuple(map(int, version.groups())) < (1, 16, 1):
        raise RuntimeError('7A 接续尾声需要 Bridge 1.16.1 或更新版本。未开始录制或发送游戏输入。')


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


def speedrun_tool_hotkey_notice(hello):
    """Speedrun Tool 的快捷键自己读真实键盘与手柄，不看桥接的合成输入；桥接 1.22.1 起在接管期间停用它们。
    没装 Speedrun Tool 或已停用时不提醒；旧桥接或没挂上时提醒一句（不拦开跑）。"""
    state = hello.get('speedrun_tool_hotkeys')
    if state in ('guarded', 'absent'):
        return None
    if state is None:
        return ('[run] 桥接早于 1.22.1：装了 Speedrun Tool 时，接管期间它的快捷键仍会被手柄（包括虚拟手柄）'
                '或游戏窗口在前台时的键盘触发，读档、传送房间、刷出望远镜等会改动受控的关卡。请更新桥接 Mod。')
    return ('[run] 装了 Speedrun Tool，但桥接没能在接管期间停用它的快捷键（Speedrun Tool 版本不认得）：手柄或键盘'
            '仍可能触发读档、传送房间、刷出望远镜等，改动受控的关卡。运行期间请别碰手柄与游戏窗口，或在 Speedrun Tool 设置里关掉快捷键。')


def preflight_bridge(port):
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=port)
    try:
        hello = client.call('hello')
        validate_bridge(hello)
        if not getattr(client, 'authenticated', False):
            # 常驻核心 1.1.0 之前，本机端口不核对来人：能连就能操作游戏。不拦开跑，只提醒更新。
            print('[run] 游戏里的常驻核心还没有连接口令保护（早于 1.1.0）：本机上的其他程序、甚至浏览器里的网页'
                  '都可能连上桥接端口操作游戏。请更新桥接 Mod 与常驻核心（需要关闭游戏后部署）。', flush=True)
        from .action_policy import require_capability
        try:
            require_capability(hello)
        except ValueError as exc:
            raise RuntimeError(str(exc) + '未开始录制或进档。') from exc
        # Steam 在游戏启动后重启过或已退出：游戏调用 Steam 时可能被直接结束，先请用户重启游戏。
        from .game_watch import require_live_steam
        require_live_steam(hello, log=lambda line: print(line, flush=True))
        if notice := speedrun_tool_hotkey_notice(hello):
            print(notice, flush=True)
        return hello
    finally:
        client.close()
