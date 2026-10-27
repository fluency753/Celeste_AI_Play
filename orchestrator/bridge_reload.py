"""Owner-only payload reload, on the existing session connection and action lock.

This module never starts/stops recording, creates a bridge connection, advances
the game, or retries an act. The resident runtime owns the hold across failure.
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


def options(data):
    if not isinstance(data, dict) or set(data) - {'dll', 'expected_version', 'timeout_s', 'retry'}:
        raise ValueError('桥接重载参数只支持 dll、expected_version、timeout_s、retry')
    result = dict(data)
    if 'dll' in result:
        if not isinstance(result['dll'], str) or not result['dll'].strip():
            raise ValueError('dll 须为本机桥接构建产物路径')
        source = Path(result['dll']).expanduser().resolve()
        if source.name != 'CelesteAIBridge.dll' or not source.is_file():
            raise ValueError('dll 须指向存在的 CelesteAIBridge.dll；不能热更新常驻核心')
        result['dll'] = str(source)
    if 'expected_version' in result and (not isinstance(result['expected_version'], str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', result['expected_version'])):
        raise ValueError('expected_version 须为 major.minor.patch 版本')
    timeout = result.get('timeout_s', 120)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 300:
        raise ValueError('timeout_s 须为 1–300 秒')
    if 'retry' in result and type(result['retry']) is not bool:
        raise ValueError('retry 须为布尔值')
    return {**result, 'timeout_s': timeout, 'retry': result.get('retry', False)}


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
    return {'frame': hello.get('frame', obs.get('frame')), 'scene': obs.get('scene'),
            'save': {'name': save.get('name'), 'slot': save.get('slot')},
            'level': {key: level.get(key) for key in ('sid', 'side', 'room', 'chapterTimeTicks')},
            'recording': {'dir': plugin.get('dir'), 'active': plugin.get('recording') is True or plugin.get('state') == 'recording',
                          'audio': hello.get('record_audio_active'),
                          'frames': hello.get('record_audio_frames'), 'replays': hello.get('record_audio_replay_frames')}}


def _verify(before, hello, obs):
    after = _snapshot(hello, obs)
    for key in ('frame', 'scene', 'save', 'level'):
        if before[key] != after[key]:
            raise BridgeReloadError(f'重载核验失败：{key} 已变化（要求 {before[key]!r}，实际 {after[key]!r}）；保持暂停')
    if hello.get('frozen') is not True:
        raise BridgeReloadError('重载核验失败：游戏尚未保持冻结')
    if before['recording'] != after['recording']:
        raise BridgeReloadError('重载核验失败：原录像目录、录音或音频帧计量已变化；未停止或另开录像')


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


def reload_bridge(session, run_dir, request, *, cancel=None, owner_was_paused=None):
    """Called by SessionControls only while holding its action_lock."""
    request = options(request)
    run_dir = Path(run_dir)
    path = run_dir / 'bridge_reload.json'
    deadline = time.monotonic() + request['timeout_s']
    record = {}
    already_finished = False
    reuse_ready_attempt = False

    def call(command, **args):
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
                client.timeout = min(remaining, 10)
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

    try:
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
                    raise BridgeReloadError('已确认常驻核心未进入重载；试玩仍暂停，可继续试玩或重新发起重载')
                if current.get('stage') not in {'prepared', 'reloading', 'ready', 'failed'} or not isinstance(lease, str) or not lease:
                    raise BridgeReloadError('常驻核心没有可接回的重载 lease')
                record.update(lease=lease, payload_path=current.get('payload_path'), prepare_outcome_unknown=False)
                _write(path, record)
            if current.get('lease') != record.get('lease'):
                raise BridgeReloadError('当前重载 lease 与本会话交接记录不符，未替换文件或接回其他重载')
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
                if (previous.get('lease') or previous.get('prepare_outcome_unknown')) and previous.get('phase') != 'finished':
                    raise BridgeReloadError('本会话存在未完成重载，请使用 --retry 保留原交接状态')
            hello = call('hello')
            if type(hello.get('bridge_reload')) is not int or hello['bridge_reload'] != 1:
                raise BridgeReloadError('当前桥接没有常驻重载核心；首次安装须维护重启，未替换文件或停止录像')
            obs = call('observe', image=False, scope='room', fields=True)['obs']
            if hello.get('frozen') is not True:
                raise BridgeReloadError('只能在章节已冻结的动作边界重载；未发送游戏输入')
            if obs.get('scene') != 'Level' or not (obs.get('level') or {}).get('sid'):
                raise BridgeReloadError('当前不在可核验的关卡内，暂不支持保持录像的桥接重载')
            level, save = obs.get('level') or {}, obs.get('save') or {}
            if (type(hello.get('frame')) is not int or type(save.get('slot')) is not int or not save.get('name')
                    or not level.get('room') or not level.get('side') or type(level.get('chapterTimeTicks')) is not int):
                raise BridgeReloadError('当前观测缺少完整帧号、存档或章节计时，不能核验无缝重载')
            record = {'phase': 'preparing', 'before': _snapshot(hello, obs), 'started_wall': time.time(),
                      'expected_version': request.get('expected_version')}
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
            lease = current.get('lease')
            if current.get('stage') != 'prepared' or not isinstance(lease, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', lease):
                raise BridgeReloadError(current.get('error') or '常驻核心没有确认重载交接，未替换文件')
            record.update(lease=lease, phase='prepared', payload_path=current.get('payload_path'), prepare_outcome_unknown=False)
            _write(path, record)

        if request.get('dll') and not record.get('resident_finished') and not reuse_ready_attempt:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record.get('lease') or ''):
                raise BridgeReloadError('常驻核心返回了非法 lease，未替换文件')
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
            time.sleep(.1)
        hello = call('hello')
        if request.get('expected_version') and hello.get('bridge') != request['expected_version']:
            raise BridgeReloadError('新桥接版本与要求不符，保持暂停')
        if type(hello.get('bridge_reload')) is not int or hello['bridge_reload'] != 1:
            raise BridgeReloadError('新桥接缺少受控重载能力，保持暂停')
        from .action_policy import require_capability
        require_capability(hello)
        if hello.get('entry_guard') is not True:
            raise BridgeReloadError('新桥接的地图入场门禁未加载，保持暂停')
        obs = call('observe', image=False, scope='room', fields=True)['obs']
        _verify(record['before'], hello, obs)
        if current.get('stage') != 'finished':
            call('reload_finish', lease=lease)
        record.update(resident_finished=True, phase='refreshing', version=hello.get('bridge'))
        _write(path, record)
        session.entry_guard_supported = hello.get('entry_guard') is True
        session.chapter_reset_guard_supported = hello.get('chapter_reset_guard') is True
        session.action_policy_supported = hello.get('action_policy')
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
        if record.get('prepare_outcome_unknown') and not record.get('lease'):
            reported = BridgeReloadError('重载准备请求的结果尚未确认：' + str(exc)
                                         + '；保持暂停，请用 --retry 查询常驻核心，不重复准备或原动作')
        reported.reload_held = bool(record.get('lease') or record.get('prepare_outcome_unknown')) and not already_finished
        if record and not already_finished:
            record.update(resume_phase=record.get('phase'), phase='failed', error=str(reported),
                          connection_failed=getattr(exc, 'bridge_connection_failed', False))
            try:
                _write(path, record)
            except OSError:
                pass  # The runtime still owns the hold; never release it on persistence failure.
        if reported is not exc:
            raise reported from exc
        raise
