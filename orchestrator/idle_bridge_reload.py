"""空闲游戏的维护连接：复用桥接重载，不启动模型、录制或游戏任务。"""
from __future__ import annotations

from pathlib import Path
import threading
import uuid
from types import SimpleNamespace

from .bridge_reload import BridgeReloadError, _check_runtime, options, reload_bridge
from .config import REPO_ROOT
from .game_process import GameProcess, controller_lease, idle_bridge_listener
from .session_store import load_json, write_json
from tools.bridge_client import BridgeClient


class MaintenanceConnection:
    def __init__(self, client):
        self.client = client
        self.lock = threading.RLock()
        self.cfg = SimpleNamespace(play_scope='chapter', allow_detour=False)

    def bridge_call(self, command, **args):
        args.setdefault('allow_reconnect', False)
        return self.client.call(command, **args)

    def after_bridge_reload(self, obs):
        self.last_obs = obs


def maintenance_directory(port):
    return REPO_ROOT / 'local' / 'bridge-maintenance' / str(port)


def _read(path):
    value = load_json(path) if path.exists() else {}
    if not isinstance(value, dict):
        raise BridgeReloadError('维护记录不是 JSON 对象，不能判断交接归属；未继续操作')
    return value


def _recording(hello):
    clock = hello.get('clock') or {}
    plugin = clock.get('plugin') or {}
    return (hello.get('record_audio_active') is True or clock.get('lockstep') is True
            or plugin.get('recording') is True or plugin.get('state') in {'recording', 'finishing'})


def _return_control(session, process, metadata, path):
    process.identity()   # The original process handle must still be alive.
    hello = session.bridge_call('hello')
    if hello.get('reload_hold') is True or _recording(hello):
        raise BridgeReloadError('维护交接或录制仍在持有游戏；不解除冻结，请用 --retry/--abort 接回')
    metadata['phase'] = 'returning'
    write_json(path, metadata, indent=2)
    if hello.get('active') is True:
        session.bridge_call('deactivate')
        hello = session.bridge_call('hello')
    if hello.get('active') is not False:
        raise BridgeReloadError('未确认维护控制权已归还；请用 --retry 查询，不重做桥接更新')
    metadata['phase'] = 'returned'
    metadata.pop('error', None)
    write_json(path, metadata, indent=2)


