"""HUD session lifecycle. Preparation runs independently; game entry always needs a separate click."""
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .background import launch
from .chat import can_wake
from .config import REPO_ROOT, RunConfig
from .control import RunControl
from .conversations import read_json, release_chat, start_chat, prepare_target

_lock = threading.RLock()
_monitors = set()
BUSY = {"preparing", "connecting", "starting", "naming"}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def require_idle(current):
    control = RunControl(current).snapshot()
    if not control.get('connected') and read_json(current/'run_state.json').get('session_kind') in {'idle','standby'}:
        return
    if not can_wake(current) or (control.get('connected') and not control.get('chat_only')):
        raise ValueError('请先停止试玩并保存录像，再管理会话')
    if control.get('connected') and control.get('phase') == 'chatting':
        raise ValueError('agent 正在回复，请等回复结束')


def operation_status(anchor):
    if anchor is None:
        return {}
    operation = read_json(anchor / 'session_operation.json')
    if not operation:
        pending = read_json(REPO_ROOT / 'local/hud-session-operation.json')
        if pending.get('status') in BUSY and pending.get('agent') == anchor.parent.name:
            operation = pending
    if operation.get('status') in BUSY:
        _monitor(anchor, operation)
    return {k: operation[k] for k in ('id', 'kind', 'status', 'error', 'save_name') if k in operation}


def store_operation(anchor, operation, *, new=False):
    with _lock:
        write_json(anchor / 'session_operation.json', operation)
        pointer = REPO_ROOT / 'local/hud-session-operation.json'
        previous = read_json(pointer)
        if new or previous.get('id') in {None, operation['id']}:
            write_json(pointer, operation)


def _monitor(anchor, operation):
    with _lock:
        if operation['id'] in _monitors:
            return
        _monitors.add(operation['id'])
    threading.Thread(target=_finish, args=(anchor, operation), daemon=True, name='hud-session').start()


def _finish(anchor, operation):
    previous = Path(operation['previous'])
    try:
        if operation['status'] == 'naming':
            name_host = Path(operation['host'])
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                status = read_json(name_host / 'status.json')
                if status.get('state') in {'exited', 'failed'}:
                    if status.get('state') == 'failed' or status.get('exit_code') != 0:
                        raise RuntimeError('存档取名失败：' + (name_host / 'output.log').read_text(encoding='utf-8',errors='replace')[-800:])
                    break
                time.sleep(.25)
            else:
                raise RuntimeError('存档取名超时，没有开始试玩')
            from .saves import validate_selection
            from .save_naming import parse_name
            result = read_json(previous / 'save_name_result.json')
            if result.get('request_id') != operation['id']:
                raise ValueError('没有收到本次会话的取名结果，没有开始试玩')
            options = operation['play_options']
            options['save'] = validate_selection(parse_name(result.get('name')), True, options['chapter'])
            host, _ = _launch_play(previous, options)
            operation.update(host=str(host), status='starting', save_name=options['save'])
            store_operation(anchor, operation)
        host = Path(operation['host'])
        deadline = time.monotonic() + (65 * 60 if operation['kind'] == 'new' else 60)
        while time.monotonic() < deadline:
            status = read_json(host / 'status.json')
            if operation['kind'] == 'play':
                latest = (REPO_ROOT / 'runs/latest.txt')
                path = latest.read_text(encoding='utf-8').strip() if latest.exists() else ''
                if path and path != operation['latest_before']:
                    control = RunControl(Path(path)).snapshot()
                    if control.get('connected') and not control.get('chat_only'):
                        operation['status'] = 'ready'
                        return
            if status.get('state') in {'exited', 'failed'}:
                if status.get('exit_code', 1) != 0 or status.get('state') == 'failed' or operation['kind'] == 'play':
                    raise RuntimeError('后台启动失败：' + (host / 'output.log').read_text(encoding='utf-8', errors='replace')[-1200:])
                break
            time.sleep(.25)
        else:
            raise RuntimeError('后台操作超时，请检查运行日志')
        directory = Path(operation['directory'])
        prepared = read_json(directory / 'prepare_summary.json')
        if not prepared.get('kb_study', {}).get('complete') or prepared.get('quick_check') or not prepared.get('session_id'):
            raise ValueError('准备未完成，未切换原会话')
        metrics = prepared['metrics']
        state = {'engine': 'codex', 'provider': 'codex', 'agent': operation['agent'], 'save': '',
                 'model': metrics['model'], 'effort': metrics['effort'], 'session_id': prepared['session_id'],
                 'knowledge_profile': 'explore', 'session_kind': 'ready', 'session_name': operation['name'],
                 'prepared_dir': str(directory), 'runtime': 'container', 'sim': False, 'sim_assist': False,
                 'max_minutes': 240}
        write_json(directory / 'run_state.json', state)
        RunControl(directory).set_fast(operation['fast'])
        RunControl(directory).set_cutscene_mode(operation.get('cutscene_mode','auto'))
        operation['status'] = 'connecting'
        store_operation(anchor, operation)
        target = prepare_target(anchor, {'id': prepared['session_id'], 'directory': str(directory)})
        # Keep token data from preparation, but never copy old conversation text into this new session.
        start_chat(target)
        write_json(anchor / 'hud_session.json', {'relative': str(target.relative_to(anchor))})
        operation['status'] = 'ready'
    except Exception as exc:
        operation.update(status='error', error=str(exc))
        if operation['kind'] == 'play':
            latest = REPO_ROOT / 'runs/latest.txt'
            path = latest.read_text(encoding='utf-8').strip() if latest.exists() else ''
            if path and path != operation['latest_before'] and read_json(Path(path) / 'run_state.json').get('agent') == operation['agent']:
                latest.write_text(operation['latest_before'], encoding='utf-8')
        if read_json(previous/'run_state.json').get('session_id'):
            try:
                start_chat(previous)
            except Exception as restore:
                operation['error'] += f'；原会话可稍后手动唤起：{restore}'
    finally:
        store_operation(anchor, operation)
        with _lock:
            _monitors.discard(operation['id'])


