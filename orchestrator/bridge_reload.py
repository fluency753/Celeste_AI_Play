"""Owner-only payload reload, on the existing session connection and action lock.

This module never starts/stops recording, creates a second owner, or retries an
act. It can wait through automatic loading at a zero-input scene boundary.
The resident runtime owns the hold across failure.
An abort only releases a prepared, never-committed hold that the runtime itself
reports as provably unchanged.
"""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import shutil
import time


class BridgeReloadError(RuntimeError):
    pass


def options(data, *, fill_defaults=True):
    if not isinstance(data, dict) or set(data) - {'dll', 'runtime_dll', 'expected_version', 'timeout_s', 'retry', 'abort'}:
        raise ValueError('桥接重载参数只支持 dll、runtime_dll、expected_version、timeout_s、retry、abort')
    result = dict(data)
    if 'dll' in result:
        if not isinstance(result['dll'], str) or not result['dll'].strip():
            raise ValueError('dll 须为本机桥接构建产物路径')
        source = Path(result['dll']).expanduser().resolve()
        if source.name != 'CelesteAIBridge.dll' or not source.is_file():
            raise ValueError('dll 须指向存在的 CelesteAIBridge.dll；不能热更新常驻核心')
        result['dll'] = str(source)
    if 'runtime_dll' in result:
        if not isinstance(result['runtime_dll'], str) or not result['runtime_dll'].strip():
            raise ValueError('runtime_dll 须为与桥接一同构建的 CelesteAIRuntime.dll 路径')
        runtime = Path(result['runtime_dll']).expanduser().resolve()
        if runtime.name != 'CelesteAIRuntime.dll' or not runtime.is_file():
            raise ValueError('runtime_dll 须指向存在的 CelesteAIRuntime.dll（只用于核对，不会安装）')
        result['runtime_dll'] = str(runtime)
    if 'abort' in result:
        if type(result['abort']) is not bool:
            raise ValueError('abort 须为布尔值')
        if result['abort'] and (result.get('retry') or 'dll' in result or 'expected_version' in result):
            raise ValueError('abort 只撤销尚未提交的重载交接，不能与 retry、dll 或 expected_version 同时使用')
    if 'expected_version' in result and (not isinstance(result['expected_version'], str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', result['expected_version'])):
        raise ValueError('expected_version 须为 major.minor.patch 版本')
    timeout = result.get('timeout_s', 120)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 300:
        raise ValueError('timeout_s 须为 1–300 秒')
    if 'retry' in result and type(result['retry']) is not bool:
        raise ValueError('retry 须为布尔值')
    return ({**result, 'timeout_s': timeout, 'retry': result.get('retry', False), 'abort': result.get('abort', False)}
            if fill_defaults else result)


def _state(reply):
    state = reply.get('reload', reply)
    if not isinstance(state, dict):
        raise BridgeReloadError('常驻核心返回了无效重载状态')
    return state


def _write(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _snapshot(hello, obs):
    level, save = obs.get('level') or {}, obs.get('save') or {}
    plugin = (hello.get('clock') or {}).get('plugin') or {}
    result = {'frame': hello.get('frame', obs.get('frame')), 'scene': obs.get('scene'),
            'save': {'name': save.get('name'), 'slot': save.get('slot')},
            'level': {key: level.get(key) for key in ('sid', 'side', 'room', 'chapterTimeTicks')},
            'recording': {'dir': plugin.get('dir'), 'active': plugin.get('recording') is True or plugin.get('state') == 'recording',
                          'audio': hello.get('record_audio_active'),
                          'frames': hello.get('record_audio_frames'), 'replays': hello.get('record_audio_replay_frames')}}
    if obs.get('scene') == 'AreaComplete':
        result['completion'] = {**(obs.get('completion') or {}), 'ui': obs.get('ui')}
    if isinstance(obs.get('reload_boundary'), dict):
        result['boundary'] = obs['reload_boundary']
        if obs['reload_boundary'].get('mode') == 'preserve':
            result['presentation'] = {key: obs.get(key) for key in ('menu', 'ui', 'storyInput', 'storyDecision')}
            if obs['reload_boundary'].get('requires_origin_hold') is True:
                result['presentation']['prompt'] = {key: obs.get(key) for key in ('story', 'storyPlaybackState')}
    return result


def _require_origin_hold(hello, obs):
    if any((value.get('reload_boundary') or {}).get('requires_origin_hold') is True for value in (hello, obs)):
        if hello.get('reload_origin_scene_hold') is not True:
            raise BridgeReloadError('加载器内提示原地重载需要常驻核心 1.0.5 的原场景冻结能力；'
                                    '请先维护重启升级核心，未确认提示、未准备重载或替换文件')


def _verify(before, hello, obs):
    after = _snapshot(hello, obs)
    for key in ('frame', 'scene', 'save', 'level'):
        if before[key] != after[key]:
            raise BridgeReloadError(f'重载核验失败：{key} 已变化（要求 {before[key]!r}，实际 {after[key]!r}）；保持暂停')
    if before.get('completion') != after.get('completion'):
        raise BridgeReloadError('重载核验失败：结算身份或 UI 已变化；保持暂停')
    for key in ('boundary', 'presentation'):
        if key in before and before[key] != after.get(key):
            raise BridgeReloadError(f'重载核验失败：{key} 已变化；保持暂停')
    if hello.get('frozen') is not True:
        raise BridgeReloadError('重载核验失败：游戏尚未保持冻结')
    if before['recording'] != after['recording']:
        raise BridgeReloadError('重载核验失败：原录像目录、录音或音频帧计量已变化；未停止或另开录像')


def hold_message(hello):
    """新连接的 hello 显示游戏仍被桥接重载交接冻结时，给出能否撤销或必须重启的说明；未冻结时返回 None。"""
    if not isinstance(hello, dict) or hello.get('reload_hold') is not True:
        return None
    state = hello.get('reload') if isinstance(hello.get('reload'), dict) else {}
    stage, error = state.get('stage') or '未知', state.get('error') or ''
    detail = f'（阶段 {stage}' + (f'：{error}' if error else '') + '）'
    if state.get('restart_required') is True:
        advice = '这次交接已无法安全接回或撤销，只能保存好其他工作后重启游戏释放冻结'
    elif state.get('abortable') is True:
        advice = ('交接尚未提交，可以撤销：原主控仍在时用 tools/reload_bridge.py --abort；原主控已退出时用'
                  ' tools/reload_bridge.py --abort --direct --run-dir <原运行目录>，直接连接常驻核心并还原已替换的桥接文件')
    else:
        advice = ('原主控仍在时用 tools/reload_bridge.py --retry 接回（可加 --dll 指定兼容构建）；'
                  '原主控已退出或没有兼容构建时只能重启游戏')
    return f'游戏仍被桥接重载交接冻结{detail}，新主控不接管、不发送输入。{advice}。'


def _abort_refusal(state):
    stage, error = state.get('stage') or '未知', state.get('error') or ''
    reason = f'（阶段 {stage}' + (f'：{error}' if error else '') + '）'
    if state.get('restart_required') is True:
        return f'这次重载交接不能撤销{reason}：常驻核心已不知道原桥接的元数据，只能重启游戏释放冻结；原录像与交接保留'
    if stage in {'reloading', 'ready', 'failed'}:
        return (f'这次重载已经提交，原桥接逻辑已卸载或房间已重建{reason}，不能撤销；请用 --retry（可加 --dll 指定兼容构建）接回，'
                '没有兼容构建时只能重启游戏')
    return f'常驻核心报告当前交接不能安全撤销{reason}；保持暂停，不发送输入'


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _runtime_reference(request):
    if request.get('runtime_dll'):
        return Path(request['runtime_dll'])
    dll = Path(request['dll'])
    candidates = [dll.with_name('CelesteAIRuntime.dll')]
    if dll.parent.name.lower() == 'bin' and dll.parent.parent.name.lower() == 'game-bridge':
        candidates.append(dll.parent.parent.parent / 'game-runtime' / 'bin' / 'CelesteAIRuntime.dll')
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BridgeReloadError('找不到与该桥接一同构建的常驻核心 CelesteAIRuntime.dll，无法核对核心是否相同；'
                            '请用 --runtime-dll 指定，未替换文件')


def _installed_runtime(reply, payload_path):
    path = (reply or {}).get('runtime_path')
    if isinstance(path, str) and path:
        return Path(path)
    if payload_path:
        # Mods/CelesteAIBridge/bin/CelesteAIBridge.dll → Mods/CelesteAIRuntime/bin/CelesteAIRuntime.dll
        return Path(payload_path).resolve().parents[2] / 'CelesteAIRuntime' / 'bin' / 'CelesteAIRuntime.dll'
    return None


def _check_runtime(request, installed):
    """与 deploy_bridge.ps1 -HotReload 相同：新桥接所依赖的核心构建必须与游戏里已安装的核心逐字节一致。"""
    reference = _runtime_reference(request)
    if installed is None or not Path(installed).is_file():
        raise BridgeReloadError('找不到游戏正在使用的常驻核心文件，无法核对核心是否相同；未替换文件')
    if _sha256(reference) != _sha256(installed):
        raise BridgeReloadError(f'新桥接构建所依赖的常驻核心（{reference}）与游戏中已安装的核心（{installed}）不同；'
                                '核心变化必须维护重启安装，未替换文件')
    return {'reference': str(reference), 'installed': str(installed), 'sha256': _sha256(installed)}


def _held_counters(record, reply):
    """以常驻核心在冻结后报告的帧号与录音计量为准：准备前的观测与 prepare 之间，回放线程可能还画了一段。"""
    if not isinstance(reply, dict) or reply.get('holding') is not True:
        return False
    counters = {'frame': reply.get('frame'), 'audio': reply.get('record_audio_active'),
                'frames': reply.get('record_audio_frames'), 'replays': reply.get('record_audio_replay_frames')}
    if type(counters['frame']) is not int or type(counters['frames']) is not int or type(counters['replays']) is not int:
        return False
    before = record['before']
    before['frame'] = counters['frame']
    before['recording'].update(audio=counters['audio'], frames=counters['frames'], replays=counters['replays'])
    record['before_from_hold'] = True
    return True


def _restore_payload(record, run_dir):
    """撤销前把已替换的桥接文件还原成备份（与仍在运行的原桥接一致）；原来不存在的文件移入备份目录，不删除。"""
    restored = []
    for item in record.get('files') or []:
        target = Path(item['path'])
        backup = Path(item['backup']) if item.get('backup') else None
        if backup is not None:
            if not backup.is_file():
                raise BridgeReloadError(f'备份文件缺失，不能还原 {target}；保持暂停')
            staged = target.with_name(target.name + '.abort-tmp')
            try:
                staged.write_bytes(backup.read_bytes())
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
            restored.append(str(target))
        elif target.exists():
            aside = Path(run_dir) / 'bridge_reload_backups' / str(record.get('lease')) / ('added-' + target.name)
            aside.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, aside)
            restored.append(str(aside))
    return restored


def _abort(call, run_dir, path, record):
    current = _state(call('reload_status'))
    lease = record.get('lease') or current.get('lease')
    if not isinstance(lease, str) or not lease:
        raise BridgeReloadError('常驻核心没有可撤销的重载交接')
    if record.get('lease') and current.get('lease') != record['lease']:
        raise BridgeReloadError('当前重载 lease 与本会话交接记录不符，不撤销其他交接')
    if current.get('stage') != 'aborted':
        if current.get('abortable') is not True:
            raise BridgeReloadError(_abort_refusal(current))
        if record.get('files'):
            record['restored'] = _restore_payload(record, run_dir)
        if path is not None and record:
            record['phase'] = 'aborting'
            _write(path, record)
        current = _state(call('reload_abort', lease=lease))
        if current.get('stage') != 'aborted':
            raise BridgeReloadError('常驻核心没有确认撤销：' + str(current.get('error') or current.get('stage')))
    record.update(lease=lease, phase='aborted', aborted_wall=time.time(), error=None)
    if path is not None:
        _write(path, record)
    return record


def _replace_payload(source, installed, directory):
    target = Path(installed).resolve()
    source = Path(source).resolve()
    if target.name != 'CelesteAIBridge.dll' or not target.is_file() or source == target:
        raise BridgeReloadError('常驻核心提供的 payload_path 无效，或构建产物就是安装文件；未替换')
    backup = directory
    backup.mkdir(parents=True, exist_ok=True)
    records = []
    for src in (source.with_suffix('.pdb'), source):
        if not src.is_file():
            continue
        dst = target.with_suffix(src.suffix)
        original = backup / dst.name
        if dst.exists() and not original.exists():
            shutil.copy2(dst, original)
        content = src.read_bytes()
        staged = dst.with_name(dst.name + '.reload-tmp')
        try:
            staged.write_bytes(content)
            os.replace(staged, dst)
        finally:
            staged.unlink(missing_ok=True)
        records.append({'path': str(dst), 'sha256': hashlib.sha256(content).hexdigest(),
                        'backup': str(original) if original.exists() else None})
    return records


def reload_bridge(session, run_dir, request, *, cancel=None, owner_was_paused=None, progress=None):
    """Called by SessionControls only while holding its action_lock."""
    explicit_request = options(request, fill_defaults=False)
    request = options(explicit_request)
    run_dir = Path(run_dir) if run_dir is not None else None
    path = run_dir / 'bridge_reload.json' if run_dir is not None else None
    if path is None and not request['abort']:
        raise BridgeReloadError('缺少运行目录，不能记录重载交接')
    # A wait before prepare has no lease or file replacement to resume. Retry
    # its recorded request against a fresh loaded boundary, not an old snapshot.
    cancel_pending_loading = False
    reconnect_pending_boundary = False
    preflight_retry_record = {}
    if request['retry'] and path is not None and path.exists():
        waiting = json.loads(path.read_text(encoding='utf-8'))
        if (not waiting.get('lease') and not waiting.get('prepare_outcome_unknown')
                and waiting.get('resume_phase', waiting.get('phase')) in {'waiting_loading', 'waiting_boundary'}):
            request = options({**waiting.get('request', {}), **explicit_request, 'retry': False})
            if type(waiting.get('owner_was_paused')) is bool:
                owner_was_paused = waiting['owner_was_paused']
            cancel_pending_loading = waiting.get('connection_failed') is True and waiting.get('resume_phase', waiting.get('phase')) == 'waiting_loading'
            reconnect_pending_boundary = waiting.get('connection_failed') is True and not cancel_pending_loading
            preflight_retry_record = waiting
    deadline = time.monotonic() + request['timeout_s']
    record = preflight_retry_record
    already_finished = False
    reuse_ready_attempt = False
    boundary_may_hold = record.get('boundary_release_unconfirmed') is True

    def call(command, *, _loading=False, **args):
        if cancel is not None and cancel.is_set():
            raise BridgeReloadError('重载等待已取消；保留原交接与录像，不解除常驻核心的冻结')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeReloadError('桥接重载等待超时；常驻核心与原录像保持当前状态，可重试接回')
        # The normal game client may wait ten minutes for a long act. Reload
        # control RPCs must honor their own deadline and never retry a mutation.
        with getattr(session, 'lock', nullcontext()):
            client = getattr(session, 'client', None)
            sock = getattr(client, 'sock', None)
            if sock is None:
                try:
                    return session.bridge_call(command, **args)
                except OSError as exc:
                    exc.bridge_connection_failed = True
                    raise
            timeout, connect_timeout = sock.gettimeout(), client.timeout
            try:
                client.timeout = remaining if _loading else min(remaining, 10)
                sock.settimeout(client.timeout)
                return session.bridge_call(command, **args)
            except OSError as exc:
                exc.bridge_connection_failed = True
                raise
            finally:
                client.timeout = connect_timeout
                current_socket = getattr(client, 'sock', None)
                if current_socket is not None:
                    try:
                        current_socket.settimeout(timeout)
                    except OSError:
                        pass  # A failed connection must not hide the original control error.

    def wait_for_loading(hello, obs):
        nonlocal record
        loading_scenes = {'LevelLoader', 'LevelExit', 'OverworldLoader', 'GameLoader'}
        source_scene = hello.get('scene') if hello.get('scene') in loading_scenes else obs.get('scene')
        if hello.get('reload_ready_stop') is not True:
            raise BridgeReloadError('等待加载需要 Bridge 1.6.0 的 reload_ready 截断；未发送输入或替换文件')
        record = {'phase': 'waiting_loading', 'request': dict(request), 'started_wall': time.time(),
                  'owner_was_paused': owner_was_paused}
        _write(path, record)
        if progress:
            progress('正在等待加载或保存完成，将在首个稳定场景边界重载；不确认剧情或结算')
        client = getattr(session, 'client', None)
        previous_check = getattr(client, 'cancel_check', None)
        try:
            if client is not None:
                client.cancel_check = lambda: (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline
            # The placeholder neutral frame must not execute: reload_ready
            # stops before the destination scene updates, including results.
            reply = call('step', _loading=True, inputs=['1'], stop_on=['reload_ready'], progress=0,
                         image=False, entities=False, tiles=False, allow_reconnect=False)
        except OSError as exc:
            confirmed = False
            try:
                # A socket makefile cannot be reused after a read timeout.
                # Reconnect only this owner, cancel the outstanding wait, and
                # never resend its step or any agent action.
                if client is not None and getattr(client, 'sock', None) is not None:
                    old_timeout = client.timeout
                    try:
                        client.timeout = 3
                        client.reconnect()
                        session.bridge_call('cancel', allow_reconnect=False)
                    finally:
                        client.timeout = old_timeout
                        client.sock.settimeout(old_timeout)
                else:
                    session.bridge_call('cancel', allow_reconnect=False)
                confirmed = True
            except Exception:
                pass
            error = BridgeReloadError('加载等待超时或连接中断；未准备重载、未替换文件、未停止录像。'
                                      + ('已取消等待，可用 --retry 重试。' if confirmed else
                                         '尚未确认等待取消，保持暂停，请恢复原连接后用 --retry。'))
            error.bridge_connection_failed = not confirmed
            raise error from exc
        finally:
            if client is not None:
                client.cancel_check = previous_check
        result = reply.get('result') or {}
        record['loading_wait'] = {'source_scene': source_scene, 'stop': result.get('stop'),
                                  'consumed': result.get('consumed'), 'loading_frames': result.get('loadingFrames', 0)}
        _write(path, record)
        if result.get('consumed') != 0 or result.get('stop') != 'reload_ready':
            raise BridgeReloadError('加载等待未在首个稳定场景边界停止；保持暂停，未进入重载或替换文件')
        if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
            raise BridgeReloadError('加载等待已超时或取消；未进入重载，可用 --retry 重试')
        loaded_hello = call('hello')
        loaded_obs = call('observe', image=False, scope='room', fields=True)['obs']
        for key in ('slot', 'name'):
            expected = (obs.get('save') or {}).get(key)
            if expected is None and hello.get('reload_boundary_control') is not True:
                expected = getattr(getattr(session, 'cfg', None), 'save_slot' if key == 'slot' else 'save', None)
            if expected is not None and (loaded_obs.get('save') or {}).get(key) != expected:
                raise BridgeReloadError('加载后存档身份改变；保持暂停，未进入重载')
        if progress:
            progress('加载已完成，正在核验冻结场景并准备重载')
        return loaded_hello, loaded_obs

    def release_boundary():
        # Cleanup must run even after the request was cancelled or timed out.
        # It uses only the existing owner connection and never touches a lease.
        with getattr(session, 'lock', nullcontext()):
            client = getattr(session, 'client', None)
            previous_timeout = getattr(client, 'timeout', None)
            sock = getattr(client, 'sock', None)
            socket_timeout = sock.gettimeout() if sock is not None else None
            try:
                if client is not None: client.timeout = 3
                if sock is not None: sock.settimeout(3)
                session.bridge_call('reload_boundary', enabled=False, allow_reconnect=False)
            finally:
                if client is not None: client.timeout = previous_timeout
                current_socket = getattr(client, 'sock', None)
                if current_socket is not None:
                    try:
                        current_socket.settimeout(socket_timeout)
                    except OSError:
                        pass

    def abort_after_rejection(exc):
        # Pre-commit validation failed while the runtime still reports the
        # untouched pre-prepare game: release it instead of leaving a hold.
        try:
            state = _state(call('reload_status'))
            if state.get('stage') == 'prepared' and state.get('abortable') is True:
                _abort(call, run_dir, path, record)
                error = BridgeReloadError(str(exc) + '；已撤销尚未提交的重载交接，游戏回到重载前状态，原录像未受影响')
                error.reload_aborted = True
                return error
        except Exception:  # Keep the original refusal; the hold stays for --retry/--abort.
            pass
        return exc

    try:
        if cancel_pending_loading or reconnect_pending_boundary:
            client = getattr(session, 'client', None)
            with getattr(session, 'lock', nullcontext()):
                if client is not None and getattr(client, 'sock', None) is not None:
                    old_timeout = client.timeout
                    try:
                        client.timeout = min(deadline - time.monotonic(), 3)
                        client.reconnect()
                    finally:
                        client.timeout = old_timeout
                if cancel_pending_loading:
                    call('cancel', allow_reconnect=False)
        if request['abort']:
            if path is not None and path.exists():
                try:
                    record = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    raise BridgeReloadError('重载交接记录无法读取，不能确认是否替换过文件；未撤销') from exc
            if record.get('phase') == 'finished':
                already_finished = True
                raise BridgeReloadError('这次桥接重载已经完成，无需撤销')
            return _abort(call, run_dir, path, record)
        if request['retry']:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise BridgeReloadError('缺少本次重载交接记录，不能自动接回') from exc
            if not record.get('before'):
                raise BridgeReloadError('重载交接记录没有原状态，不能自动接回')
            if record.get('phase') == 'finished':
                already_finished = True
                raise BridgeReloadError('这次桥接重载已经完成，无需重试')
            if 'expected_version' not in request and record.get('expected_version'):
                request['expected_version'] = record['expected_version']
            elif 'expected_version' in request:
                record['expected_version'] = request['expected_version']
                _write(path, record)
            if record.get('connection_failed'):
                # A timed-out file reader is unusable. Reconnect only this owner
                # client on an explicit retry, never replay its previous command.
                with getattr(session, 'lock', nullcontext()):
                    client = getattr(session, 'client', None)
                    if client is not None:
                        previous_timeout = client.timeout
                        try:
                            client.timeout = min(deadline - time.monotonic(), 10)
                            client.reconnect()
                        finally:
                            client.timeout = previous_timeout
            current = _state(call('reload_status'))
            if not record.get('lease') and (record.get('prepare_outcome_unknown') or record.get('phase') == 'preparing'
                                            or record.get('resume_phase') == 'preparing'):
                # prepare may have succeeded while writing its acknowledgement
                # failed. Recover the resident lease without re-running prepare.
                lease = current.get('lease')
                if current.get('stage') == 'idle' and not lease:
                    record['prepare_outcome_unknown'] = False
                    if record.get('payload_boundary'):
                        boundary_may_hold = True
                        release_boundary()
                        boundary_may_hold = False
                        record.update(phase='waiting_boundary', payload_boundary=False, boundary_release_unconfirmed=False)
                        raise BridgeReloadError('已确认常驻核心未进入重载，临时维护冻结已释放；可用 --retry 重新取得边界')
                    raise BridgeReloadError('已确认常驻核心未进入重载；试玩仍暂停，可继续试玩或重新发起重载')
                if current.get('stage') not in {'prepared', 'reloading', 'ready', 'failed'} or not isinstance(lease, str) or not lease:
                    raise BridgeReloadError('常驻核心没有可接回的重载 lease')
                record.update(lease=lease, payload_path=current.get('payload_path'), prepare_outcome_unknown=False)
                _write(path, record)
            if current.get('lease') != record.get('lease'):
                raise BridgeReloadError('当前重载 lease 与本会话交接记录不符，未替换文件或接回其他重载')
            if current.get('stage') == 'aborted':
                record.update(phase='aborted', error=None)
                _write(path, record)
                already_finished = True
                raise BridgeReloadError('这次桥接重载已经撤销，游戏未重载；如需更新请重新发起重载')
            if not record.get('before_from_hold') and _held_counters(record, current):
                _write(path, record)
            pending_commit = record.get('commit_outcome_unknown') or current.get('stage') == 'reloading'
            while current.get('stage') == 'reloading':
                # The previous commit may be reading the installed DLL right
                # now. Never replace it or submit another commit until that
                # attempt has reached a terminal handoff state.
                time.sleep(.1)
                current = _state(call('reload_status'))
                if current.get('lease') != record.get('lease'):
                    raise BridgeReloadError('等待原重载期间 lease 已变化，保持暂停')
            if pending_commit:
                reuse_ready_attempt = current.get('stage') in {'ready', 'finished'}
                record['commit_outcome_unknown'] = False
                _write(path, record)
            if current.get('stage') == 'finished':
                record['resident_finished'] = True
        else:
            if path.exists():
                previous = json.loads(path.read_text(encoding='utf-8'))
                if (previous.get('lease') or previous.get('prepare_outcome_unknown')) and previous.get('phase') not in {'finished', 'aborted'}:
                    raise BridgeReloadError('本会话存在未完成重载，请使用 --retry 保留原交接状态')
            # Hold the owner connection from the snapshot until the runtime has
            # frozen everything: the playback worker uses the same (re-entrant)
            # lock, so no replay segment can advance the recording counters in
            # between. The counters are then re-read from the held runtime.
            with getattr(session, 'lock', nullcontext()):
                hello = call('hello')
                if hello.get('reload_hold') is True:
                    raise BridgeReloadError(hold_message(hello) or '游戏已有未完成的桥接重载交接')
                if type(hello.get('bridge_reload')) is not int or hello['bridge_reload'] != 1:
                    raise BridgeReloadError('当前桥接没有常驻重载核心；首次安装须维护重启，未替换文件或停止录像')
                if hello.get('input_reinit_guard') is not True or 'input_bindings_ok' not in hello:
                    raise BridgeReloadError('当前常驻核心或桥接不能在重载后恢复并核验注入按键绑定（需要核心 1.0.2 与桥接 1.3.1），'
                                            '重载后合成输入可能按玩家自己的绑定解释；请维护重启安装新版后再使用受控重载。'
                                            '未进入交接，未替换文件或停止录像')
                if hello.get('input_bindings_ok') is not True:
                    mismatch = '、'.join(map(str, hello.get('input_binding_mismatches') or [])) or '未报告'
                    raise BridgeReloadError(f'重载前的注入按键绑定已与桥接不一致（{mismatch}）；请先排查，未进入交接')
                runtime_check = None
                if request.get('dll') and hello.get('runtime_path'):
                    runtime_check = _check_runtime(request, _installed_runtime(hello, None))
                obs = call('observe', image=False, scope='room', fields=True)['obs']
                if hello.get('active') is not True:
                    raise BridgeReloadError('桥接控制权已交还；当前主控不再拥有游戏，未进入重载')
                loading_scenes = {'LevelLoader', 'LevelExit', 'OverworldLoader', 'GameLoader'}
                def waiting():
                    if hello.get('reload_boundary_control') is True:
                        return any((value.get('reload_boundary') or {}).get('mode') == 'wait' for value in (hello, obs))
                    return obs.get('scene') in loading_scenes or hello.get('scene') in loading_scenes
                if waiting():
                    hello, obs = wait_for_loading(hello, obs)
                _require_origin_hold(hello, obs)
                if hello.get('reload_boundary_control') is True:
                    record.update(phase='waiting_boundary', request=dict(request), owner_was_paused=owner_was_paused)
                    _write(path, record)
                    while True:
                        # Menus normally animate while idle. Freeze in the payload
                        # before reading the baseline, then transfer that hold to
                        # the resident prepare in the same game-thread boundary.
                        boundary_may_hold = True
                        if call('reload_boundary', enabled=True).get('acquired') is True:
                            break
                        hello = call('hello')
                        obs = call('observe', image=False, scope='room', fields=True)['obs']
                        if waiting():
                            hello, obs = wait_for_loading(hello, obs)
                        time.sleep(.05)
                    hello = call('hello')
                    obs = call('observe', image=False, scope='room', fields=True)['obs']
                    boundary = obs.get('reload_boundary') or {}
                    mode = boundary.get('mode')
                    if (mode not in {'replace', 'preserve'} or not boundary.get('scene')
                            or hello.get('reload_boundary') != boundary):
                        raise BridgeReloadError('桥接维护边界描述缺失或不一致；未进入重载')
                    _require_origin_hold(hello, obs)
                else:
                    mode = 'replace' if obs.get('scene') == 'Level' else 'preserve'
                    if obs.get('scene') not in {'Level', 'AreaComplete'}:
                        raise BridgeReloadError(f'场景 {obs.get("scene")} 需要 Bridge 1.7.0 的维护冻结边界；未进入重载')
                if hello.get('frozen') is not True:
                    raise BridgeReloadError('只能在游戏已冻结的动作边界重载；未发送游戏输入')
                kind = obs.get('scene')
                if mode == 'preserve' and hello.get('reload_scene_modes') is not True:
                    raise BridgeReloadError('保留场景需要核心 1.0.4 的通用场景交接支持；请先维护升级，未进入重载')
                level = (obs.get('level') if kind == 'Level' else obs.get('completion')) or {}
                save = obs.get('save') or {}
                if type(hello.get('frame')) is not int:
                    raise BridgeReloadError('当前观测缺少完整帧号，不能核验无缝重载')
                if kind in {'Level', 'AreaComplete'} and (type(save.get('slot')) is not int or not save.get('name') or not level.get('sid')
                        or not level.get('room') or not level.get('side') or type(level.get('chapterTimeTicks')) is not int):
                    raise BridgeReloadError('当前观测缺少完整帧号、存档或章节计时，不能核验无缝重载')
                if kind == 'AreaComplete' and ((obs.get('ui') or {}).get('type') != 'AreaComplete'
                                             or type((obs.get('ui') or {}).get('confirmReady')) is not bool):
                    raise BridgeReloadError('当前结算观测不完整，未进入重载')
                loading_wait = record.get('loading_wait')
                record = {'phase': 'preparing', 'before': _snapshot(hello, obs), 'started_wall': time.time(),
                          'request': dict(request), 'payload_boundary': boundary_may_hold,
                          'initial_timeout_s': request['timeout_s'],
                          'expected_version': request.get('expected_version'),
                          'scene_mode': mode,
                          'room_rebuild': ('Everest 普通重载会重建当前房间；临时剧情、玩家动作、手持物和跟随物不保证保留。'
                                           if mode == 'replace' else 'Everest 保留原场景对象与进度，不重建房间或代按确认。')}
                if loading_wait is not None:
                    record['loading_wait'] = loading_wait
                record['reload_scene_modes'] = hello.get('reload_scene_modes') is True
                if runtime_check:
                    record['runtime_check'] = runtime_check
                if type(owner_was_paused) is bool:
                    record['owner_was_paused'] = owner_was_paused
                _write(path, record)
                while True:
                    # The runtime may establish the hold even if its reply is lost.
                    # Persist uncertainty before sending so an explicit retry queries
                    # status instead of issuing prepare a second time.
                    record['prepare_outcome_unknown'] = True
                    _write(path, record)
                    current = _state(call('reload_prepare'))
                    if current.get('stage') != 'busy':
                        break
                    record['prepare_outcome_unknown'] = False
                    time.sleep(.05)
                if current.get('stage') == 'refused':
                    record['prepare_outcome_unknown'] = False
                    raise BridgeReloadError('常驻核心拒绝重载：' + str(current.get('error') or '当前状态不支持')
                                            + '；未进入交接，未替换文件')
                lease = current.get('lease')
                if current.get('stage') != 'prepared' or not isinstance(lease, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', lease):
                    raise BridgeReloadError(current.get('error') or '常驻核心没有确认重载交接，未替换文件')
                record.update(lease=lease, phase='prepared', payload_path=current.get('payload_path'), prepare_outcome_unknown=False)
                boundary_may_hold = False  # Export transferred the payload boundary to the resident hold.
                _held_counters(record, current)
                _write(path, record)

        if request.get('dll') and not record.get('resident_finished') and not reuse_ready_attempt:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record.get('lease') or ''):
                raise BridgeReloadError('常驻核心返回了非法 lease，未替换文件')
            try:
                record['runtime_check'] = _check_runtime(
                    request, _installed_runtime(current, record.get('payload_path') or current.get('payload_path')))
            except BridgeReloadError as exc:
                raise abort_after_rejection(exc) from exc
            _write(path, record)
            record['files'] = _replace_payload(request['dll'], record.get('payload_path') or current.get('payload_path') or '',
                                               run_dir / 'bridge_reload_backups' / record['lease'])
            record['phase'] = 'replaced'
            _write(path, record)
        lease = record['lease']
        current = _state(call('reload_status'))
        if current.get('lease') != lease:
            raise BridgeReloadError('重载交接已变化，保持暂停且不提交其他 lease')
        if not record.get('resident_finished') and not reuse_ready_attempt and (current.get('stage') not in {'ready', 'finished'} or request.get('dll')):
            record.update(phase='committing', commit_outcome_unknown=True)
            _write(path, record)
            args = {'lease': lease}
            if record.get('reload_scene_modes'):
                args['expect_scene_change'] = record.get('scene_mode', 'replace') == 'replace'
            if request.get('expected_version'):
                args['expected_version'] = request['expected_version']
            call('reload_commit', **args)
            record['commit_outcome_unknown'] = False
        while True:
            current = _state(call('reload_status'))
            if current.get('lease') != lease:
                raise BridgeReloadError('重载等待期间 lease 已变化，保持暂停')
            if current.get('stage') in {'ready', 'finished'}:
                break
            if current.get('stage') == 'failed':
                raise BridgeReloadError('桥接重载失败：' + str(current.get('error') or '新逻辑未接回') + '；原录像保持，试玩仍暂停')
            if current.get('stage') == 'aborted':
                already_finished = True
                raise BridgeReloadError('这次重载交接已被撤销，原桥接继续运行；未提交新桥接')
            time.sleep(.1)
        hello = call('hello')
        if request.get('expected_version') and hello.get('bridge') != request['expected_version']:
            raise BridgeReloadError('新桥接版本与要求不符，保持暂停')
        if type(hello.get('bridge_reload')) is not int or hello['bridge_reload'] != 1:
            raise BridgeReloadError('新桥接缺少受控重载能力，保持暂停')
        from .action_policy import require_capability, require_pico8_guard
        require_capability(hello)
        if record.get('scene_mode') == 'preserve' and hello.get('reload_scene_modes') is not True:
            raise BridgeReloadError('新桥接缺少保留场景交接能力；保持暂停')
        try:
            require_pico8_guard(hello)
        except ValueError as exc:
            raise BridgeReloadError(str(exc) + '保持暂停，修复后用 --retry 重新核验。') from exc
        if hello.get('entry_guard') is not True:
            raise BridgeReloadError('新桥接的地图入场门禁未加载，保持暂停')
        if getattr(session.cfg, 'play_scope', 'chapter') == 'visited' and hello.get('map_entry_set') is not True:
            raise BridgeReloadError('新桥接缺少自由探索地图集合门禁，保持暂停；请安装兼容桥接后重试')
        from .detour import enabled as detour_enabled
        if detour_enabled(session.cfg) and hello.get('map_entry_set') is not True:
            raise BridgeReloadError('新桥接缺少计划外地图所需的地图集合门禁，保持暂停；请安装兼容桥接后重试')
        if hello.get('input_bindings_ok') is not True:
            mismatch = '、'.join(map(str, hello.get('input_binding_mismatches') or [])) or '未报告'
            raise BridgeReloadError(f'新桥接接回后注入按键绑定与桥接不一致（{mismatch}），继续会按玩家自己的绑定解释合成输入；'
                                    '保持暂停，可检查后用 --retry 重新核验')
        obs = call('observe', image=False, scope='room', fields=True)['obs']
        if (record['before'].get('boundary') or {}).get('requires_origin_hold') is True:
            if hello.get('reload_origin_scene_hold') is not True:
                raise BridgeReloadError('新桥接未报告原场景冻结能力；继续保持交接，请用兼容核心与桥接接回')
        _verify(record['before'], hello, obs)
        if current.get('stage') != 'finished':
            call('reload_finish', lease=lease)
        record.update(resident_finished=True, phase='refreshing', version=hello.get('bridge'))
        _write(path, record)
        session.entry_guard_supported = hello.get('entry_guard') is True
        session.chapter_reset_guard_supported = hello.get('chapter_reset_guard') is True
        session.action_policy_supported = hello.get('action_policy')
        session.map_entry_set_supported = hello.get('map_entry_set') is True
        session.bridge_reload_supported = True
        refresh = getattr(session, 'after_bridge_reload', None)
        if refresh:
            try:
                refresh(obs)
            except Exception as exc:
                raise BridgeReloadError('新桥接已接回，但观测或工作区刷新失败：' + str(exc)
                                        + '；保持暂停，可用 --retry 补齐，不会再次重载或执行动作') from exc
        else:
            session._input_formats = None
            session._held_input = ('', 'celeste_ai')
            session._last_positions, session._last_room = {}, None
            from .observation import enrich_obs
            session.last_obs = enrich_obs(obs)
            reset = getattr(session, 'reset_observation_delivery', None)
            if reset:
                reset()
        record.update(phase='finished', version=hello.get('bridge'), finished_wall=time.time(), error=None)
        _write(path, record)
        return record
    except Exception as exc:
        reported = exc
        if boundary_may_hold and not record.get('lease') and not record.get('prepare_outcome_unknown'):
            try:
                # Cleanup has its own short deadline: cancellation/timeout must
                # still release a payload-only menu freeze. Never acquire a new
                # connection or release an uncertain resident prepare here.
                release_boundary()
                record['boundary_release_unconfirmed'] = False
            except Exception as cleanup_error:
                record['boundary_release_unconfirmed'] = True
                reported = BridgeReloadError(str(exc) + '；临时维护冻结尚未确认释放：' + str(cleanup_error)
                                             + '。请恢复原连接后用 --retry 接回，不要另开主控。')
                reported.bridge_connection_failed = isinstance(cleanup_error, OSError)
        if record.get('prepare_outcome_unknown') and not record.get('lease'):
            reported = BridgeReloadError('重载准备请求的结果尚未确认：' + str(exc)
                                         + '；保持暂停，请用 --retry 查询常驻核心，不重复准备或原动作')
        aborted = getattr(exc, 'reload_aborted', False)
        reported.reload_held = bool(record.get('lease') or record.get('prepare_outcome_unknown') or
                                    record.get('boundary_release_unconfirmed')) and not already_finished and not aborted
        if record and not already_finished and not aborted and path is not None:
            record.update(resume_phase=record.get('resume_phase') if record.get('phase') == 'failed' else record.get('phase'),
                          phase='failed', error=str(reported),
                          connection_failed=getattr(exc, 'bridge_connection_failed', False) or
                                            getattr(reported, 'bridge_connection_failed', False))
            try:
                _write(path, record)
            except OSError:
                pass  # The runtime still owns the hold; never release it on persistence failure.
        if reported is not exc:
            raise reported from exc
        raise