def reload_idle(port, request, *, directory=None, game_dir=None, save_slot=None, progress=None):
    """调用者明确选择空闲维护；准入必须发生在 TCP connect 前。失败后不退出游戏。"""
    request = options(request)
    if not request['retry'] and not request['abort'] and not request.get('dll'):
        raise BridgeReloadError('空闲更新须指定 --dll；未连接游戏')
    directory = Path(directory).resolve() if directory else maintenance_directory(port)
    metadata_path = directory / 'idle_maintenance.json'
    ledger_path = directory / 'bridge_reload.json'
    with controller_lease():
        metadata = _read(metadata_path)
        recovering = request['retry'] or request['abort']
        if metadata and metadata.get('phase') != 'returned' and not recovering:
            raise BridgeReloadError(f'存在未完成的空闲维护，请用 --direct --retry --run-dir "{directory}" 接回')
        if recovering and (metadata.get('mode') != 'idle_maintenance' or metadata.get('port') != port):
            raise BridgeReloadError('缺少匹配端口的空闲维护身份记录；未连接游戏')
        if recovering and metadata.get('phase') == 'returned':
            return {**_read(ledger_path), 'idle_control_returned': True, 'already_returned': True}
        pid = idle_bridge_listener(port)  # Refuse even an inactive existing client.
        process = GameProcess(pid)
        client = None
        owned = False
        try:
            identity = process.identity()
            if recovering and identity != metadata.get('process'):
                raise BridgeReloadError('游戏进程身份已变化，不能接回其他游戏的维护；未连接')
            client = BridgeClient(port=port, timeout=min(request['timeout_s'], 10))
            session = MaintenanceConnection(client)
            hello = session.bridge_call('hello')
            process.identity()
            if hello.get('process_id') is not None and hello['process_id'] != pid:
                raise BridgeReloadError('桥接进程与端口监听者不符；未接管')
            if _recording(hello):
                raise BridgeReloadError('游戏仍有录制在用；空闲维护不停止或接管原录像')
            if not recovering:
                if hello.get('active') is not False or hello.get('reload_hold') is True:
                    raise BridgeReloadError('游戏并非空闲，或已有重载交接；未接管')
                runtime = hello.get('runtime_path')
                if not runtime or hello.get('process_id') != pid:
                    raise BridgeReloadError('桥接未提供可核对的进程与 Runtime 路径；未接管')
                actual_game = Path(runtime).resolve().parents[3]
                if game_dir is not None and actual_game != Path(game_dir).resolve():
                    raise BridgeReloadError('桥接所属游戏目录与部署目标不符；未接管或替换文件')
                if not request.get('dll'):
                    raise BridgeReloadError('空闲更新须指定 --dll；未接管')
                _check_runtime(request, Path(runtime))
                directory.mkdir(parents=True, exist_ok=True)
                if ledger_path.exists():
                    previous = _read(ledger_path)
                    if previous.get('phase') not in {'finished', 'aborted'}:
                        raise BridgeReloadError('目录中已有未完成交接；未接管，请使用原来的 --retry/--abort 入口')
                    ledger_path.rename(directory / ('bridge_reload-' + uuid.uuid4().hex + '.json'))
                metadata = {'mode': 'idle_maintenance', 'port': port, 'process': identity,
                            'game_dir': str(actual_game), 'runtime_path': runtime,
                            'save_slot': save_slot, 'phase': 'activating'}
                write_json(metadata_path, metadata, indent=2)  # Before any ownership mutation.
                owned = True
                activated = session.bridge_call('activate', image=False, entities=False, tiles=False)
                slot = ((activated.get('obs') or {}).get('save') or {}).get('slot')
                if save_slot is not None and slot is not None and slot != save_slot:
                    raise BridgeReloadError('当前存档槽与维护指定值不符；未更新桥接')
            else:
                if game_dir is not None and Path(metadata['game_dir']) != Path(game_dir).resolve():
                    raise BridgeReloadError('指定目录与原维护目标不同；未更新或归还其他游戏')
                owned = True
                ledger = _read(ledger_path)
                # A lost deactivate reply is reconciled without replaying reload.
                if metadata.get('phase') == 'returning' or ledger.get('phase') in {'finished', 'aborted'}:
                    _return_control(session, process, metadata, metadata_path)
                    return {**ledger, 'idle_control_returned': True}
                # An activate may have failed before the reload worker wrote anything.
                if not ledger:
                    _return_control(session, process, metadata, metadata_path)
                    return {'phase': 'not_started', 'idle_control_returned': True}
            result = reload_bridge(session, directory, request, owner_was_paused=True, progress=progress)
            _return_control(session, process, metadata, metadata_path)
            return {**result, 'idle_control_returned': True}
        except (Exception, KeyboardInterrupt) as exc:
            if owned:
                try:
                    ledger = _read(ledger_path)
                except (OSError, ValueError, BridgeReloadError):
                    ledger = {'prepare_outcome_unknown': True}
                held = (getattr(exc, 'reload_held', False) or ledger.get('prepare_outcome_unknown')
                        or ledger.get('boundary_release_unconfirmed')
                        or (ledger.get('lease') and ledger.get('phase') not in {'finished', 'aborted'}))
                reason = '用户取消维护' if isinstance(exc, KeyboardInterrupt) else str(exc)
                recover = (f'接回命令：python tools/reload_bridge.py --direct --retry --bridge-port {port}'
                           f' --run-dir "{directory}"')
                if not held and metadata.get('phase') != 'returning':
                    try:
                        _return_control(session, process, metadata, metadata_path)
                    except Exception as cleanup:
                        metadata['error'] = f'{reason}；控制归还未确认：{cleanup}'
                        write_json(metadata_path, metadata, indent=2)
                        raise BridgeReloadError(metadata['error'] + '；' + recover) from exc
                    raise BridgeReloadError(reason + '；已归还原空闲游戏控制权，未自动重试更新或重启游戏') from exc
                else:
                    metadata['error'] = reason
                    write_json(metadata_path, metadata, indent=2)
                    raise BridgeReloadError(reason + '；保留维护交接。' + recover) from exc
            raise
        finally:
            if client is not None:
                client.close()
            process.close()
