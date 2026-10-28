"""暂停中更新主控：只经原控制入口中断，接续同一会话和录制。"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.request
import uuid

from .background import launch
from .config import REPO_ROOT
from .game_process import GameProcess, controller_lease
from .launch_options import resume_args
from .recording import RecordingError
from .session_store import write_json


class HandoffRejected(ValueError):
    """已确认无法接续的交接，不再占用待重试记录。"""


IDENTITY = ('agent', 'save', 'save_slot', 'session_id', 'engine', 'provider', 'model', 'runtime')


def read(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return json.load(stream)


def control(port, payload=None):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/control', data=data,
                                     headers={'Content-Type': 'application/json'} if data else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def inside(value, folder):
    path = Path(value).resolve()
    if (REPO_ROOT / folder).resolve() not in path.parents:
        raise ValueError(f'路径必须位于仓库 {folder}：{path}')
    return path


def recording(run, state):
    pointer = run / 'recording_session.json'
    if not pointer.exists():
        if state.get('record', True):
            raise ValueError('运行要求录制但缺少原录制引用，未中断主控')
        return None
    link = read(pointer)
    from .recording import Recorder
    from .recording_recovery import prefix_counts
    manifest = Recorder.saved_session(run)
    capture = Recorder.capture_directory(manifest)
    prefix_counts(manifest)  # same pure-read recovery validation used by retain
    if manifest.get('state') != 'recording':
        raise ValueError('原录制不处于 recording 状态，不能无缝重启主控')
    return {'id': link['id'], 'manifest': str(Path(link['manifest']).resolve()),
            'capture_dir': str(capture.resolve()),
            'audio_required': (manifest.get('options') or {}).get('audio_source') == 'game_nrt'}


def inspect(port=47792, *, host=None, bridge_port=None):
    snapshot = control(port)
    run = inside(snapshot['run'], 'runs')
    state = read(run / 'run_state.json')
    current = snapshot.get('control') or {}
    if current.get('connected') and current.get('session_id') != state.get('session_id'):
        raise ValueError('控制心跳与运行会话身份不一致')
    if bridge_port is not None and state.get('bridge_port') is not None and int(bridge_port) != state['bridge_port']:
        raise ValueError('显式桥接端口与运行记录不一致')
    selected_host = state.get('controller_host') or host
    selected_port = state.get('bridge_port', bridge_port)
    bridge = {}
    if selected_port is not None:
        from overlay.modhud import bridge_status
        bridge = bridge_status(int(selected_port))
    return {'run': str(run), 'session': {key: state.get(key) for key in IDENTITY},
            'host': str(inside(selected_host, 'local/background')) if selected_host else None,
            'pid': state.get('controller_pid'), 'parent_pid': state.get('controller_parent_pid'), 'bridge_port': selected_port,
            'paused': current.get('paused'), 'phase': current.get('phase'),
            'connected': current.get('connected'), 'interrupt_control': current.get('interrupt_control'),
            'recording': recording(run, state), 'bridge': bridge}


def require_frozen(status):
    if not status['connected'] or not status['paused'] or status['phase'] != 'idle':
        raise ValueError('请先在 HUD 暂停并等当前动作结束；未中断主控')
    if not status.get('interrupt_control'):
        raise ValueError('当前执行器不支持保留接管中断；未中断主控')
    bridge = status['bridge']
    menu = bridge.get('scene') == 'Overworld'
    if not bridge.get('running') or not bridge.get('active') or not (bridge.get('frozen') or menu):
        raise ValueError('没有确认游戏正在冻结；菜单／地图或过期状态不支持保录像重启')
    if not isinstance(bridge.get('frame'), int):
        raise ValueError('缺少游戏帧号，无法核验重启期间是否推进；未中断主控')
    if status['recording'] and not (bridge.get('lockstep') and bridge.get('obs_plugin')):
        raise ValueError('原录制的锁步连接未就绪；未中断主控')
    if status['recording'] and status['recording']['audio_required'] and not bridge.get('recording_audio'):
        raise ValueError('原录制音频已断开；未中断主控')
    return 'menu' if menu and not bridge.get('frozen') else 'frozen'


def interrupt_action(operation):
    database = Path(operation['before']['run']) / 'control.sqlite3'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        row = connection.execute("SELECT id,status,detail FROM actions WHERE kind='interrupt' AND text=? ORDER BY id DESC LIMIT 1",
                                 (operation['token'],)).fetchone()
    return dict(zip(('id', 'status', 'detail'), row)) if row else None


@contextmanager
def operation_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _wait(check, deadline):
    while True:
        result = check()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError('等待超时；原交接保留，修复后用 --retry 接续，不重复启动')
        time.sleep(.2)


def restart(*, port=47792, host=None, bridge_port=None, timeout=90, retry=False, record_path=None):
    path = Path(record_path or REPO_ROOT / 'local/controller-restart.json')
    with operation_lock(path.with_suffix('.lock')):
        return _restart(path, port, host, bridge_port, timeout, retry)


def _restart(path, port, host, bridge_port, timeout, retry):
    deadline = time.monotonic() + timeout
    if retry:
        operation = read(path)
        if operation['phase'] == 'failed':
            raise RuntimeError(operation['error'] + '；本次交接已结束，人工续跑恢复后可重新 restart')
        if operation['phase'] == 'finished':
            now = inspect(operation['port'])
            if now['run'] != operation['after']['run'] or now['session'] != operation['before']['session']:
                raise ValueError('该更新已完成，当前运行已改变；请使用 check')
            operation['after'] = now
            return operation
        port = operation['port']
    else:
        if path.exists() and read(path).get('phase') not in {'finished', 'failed'}:
            raise ValueError('存在未完成更新，请用 --retry；未重复中断或启动')
        before = inspect(port, host=host, bridge_port=bridge_port)
        boundary = require_frozen(before)
        if (Path(before['run']) / 'summary.json').exists():
            raise ValueError('该运行已有结束记录；未中断主控')
        if not before['session']['session_id'] or not before['host'] or before['bridge_port'] is None:
            raise ValueError('旧主控缺少身份记录，请首次指定 --host 和 --bridge-port；未中断')
        old_host = Path(before['host'])
        started = read(old_host / 'status.json')
        argv = read(old_host / 'launch.json')['arguments']
        if started.get('state') != 'running' or before['session']['agent'] not in argv or before['session']['save'] not in argv:
            raise ValueError('宿主与本次运行不匹配；未中断')
        if before['pid'] is not None and started.get('child_pid') not in (before['pid'], before['parent_pid']):
            raise ValueError('主控 PID 与宿主记录不一致；未中断')
        operation = {'phase': 'prepared', 'port': port, 'before': before, 'started_wall': time.time(),
                     'token': uuid.uuid4().hex, 'boundary': boundary,
                     'old_pids': list({pid for pid in (started['child_pid'], started['host_pid'], before['pid']) if pid})}
        write_json(path, operation)
    before = operation['before']
    old = inside(before['run'], 'runs')
    try:
        if operation['phase'] in {'prepared', 'interrupting'}:
            now = inspect(port, host=before['host'], bridge_port=before['bridge_port'])
            if now['run'] != before['run'] or now['session'] != before['session'] or now['recording'] != before['recording']:
                raise HandoffRejected('运行或会话已切换，未操作其他会话')
            action = interrupt_action(operation)
            if not action and not (old / 'summary.json').exists():
                require_frozen(now)
                operation['phase'] = 'interrupting'; write_json(path, operation)
                control(port, {'run': before['run'], 'kind': 'interrupt', 'text': operation['token']})
            operation['phase'] = 'waiting_exit'; write_json(path, operation)
        if operation['phase'] == 'waiting_exit':
            def stopped():
                action = interrupt_action(operation)
                if action is None:
                    raise HandoffRejected('未发现本次中断指令，不接续其他中断')
                if action['status'] == 'error':
                    raise HandoffRejected('中断未被执行：' + action['detail'])
                if action['status'] != 'submitted':
                    return None
                if not (old / 'summary.json').exists():
                    return None
                summary = read(old / 'summary.json')
                if summary.get('game_released') is not False:
                    raise HandoffRejected('旧主控未保留游戏接管，停止自动续接')
                if before['recording']:
                    held = summary.get('recording') or {}
                    closed = (operation.get('boundary') == 'menu' and held.get('encoder_finished') is True
                              and held.get('postprocess_pending') is True and held.get('capture_state') == 'finished'
                              and type(held.get('frames')) is int)
                    if held.get('id') != before['recording']['id'] or not (held.get('state') == 'retained' or closed):
                        raise HandoffRejected('旧录制未成功保留；未另建录制')
                    if closed:
                        operation['closed_frames'] = held['frames']
                if read(Path(before['host']) / 'status.json').get('state') not in {'exited', 'failed'}:
                    return None
                # Holding a process handle distinguishes actual exit from a
                # successful launcher return; a normal interrupt may exit 1.
                for pid in operation['old_pids']:
                    try:
                        process = GameProcess(pid)
                    except OSError as exc:
                        if getattr(exc, 'winerror', None) == 87:
                            continue
                        raise
                    except RuntimeError as exc:
                        if '已退出' in str(exc):
                            continue
                        raise
                    try:
                        process.wait(0)
                    except RuntimeError:
                        return None
                    finally:
                        process.close()
                try:
                    with controller_lease():
                        pass
                except RuntimeError:
                    return None
                return True
            _wait(stopped, deadline)
            current = control(port)
            if Path(current['run']).resolve() != old:
                raise HandoffRejected('当前运行已切换，未启动额外主控')
            args = resume_args(old) + ['--start-paused', '--keep-obs-open', '--hud-port', str(port)]
            if '--bridge-port' not in args:
                args += ['--bridge-port', str(before['bridge_port'])]
            destination = REPO_ROOT / 'local/background' / ('controller-update-' + operation['token'])
            operation.update(phase='launching', host=str(destination.resolve()), arguments=args)
            write_json(path, operation)
        if operation['phase'] == 'launching':
            destination = inside(operation['host'], 'local/background')
            # The host path is persisted before WMI dispatch. If launch's ACK
            # is lost, retry observes this same path rather than launching twice.
            if not destination.exists():
                launch(operation['arguments'], directory=destination)
            operation['phase'] = 'waiting_ready'; write_json(path, operation)
        if operation['phase'] == 'waiting_ready':
            def ready():
                host_path = Path(operation['host'])
                if not (host_path / 'status.json').exists():
                    return None
                host_state = read(host_path / 'status.json')
                if host_state.get('state') in {'failed', 'exited'}:
                    operation['phase'] = 'failed'
                    raise RuntimeError(f'新主控已退出，不会自动另开进程；查看 {host_path / "output.log"}，'
                                       f'排除错误后按标准 --resume {old} --start-paused 续跑')
                current = control(port)
                candidate = inside(current['run'], 'runs')
                if candidate == old:
                    return None
                if not (candidate / 'run_state.json').exists():
                    return None
                try:
                    state = read(candidate / 'run_state.json')
                except (OSError, ValueError):
                    return None  # latest is published before run_state is ready
                if not all(state.get(key) for key in ('controller_host', 'controller_pid', 'session_id')):
                    return None
                if Path(state.get('controller_host') or '').resolve() != host_path.resolve() or host_state.get('child_pid') not in (state.get('controller_pid'), state.get('controller_parent_pid')):
                    raise HandoffRejected('检测到其他宿主接管，停止等待，不操作该会话')
                if {key: state.get(key) for key in IDENTITY} != before['session'] or Path(state.get('resumed_from') or '').resolve() != old:
                    raise HandoffRejected('新主控未接回指定会话，停止操作')
                now = inspect(port)
                previous_recording, current_recording = before['recording'], now['recording']
                if 'closed_frames' in operation:
                    from .recording_recovery import prefix_counts
                    same = (current_recording is not None and
                            all(current_recording.get(key) == previous_recording.get(key) for key in ('id', 'manifest')))
                    if not same or prefix_counts(read(current_recording['manifest']))['frames'] != operation['closed_frames']:
                        raise HandoffRejected('恢复前缀与已关闭录像计量不一致，未声称接续成功')
                elif current_recording != previous_recording:
                    raise HandoffRejected('新主控录制身份改变，未声称接续成功')
                try:
                    boundary = require_frozen(now)
                except ValueError:
                    return None
                if operation.get('boundary', 'frozen') == 'menu':
                    if now['bridge'].get('scene') != 'Overworld':
                        raise HandoffRejected('更新期间已离开选章地图，保持暂停等待检查')
                elif boundary != 'frozen' or now['bridge'].get('frame') != before['bridge'].get('frame'):
                    raise HandoffRejected('重启期间游戏帧号发生变化，未声称零推进；保持当前暂停')
                return now
            operation['after'] = _wait(ready, deadline)
            operation.update(phase='finished', finished_wall=time.time(), error=None)
            write_json(path, operation)
        return operation
    except Exception as exc:
        if isinstance(exc, (HandoffRejected, RecordingError)):
            operation['phase'] = 'failed'
        operation['error'] = str(exc)
        write_json(path, operation)
        raise
