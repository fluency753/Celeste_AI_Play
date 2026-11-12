"""Bounded, export-only projections. Never project model messages or reasoning."""
import hashlib
import json
import math
import re
from datetime import datetime

from .tool_facts import native_shell_args, safe_text


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_text(value, limit=4000):
    text = str(value)
    text = re.sub(r'data:[^\s\"\'<>]+', '[attachment omitted]', text, flags=re.I)
    text = re.sub(r'<(analysis|thinking|reasoning)>.*?</\1>', '[private reasoning omitted]', text, flags=re.I | re.S)
    text = re.sub(r'''(?i)(\bauthorization\s*[:=]\s*)(?:bearer|basic|digest|negotiate)\s+[^\r\n"'<>]+''', r'\1[REDACTED]', text)
    # Consume the complete bearer value before generic key:value redaction can
    # remove only the word "Bearer" and leave its credential behind.
    text = re.sub(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]', text)
    return safe_text(text, limit)


def bounded_tree(value, budget=None, depth=0):
    """A budget applies across the entire value, including nested arrays/strings."""
    if budget is None:
        budget = [16000, False]
    if budget[0] <= 0 or depth > 7:
        budget[1] = True
        return None
    budget[0] -= 8
    if value is None or isinstance(value, bool) or finite(value):
        return value
    if isinstance(value, str):
        limit = max(0, min(8000, budget[0]))
        result = clean_text(value, limit)
        if len(value) > limit:
            budget[1] = True
        budget[0] -= len(result)
        return result
    if isinstance(value, list):
        if len(value) > 128:
            budget[1] = True
        result = []
        for item in value[:128]:
            if budget[0] <= 0:
                budget[1] = True
                break
            result.append(bounded_tree(item, budget, depth + 1))
        return result
    if isinstance(value, dict):
        if value.get('type') in ('reasoning', 'thinking', 'redacted_thinking', 'analysis') or value.get('channel') == 'analysis':
            return {'omitted': 'private_reasoning'}
        if value.get('type') in ('image', 'inputImage', 'input_image', 'image_url', 'localImage', 'audio', 'inputAudio'):
            return {'type': value['type'], 'body_omitted': True}
        result = {}
        if len(value) > 64:
            budget[1] = True
        for key, item in list(value.items())[:64]:
            if re.search(r'(?i)api.?key|password|secret|credential|authorization|access.?token|refresh.?token|^token$|^key$|private|reasoning|encrypted|context_summary|system_prompt|^thinking$|^analysis$|^(?:image|audio|binary|data|base64)(?:_|$)', str(key)) and key not in ('reasoningOutputTokens', 'reasoning_output_tokens', 'thinking_tokens'):
                continue
            if budget[0] <= 0:
                budget[1] = True
                break
            label = clean_text(key, 100)
            budget[0] -= len(label)
            result[label] = bounded_tree(item, budget, depth + 1)
        return result
    return None


def canonical_tool(name):
    return str(name or 'unknown').removeprefix('mcp__celeste__').removeprefix('celeste_')


def text_blocks(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block['text'] for block in content if isinstance(block, dict)
                and block.get('type') in ('text', 'inputText', 'input_text') and isinstance(block.get('text'), str)]
    return []


def json_objects(text):
    """Read JSON objects embedded in a public game-tool response, with a scan budget."""
    text = text[:262144]
    decoder = json.JSONDecoder()
    objects, offset, attempts = [], 0, 0
    while len(objects) < 16 and attempts < 64:
        offset = text.find('{', offset)
        if offset < 0:
            break
        attempts += 1
        try:
            value, end = decoder.raw_decode(text, offset)
            if isinstance(value, dict):
                objects.append(value)
            offset = end
        except ValueError:
            offset += 1
    return objects


def game_state(data):
    if not isinstance(data, dict):
        return {}
    state = {key: data[key] for key in ('frame', 'scene', 'sid', 'chapter', 'room', 'controllable', 'dreamDash', 'dreaming') if key in data}
    level = data.get('level') if isinstance(data.get('level'), dict) else {}
    state.update({key: level[key] for key in ('sid', 'room', 'mode', 'side', 'completed', 'timeTicks', 'chapterTimeTicks', 'deaths', 'roomDeaths', 'deathsInRoom', 'flags', 'dreaming', 'dreamDash') if key in level})
    player = data.get('player')
    if isinstance(player, dict):
        state['player'] = {key: player[key] for key in ('x', 'y', 'vx', 'vy', 'pos', 'position', 'speed', 'state', 'stateId', 'dashes', 'maxDashes', 'stamina', 'onGround', 'dead', 'justRespawned', 'canDash', 'inControl', 'facing', 'ducking', 'dreamDash') if key in player}
    objects = data.get('objects', data.get('entities', []))
    if isinstance(data.get('collision'), dict):
        objects = data['collision'].get('objects', objects)
    if isinstance(objects, list):
        dreams = [{key: obj[key] for key in ('id', 'type', 'x', 'y', 'w', 'h', 'active') if key in obj}
                  for obj in objects if isinstance(obj, dict) and obj.get('type') in ('dream_block', 'DreamBlock')][:64]
        if dreams:
            state['dream_blocks'] = dreams
    return bounded_tree(state, [12000, False]) if state else {}


