"""Optional Codex simulator metadata reader; never starts a container or reads credentials."""
import inspect
import json
from pathlib import Path
import re

from .session_export import rows, simulation_fact


def scan_native(home, session_id):
    warnings, calls, sources = [], [], []
    for area in ('sessions', 'archived_sessions'):
        root = Path(home) / area
        for path in root.glob('*/*/*/*' + session_id + '*.jsonl'):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            with path.open('rb') as stream:
                meta = json.loads(stream.readline(4 * 1024 * 1024))
            payload = meta.get('payload') or {}
            if meta.get('type') != 'session_meta' or payload.get('id', payload.get('session_id')) != session_id:
                warnings.append(f'{path}: 会话身份不匹配，未读取')
                continue
            sources.append({'path': str(path), 'size_at_capture': path.stat().st_size})
            for event in rows(path, warnings):
                payload = event.get('payload') or {}
                item = payload.get('item') or {}
                if payload.get('type') != 'item_completed' or item.get('type') != 'DynamicToolCall' or item.get('tool') != 'celeste_simulate':
                    continue
                began = payload.get('started_at_ms')
                duration = item.get('duration') or {}
                seconds = None
                if isinstance(duration, dict) and type(duration.get('secs')) is int and type(duration.get('nanos')) is int:
                    if duration['secs'] >= 0 and 0 <= duration['nanos'] < 1000000000:
                        seconds = duration['secs'] + duration['nanos'] / 1e9
                calls.append(simulation_fact(item.get('id'), item.get('arguments'), item.get('content_items'), item.get('success'),
                    wall=began / 1000 if isinstance(began, (int, float)) else None,
                    duration_s=seconds,
                    source=str(path)))
    if not sources:
        warnings.append('没有找到已核验身份的 Codex 原生会话日志；模拟明细可能缺失')
    return {'calls': calls, 'warnings': warnings, 'sources': sources}


def reader_code():
    code = 'import json\nfrom pathlib import Path\nimport sys\n'
    code += '\n\n'.join(inspect.getsource(function) for function in (rows, simulation_fact, scan_native))
    return code


def read_container_simulations(state, container=None):
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
    code = 'import signal\nsignal.alarm(40)\n' + reader_code()
    code += "\nprint(json.dumps(scan_native('/home/agent/.codex', sys.argv[1]), ensure_ascii=False))\n"
    result = docker('exec', '-u', 'agent', '-w', '/', container, '/usr/bin/python3', '-I', '-S', '-c', code, sid, timeout=60)
    return json.loads(result.stdout)