def create_session(anchor, current, options):
    require_idle(current)
    if operation_status(anchor).get('status') in BUSY:
        raise ValueError('已有会话操作正在进行')
    if not isinstance(options, dict) or set(options) - {'name', 'model', 'effort'}:
        raise ValueError('新会话参数无效')
    name, model, effort = options.get('name', ''), options.get('model', ''), options.get('effort', 'high')
    if not all(isinstance(value, str) for value in (name, model, effort)):
        raise ValueError('名称、模型和推理强度须为文字')
    name, model = name.strip(), model.strip()
    if len(name) > 60 or not re.fullmatch(r'[A-Za-z0-9._-]{0,100}', model) or effort not in {'low','medium','high','xhigh','max'}:
        raise ValueError('名称最多60字，模型须为模型标识，推理强度须为有效档位')
    from .model_catalog import resolve_selection
    model, effort = resolve_selection(model, effort)
    ident = uuid.uuid4().hex[:10]
    agent = 'gpt-hud-' + ident
    stamp = time.strftime('%Y%m%d-%H%M%S')
    directory = REPO_ROOT / 'runs' / agent / stamp
    fast = RunControl(current).fast_preference()
    operation = {'id': ident, 'kind': 'new', 'status': 'preparing', 'previous': str(current),
                 'directory': str(directory), 'agent': agent, 'name': name or '新会话 ' + time.strftime('%H:%M'),
                 'fast': bool(fast)}
    operation['cutscene_mode']=RunControl(current).cutscene_preference() or read_json(current/'run_state.json').get('cutscene_mode','auto')
    release_chat(current)
    try:
        host, _ = launch(['--agent', agent, '--knowledge', 'explore', '--model', model, '--effort', effort,
                          '--output', str(directory), '--hud-managed', '--fast', 'on' if fast else 'off'], service='prepare')
    except Exception:
        start_chat(current)
        raise
    operation['host'] = str(host)
    store_operation(anchor, operation, new=True)
    _monitor(anchor, operation)
    return operation_status(anchor)


def disconnect(anchor, current):
    require_idle(current)
    if operation_status(anchor).get('status') in BUSY:
        raise ValueError('请等待会话操作完成')
    release_chat(current)


