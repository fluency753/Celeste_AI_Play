"""通过原生菜单退出游戏；不关窗口、不调用退出函数、不终止进程。"""
from collections import deque

from tools.menu_navigation import checked_step

QUIET = dict(image=False, entities=False, tiles=False)


def _exit_direction(menu):
    buttons = {b['index']: b for b in menu.get('buttons', [])}
    targets = [i for i, b in buttons.items() if b.get('id') == 'menu_exit']
    if len(targets) != 1 or menu.get('selection') not in buttons:
        raise RuntimeError('无法唯一识别主菜单退出按钮或当前选择；未确认任何按钮，请更新 Bridge')
    target = targets[0]
    queue = deque([(menu['selection'], None)])
    seen = set()
    while queue:
        index, first = queue.popleft()
        if index == target:
            return first
        if index in seen:
            continue
        seen.add(index)
        for key in ('U', 'D', 'L', 'R'):
            neighbor = buttons[index].get(key)
            if neighbor in buttons and neighbor not in seen:
                queue.append((neighbor, first or key))
    raise RuntimeError('主菜单退出按钮不可达；未确认任何按钮')


def exit_game_by_menu(client, process, *, max_actions=120, exit_timeout=20):
    """process 是发送确认前已绑定的原游戏进程；仅该进程正常退出才成功。

    调用方必须先保存退出关卡并停止录制。最后确认与方向分开，断线不重试确认。
    """
    hello = client.call('hello')
    if hello.get('process_id') != process.pid:
        raise RuntimeError('桥接进程与维护目标不一致；未执行退出')
    clock = hello.get('clock') or {}
    plugin = clock.get('plugin') or {}
    if (hello.get('record_audio_active') or clock.get('lockstep') or plugin.get('recording')
            or plugin.get('state') in {'recording', 'finishing'}):
        raise RuntimeError('录制资源尚未释放；未执行退出游戏')
    obs = client.call('observe', **QUIET)['obs']
    for _ in range(max_actions):
        if obs.get('scene') != 'Overworld':
            raise RuntimeError(f"当前不是已保存退出后的菜单（{obs.get('scene')}）；未退出游戏")
        menu = obs.get('menu') or {}
        current = menu.get('current')
        if menu.get('next') or obs.get('saving'):
            inputs = ['6']
        elif current in {'OuiChapterPanel', 'OuiChapterSelect', 'OuiFileSelect'}:
            inputs = ['1,B', '12']
        elif current == 'OuiTitleScreen':
            inputs = ['1,C', '12']
        elif current == 'OuiMainMenu':
            if not menu.get('focused'):
                inputs = ['6']
            else:
                direction = _exit_direction(menu)
                selected = next(b for b in menu['buttons'] if b['index'] == menu['selection'])
                if not selected.get('ready'):
                    inputs = ['2']
                elif direction:
                    inputs = [f'1,{direction}', '6']
                else:
                    # A final EOF is expected on some timings, but only an exit
                    # of the same held process proves success. Never reconnect.
                    try:
                        checked_step(client, ['1,C'], speed=1, allow_reconnect=False, **QUIET)
                    except (ConnectionError, TimeoutError, OSError):
                        pass
                    code = process.wait(exit_timeout)
                    if code != 0:
                        raise RuntimeError(f'游戏进程退出码为 {code}，不能确认正常退出')
                    return {'exited': True, 'process_id': process.pid, 'exit_code': code, 'method': 'menu_exit'}
        else:
            raise RuntimeError(f'不支持从 {current} 自动退出；保留菜单，未确认任何按钮')
        obs = checked_step(client, inputs, speed=1, **QUIET)['obs']
    raise RuntimeError('退出菜单导航超时；未重试退出确认，也未终止游戏进程')