def public_game_state(text):
    """Parse only explicit fields from the controller's public Chinese observation format."""
    state = {}
    headers = list(re.finditer(r'\[帧\s+(\d+)\]\s*([^\n]*)', text))
    if headers:
        header = headers[-1]
        text = text[header.start():]
        state['frame'] = int(header[1])
        room = re.search(r'房间\s+([^\s·]+)', header[2])
        if room:
            state['room'] = room[1]
            state['chapter_label'] = header[2].split('·')[0].strip()
        deaths = re.search(r'死亡\s+(\d+)', header[2])
        if deaths:
            state['deaths'] = int(deaths[1])
    match = re.search(r'Madeline[：:]\s*位置\s*\(([-\d.]+),\s*([-\d.]+)\)\s*速度\s*\(([-\d.]+),\s*([-\d.]+)\)\s*状态\s+(\S+)', text)
    if match:
        try:
            state['player'] = dict(zip(('x', 'y', 'vx', 'vy'), map(float, match.groups()[:4])))
            state['player']['state'] = match[5]
        except ValueError:
            pass
    return state


def prediction_result(result):
    """Typed prediction summary; never enters the real-observation collection."""
    projected = {key: result[key] for key in ('stop', 'error', 'detail', 'events', 'warnings') if key in result}
    frames = result.get('frames')
    projected['frames'] = len(frames) if isinstance(frames, list) else frames
    last = result.get('last_frame', frames[-1] if isinstance(frames, list) and frames else None)
    if isinstance(last, dict):
        projected['last_frame'] = {key: last[key] for key in ('f', 'frame', 'x', 'y', 'vx', 'vy', 'pos', 'speed', 'state', 'st', 'dash', 'dashes', 'gnd', 'dead', 'room') if key in last}
    elif isinstance(last, list):
        projected['last_frame'] = last[:16]
    return projected


