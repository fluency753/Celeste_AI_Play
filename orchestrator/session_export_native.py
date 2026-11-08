"""Optional Codex simulator metadata reader; never starts a container or reads credentials."""
import inspect
import json
from pathlib import Path
import re
import math
import hashlib
import subprocess
import tempfile

from .session_export import rows, simulation_fact
from . import session_export_projection as projection
from .session_export_projection import project_native_tool, project_tool, iso_epoch, bounded_tree


def scan_native(home, session_id, include_analysis=False, emit=None):
    warnings, calls, sources, tool_calls, lifecycle = [], [], [], [], []
    wrappers, current_turn = {}, None
    for area in ('sessions', 'archived_sessions'):
        root = Path(home) / area
        for path in root.glob('*/*/*/*' + session_id + '*.jsonl'):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            try:
                with path.open('rb') as stream:
                    meta = json.loads(stream.readline(4 * 1024 * 1024))
            except (OSError, ValueError) as exc:
                warnings.append(f'{path}: 原生会话元数据不可读：{exc}')
                continue
            if not isinstance(meta, dict):
                warnings.append(f'{path}: 原生会话元数据格式无效')
                continue
            payload = meta.get('payload') or {}
            if not isinstance(payload, dict) or meta.get('type') != 'session_meta' or payload.get('id', payload.get('session_id')) != session_id:
                warnings.append(f'{path}: 会话身份不匹配，未读取')
                continue
            sources.append({'path': str(path), 'size_at_capture': path.stat().st_size})
            current_turn = None
            for record_index, event in enumerate(rows(path, warnings, line_limit=32 * 1024 * 1024 if include_analysis else 4 * 1024 * 1024), 1):
                payload = event.get('payload') or {}
                if not isinstance(payload, dict):
                    continue
                item = payload.get('item') or {}
                if not isinstance(item, dict):
                    item = {}
                if include_analysis and event.get('type') in ('turn_context', 'event_msg') and isinstance(payload.get('turn_id'), str):
                    current_turn = payload['turn_id']
                if include_analysis and (payload.get('type') in ('task_started', 'task_complete', 'turn_aborted')
                                         or payload.get('type') == 'item_completed' and item.get('type') == 'ContextCompaction'):
                    lifecycle.append({'kind': 'compaction.completed' if item.get('type') == 'ContextCompaction' else payload['type'],
                                      'native_turn_id': payload.get('turn_id') or current_turn,
                                      'item_id': item.get('id'), 'wall': iso_epoch(event.get('timestamp')),
                                      'source': str(path), 'record_index': record_index})
                if include_analysis and event.get('type') == 'response_item' and isinstance(payload.get('call_id'), str):
                    ident, kind = payload['call_id'], payload.get('type')
                    if kind in ('function_call', 'custom_tool_call'):
                        args = {'code': payload.get('input')} if kind == 'custom_tool_call' else payload.get('arguments')
                        projected = project_tool('code_mode.' + str(payload.get('name')), args, None)
                        projected.update(call_id=ident, native_turn_id=current_turn, item_type='CodeModeWrapper',
                                         started_wall=iso_epoch(event.get('timestamp')), ended_wall=None,
                                         source=str(path), record_index=record_index)
                        if emit is not None:
                            emit(projected)
                            wrappers[ident] = {key: projected[key] for key in ('tool', 'call_id', 'native_turn_id', 'item_type', 'source', 'record_index', 'started_wall')}
                        else:
                            wrappers[ident] = projected
                    elif kind in ('function_call_output', 'custom_tool_call_output') and ident in wrappers:
                        row = wrappers[ident]
                        output = project_tool(row['tool'], None, payload.get('output'))
                        row.update({key: output[key] for key in ('result', 'result_meta', 'error')})
                        row['ended_wall'] = iso_epoch(event.get('timestamp'))
                        if emit is not None:
                            emit(row)
                            del wrappers[ident]
                if include_analysis and payload.get('type') == 'item_completed':
                    projected = project_native_tool(item, payload)
                    if projected is not None:
                        projected.update(source=str(path), record_index=record_index)
                        if emit is not None:
                            emit(projected)
                        else:
                            tool_calls.append(projected)
                if payload.get('type') != 'item_completed' or item.get('type') != 'DynamicToolCall' or item.get('tool') != 'celeste_simulate':
                    continue
                began = payload.get('started_at_ms')
                duration = item.get('duration') or {}
                seconds = None
                if isinstance(duration, dict) and type(duration.get('secs')) is int and type(duration.get('nanos')) is int:
                    if duration['secs'] >= 0 and 0 <= duration['nanos'] < 1000000000:
                        seconds = duration['secs'] + duration['nanos'] / 1e9
                fact = simulation_fact(item.get('id'), item.get('arguments'), item.get('content_items'), item.get('success'),
                    wall=began / 1000 if isinstance(began, (int, float)) else None,
                    duration_s=seconds,
                    source=str(path))
                if include_analysis:
                    budget = [8000, False]
                    fact['candidates'] = bounded_tree(fact.get('candidates'), budget)
                    fact['analysis_candidates_truncated'] = budget[1]
                calls.append(fact)
    if not sources:
        warnings.append('没有找到已核验身份的 Codex 原生会话日志；模拟明细可能缺失')
    if include_analysis and emit is None:
        tool_calls.extend(wrappers.values())
    result = {'calls': calls, 'warnings': warnings, 'sources': sources}
    if include_analysis:
        result['tool_calls'] = tool_calls
        result['lifecycle'] = lifecycle
    return result