def play_session(anchor, current, options):
    require_idle(current)
    if operation_status(anchor).get('status') in BUSY:
        raise ValueError('已有会话操作正在进行')
    state = read_json(current / 'run_state.json')
    if state.get('session_kind') != 'ready':
        raise ValueError('请先新建并准备会话；旧试玩请使用续跑入口')
    if (not isinstance(options, dict) or not {'chapter','save','create_save','save_slot'} <= set(options)
            or set(options)-{'chapter','save','create_save','save_slot','scope','sim_assist'}):
        raise ValueError('请显式选择存档及是否新建')
    from .play_scope import chapter_sequence,sim_available
    chapter_sequence(options['chapter'],options.get('scope','chapter'))
    if type(options.get('sim_assist',False)) is not bool:
        raise ValueError('模拟辅助须为开或关')
    if options.get('sim_assist') and not sim_available(REPO_ROOT):
        raise ValueError('未安装模拟器，可关闭有限模拟辅助后开始')
    from .saves import validate_selection
    if not options['create_save'] and type(options['save_slot']) is not int:
        raise ValueError('请选择已有存档的槽位')
    auto_name = options['create_save'] is True and isinstance(options['save'],str) and not options['save'].strip()
    if auto_name:
        if options['chapter'] != 0 or options['save_slot'] is not None:
            raise ValueError('新存档须从序章（0）开始并使用空槽位')
        if RunControl(current).pending():
            raise ValueError('请等待当前消息处理完成后再开始')
        save = ''
    else:
        save = validate_selection(options['save'], options['create_save'], options['chapter'], options['save_slot'])
    directory = Path(state['prepared_dir']).resolve()
    if (REPO_ROOT / 'runs').resolve() not in directory.parents:
        raise ValueError('准备目录不属于本项目')
    cfg = RunConfig(agent=state['agent'], save=save, model=state['model'], knowledge_profile='explore')
    from .codex_prepared import load_preparation
    from .kb_study import KbStudy
    load_preparation(str(directory), cfg, KbStudy(cfg.kb_dir, 60))
    # Chat may have been forked when a desktop client owned the preparation thread.
    preparation = read_json(directory / 'prepare_summary.json')
    latest = REPO_ROOT / 'runs/latest.txt'
    before = latest.read_text(encoding='utf-8').strip() if latest.exists() else ''
    release_chat(current)
    operation = {'id': uuid.uuid4().hex, 'kind':'play', 'status':'naming' if auto_name else 'starting',
                 'previous':str(current), 'latest_before':before, 'agent': state['agent']}
    try:
        if auto_name:
            host, _ = launch(['--run',str(current),'--name-save',operation['id']],service='chat')
            operation['play_options'] = dict(options)
        else:
            host, _ = _launch_play(current, options)
    except Exception:
        start_chat(current)
        raise
    operation['host'] = str(host)
    store_operation(anchor, operation, new=True)
    _monitor(anchor, operation)
    return operation_status(anchor)


def _launch_play(current, options):
    state = read_json(current / 'run_state.json')
    directory = Path(state['prepared_dir']).resolve()
    preparation = read_json(directory / 'prepare_summary.json')
    preparation['session_id'] = state['session_id']
    write_json(directory / 'prepare_summary.json', preparation)
    fast = RunControl(current).fast_preference()
    if fast is not None:
        RunControl(directory).set_fast(fast)
    manual_effort=RunControl(current).effort_preference()
    RunControl(directory).set_effort(manual_effort)
    mode=RunControl(current).cutscene_preference() or state.get('cutscene_mode','auto')
    RunControl(directory).set_cutscene_mode(mode)
    return launch(['--engine','codex','--agent',state['agent'],'--save',options['save'],'--knowledge','explore',
                          '--scope',options.get('scope','chapter'),
                          '--sim-assist' if options.get('sim_assist',False) else '--no-sim-assist',
                          '--prepared',str(directory),'--chapter',str(options['chapter']), '--effort',manual_effort or read_json(current/'metrics.json').get('effort',state['effort']),
                          '--keep-obs-open','--playback','--continuous-music','--start-paused','--cutscene-mode',mode] +
                         (['--create-save'] if options['create_save'] else ['--save-slot',str(options['save_slot'])]))