def project_tool(name, arguments, content, success=None):
    """Project tool-owned fields only; callers must first select an actual tool record."""
    tool = canonical_tool(name)
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if len(arguments) <= 262144 else None
        except ValueError:
            arguments = {'command': arguments} if tool.lower() in ('bash', 'shell', 'exec_command') else None
    allowed = ('inputs', 'candidates', 'plan_file', 'request_id', 'input_format', 'stop_on', 'detail', 'region', 'offset', 'limit',
               'snapshot_id', 'path', 'file_path', 'command', 'cmd', 'code', 'cell_id', 'cwd', 'workdir', 'timeout_ms', 'timeout_s',
               'pattern', 'glob', 'include', 'ignore_case', 'output_mode', 'action', 'choice', 'thought', 'reason', 'effort')
    params = {key: arguments[key] for key in allowed if key in arguments} if isinstance(arguments, dict) else None
    parameter_budget = [16000, False]
    parameters = bounded_tree(params, parameter_budget)
    texts = text_blocks(content)
    original_chars = sum(len(text) for text in texts)
    probe = '\n'.join(text[:262144] for text in texts[:16])[:262144]
    game = tool in ('act', 'observe', 'simulate', 'cutscene', 'travel', 'exit_level')
    parsed = json_objects(probe) if game else []
    structured, states = {}, []
    for data in parsed:
        useful = ('planned', 'consumed', 'stop', 'events', 'input_cursor', 'auto_frames', 'auto_notes', 'game_s',
                  'error', 'message', 'cost', 'receipt', 'energy', 'balance', 'capacity', 'info', 'status', 'trajectory_file', 'results_total')
        structured.update({key: data[key] for key in useful if key in data})
        if isinstance(data.get('results'), list):
            structured['results'] = [prediction_result(result) for result in data['results'][:32] if isinstance(result, dict)]
        if tool != 'simulate':
            for key, value in [('observed', data), ('before', data.get('before')), ('after', data.get('after'))]:
                state = game_state(value)
                if state:
                    states.append({'kind': key, 'evidence': 'structured_result', 'state': state})
    if game:
        state = public_game_state(probe) if tool != 'simulate' else {}
        if state:
            states.append({'kind': 'observed', 'evidence': 'parsed_public_result_text', 'state': state})
        executed = re.search(r'执行了\s*(\d+)\s*/\s*(\d+)\s*帧', probe)
        if executed:
            structured.setdefault('consumed', int(executed[1]))
            structured.setdefault('planned', int(executed[2]))
        stop = re.search(r'停止原因[：:]\s*([^\n]+)', probe)
        if stop:
            structured.setdefault('stop_label', stop[1])
        # JSON-only payloads are represented by their explicit projection, not by
        # a second raw text copy which could bypass the field allowlist.
        excerpt = probe.split('{', 1)[0] if parsed else probe
    else:
        excerpt = probe
        if texts:
            try:
                structured = json.loads(texts[0])
                excerpt = ''
            except ValueError:
                pass
    sensitive_path = isinstance(arguments, dict) and re.search(r'(?i)(?:^|[/\\])(?:\.env(?:\.[^/\\]*)?|auth\.json|credentials[^/\\]*|[^/\\]+\.(?:key|pem))$', str(arguments.get('path', arguments.get('file_path', ''))))
    result_budget = [24000, False]
    result = {'structured': bounded_tree(structured, result_budget) if structured else None,
              'text_excerpt': clean_text(excerpt, 8000) if content is not None and not sensitive_path else None}
    if sensitive_path:
        result = {'omitted': 'sensitive_file'}
    if isinstance(arguments, dict) and isinstance(arguments.get('content'), str) and not sensitive_path:
        parameters['content_chars'] = len(arguments['content'])
        parameters['content_sha256'] = hashlib.sha256(arguments['content'].encode()).hexdigest()
    return {'tool': str(name), 'parameters': parameters,
            'parameters_meta': {'available': arguments is not None, 'truncated': parameter_budget[1], 'source': 'tool_record',
                                'sensitive_file_access': bool(sensitive_path),
                                'original_chars': len(json.dumps(params, ensure_ascii=False)), 'kept_chars': len(json.dumps(parameters, ensure_ascii=False))},
            'success': success if isinstance(success, bool) else None,
            'result': result if content is not None else None,
            'result_meta': {'available': content is not None, 'original_text_chars': original_chars,
                            'kept_excerpt_chars': len(result.get('text_excerpt') or ''),
                            'truncated': original_chars > 8000 or result_budget[1], 'attachment_bodies_omitted': True},
            'game_states': states, 'error': clean_text(result.get('text_excerpt') or json.dumps(result.get('structured'), ensure_ascii=False), 2000) if success is False else None}


def project_native_tool(item, payload):
    kind = item.get('type')
    if kind not in ('DynamicToolCall', 'CommandExecution', 'FileChange', 'McpToolCall', 'ImageView'):
        return None
    name, args, content = item.get('tool'), item.get('arguments'), item.get('content_items')
    success = item.get('success')
    extra = {}
    if kind == 'CommandExecution':
        name, args = 'Bash', native_shell_args(item)
        content = item.get('aggregated_output', item.get('aggregatedOutput'))
        code = item.get('exit_code', item.get('exitCode'))
        success = code == 0 if type(code) is int else None
        extra['exit_code'] = code
    elif kind == 'FileChange':
        name, content = 'apply_patch', item.get('stdout') or item.get('stderr')
        changes = item.get('changes')
        args = {'path': [change.get('path') for change in changes[:128] if isinstance(change, dict)]} if isinstance(changes, list) else None
        extra['file_changes'] = bounded_tree([{key: change[key] for key in ('path', 'kind', 'status', 'added', 'removed') if key in change}
                                              for change in changes[:128] if isinstance(change, dict)]) if isinstance(changes, list) else None
    elif kind == 'ImageView':
        name, args, content = 'view_image', {'path': item.get('path')}, None
    if item.get('status') in ('failed', 'declined') or item.get('error') is not None:
        success = False
    elif item.get('status') == 'completed' and success is None and kind != 'DynamicToolCall':
        success = True
    row = project_tool(name or kind, args, content, success)
    if item.get('error') is not None:
        row['error'] = bounded_tree(item['error'], [2000, False])
    duration = item.get('duration') or {}
    seconds = (duration['secs'] + duration['nanos'] / 1e9 if isinstance(duration, dict) and finite(duration.get('secs'))
               and finite(duration.get('nanos')) and duration['secs'] >= 0 and 0 <= duration['nanos'] < 1e9 else None)
    started, ended = payload.get('started_at_ms'), payload.get('completed_at_ms')
    row.update(call_id=item.get('id'), native_turn_id=payload.get('turn_id'), item_type=kind,
               started_wall=started / 1000 if finite(started) else None, ended_wall=ended / 1000 if finite(ended) else None,
               runtime_duration_s=seconds, **extra)
    return row


def iso_epoch(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, ValueError, OverflowError, OSError):
        return None