def reader_code(include_analysis=False):
    code = 'import json, math, re, hashlib\nfrom pathlib import Path\nfrom datetime import datetime\nimport sys\n'
    if include_analysis:
        code += inspect.getsource(projection.safe_text) + '\n\n'
        code += '\n\n'.join(inspect.getsource(value) for value in vars(projection).values()
                            if inspect.isfunction(value) and value.__module__ == projection.__name__) + '\n\n'
    code += '\n\n'.join(inspect.getsource(function) for function in (rows, simulation_fact, scan_native))
    return code


def stream_reader_code(home='/home/agent/.codex'):
    return reader_code(True) + '''
sys.stdout.reconfigure(encoding='utf-8')
count = 0
def emit(row):
    global count
    print(json.dumps({'tool': row}, ensure_ascii=False))
    count += 1
summary = scan_native(''' + repr(str(home)) + ''', sys.argv[1], True, emit)
print(json.dumps({'summary': summary, 'tool_count': count}, ensure_ascii=False))
'''


def read_native_stream(command):
    """Drain fixed reader output to disk, then validate its completion footer."""
    from .session_export_spool import SpoolRows
    tools = SpoolRows()
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, stdout=output, stderr=errors, timeout=60)
            if result.returncode:
                errors.seek(0)
                raise RuntimeError('原生日志流读取失败：' + errors.read(4096).decode('utf-8', 'replace'))
            output.seek(0)
            footer = None
            for line in output:
                record = json.loads(line)
                if footer is not None:
                    raise ValueError('原生日志流结束标记后仍有数据')
                if isinstance(record, dict) and isinstance(record.get('tool'), dict):
                    tools.append(record['tool'])
                elif isinstance(record, dict) and isinstance(record.get('summary'), dict):
                    footer = record
                else:
                    raise ValueError('原生日志流记录无效')
            if footer is None or type(footer.get('tool_count')) is not int or footer['tool_count'] != len(tools):
                raise ValueError('原生日志流未完整结束或记录数不一致')
            summary = footer['summary']
            if tools.store.nonfinite_replaced:
                summary.setdefault('warnings', []).append(f'原生详情中 {tools.store.nonfinite_replaced} 个非有限数字置为 null，未作为零参与统计')
            summary['tool_calls'] = tools
            return summary
    except BaseException:
        tools.close()
        raise


def read_container_simulations(state, container=None, *, include_analysis=False):
    from .container import container_name, docker
    sid = state.get('session_id', '')
    if not re.fullmatch(r'[0-9a-f-]{36}', sid):
        raise ValueError('无效的 Codex 会话 ID')
    tag = state['agent'] + ('-explore' if state.get('knowledge_profile') == 'explore' else '')
    container = container or container_name(tag)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container):
        raise ValueError('无效的容器名称')
    metadata = json.loads(docker('inspect', container, timeout=10).stdout)[0]
    labels = metadata.get('Config', {}).get('Labels') or {}
    if (not metadata.get('State', {}).get('Running') or labels.get('celeste-ai-play.workspace_tag') != tag
            or labels.get('celeste-ai-play.codex_home') != '1'):
        raise ValueError('容器不在运行或工作区归属不匹配；未启动或修改容器')
    # Send a fixed reader over argv, with the identity as a separate argument. Output is a projection only.
    # The reader has no children. A kernel SIGALRM ends this exact process even
    # if the host-side Docker client times out/disconnects; no container is stopped.
    code = 'import signal\nsignal.alarm(40)\n' + (stream_reader_code() if include_analysis else reader_code())
    if not include_analysis:
        code += "\nprint(json.dumps(scan_native('/home/agent/.codex', sys.argv[1], False), ensure_ascii=False))\n"
    if include_analysis:
        # Keep Windows CreateProcess argv comfortably below its 32K limit.
        import base64
        import zlib
        payload = base64.b64encode(zlib.compress(code.encode('utf-8'))).decode('ascii')
        code = "import signal,base64,zlib\nsignal.alarm(40)\nexec(zlib.decompress(base64.b64decode(" + repr(payload) + ")))"
        from .config import find_docker_exe
        return read_native_stream([find_docker_exe(), 'exec', '-u', 'agent', '-w', '/', container,
                                   '/usr/bin/python3', '-I', '-S', '-c', code, sid])
    result = docker('exec', '-u', 'agent', '-w', '/', container, '/usr/bin/python3', '-I', '-S', '-c', code, sid, timeout=60)
    return json.loads(result.stdout)
