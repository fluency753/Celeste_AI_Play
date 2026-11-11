"""Read-only behavioral analysis over persisted controller and runtime evidence."""
from collections import Counter, defaultdict
from bisect import bisect_right
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .session_export import SessionSourceRows, timestamp, TIME_FIELDS
from .session_export_spool import ExportSpool, SpilledDict, SpoolRows
from .session_export_projection import bounded_tree, canonical_tool, clean_text, finite, iso_epoch, project_tool


class AnalysisReport(dict):
    """Legacy JSON remains a dict; large analysis records are written to referenced files."""
    def __init__(self, report, records, store):
        super().__init__(report)
        self.analysis_records = records
        self.detail_store = store

    def close(self):
        self.detail_store.close()


def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def wall(value):
    return value if timestamp(value) else None


def null_nonfinite(value):
    """Normalize malformed source numbers in place, without clipping index arrays."""
    count = 0
    if isinstance(value, float) and not math.isfinite(value):
        return None, 1
    if isinstance(value, (dict, list)):
        for key in list(value) if isinstance(value, dict) else range(len(value)):
            if isinstance(value, SpilledDict) and key in value.references:
                continue  # Spool append already validates JSON with allow_nan=False.
            value[key], replaced = null_nonfinite(value[key])
            count += replaced
    return value, count


def window_status(start, end, since, until):
    """Known intersections retain their boundary evidence; unknown intervals stay labeled."""
    if since is None and until is None:
        return 'unfiltered'
    if start is None:
        if end is not None and (since is None or end >= since) and (until is None or end < until):
            return 'end_observed_in_window'
        return 'time_unknown'
    if until is not None and start >= until:
        return 'outside'
    if end is None:
        return 'within_window' if since is None or start >= since else 'time_unknown'
    if since is not None and end < since:
        return 'outside'
    if since is not None and end == since and start < since:
        return 'outside'
    if (since is not None and start < since) or (until is not None and end >= until):
        return 'overlaps_window'
    return 'within_window'


class Analysis:
    def __init__(self, report, chain, snapshots, native, since=None, until=None):
        self.report, self.chain, self.snapshots, self.native = report, chain, snapshots, native or {}
        self.since, self.until = since, until
        self.session_id = report['session']['session_id']
        self.tools, self.turns, self.events, self.actions, self.states, self.metrics = {}, {}, [], [], [], []
        self.warnings, self.sources = [], []
        self.run_ids = {str(directory): stable_id(self.session_id, directory.as_posix())
                        for directory, _ in chain}
        self.native_turns = defaultdict(set)
        self.call_runs = defaultdict(set)
        self.public_actions = {}
        self.parameter_priority = {}
        self.result_priority = {}
        self.detail_store = ExportSpool()
        self.source_rows = SessionSourceRows(chain)

    def read(self, directory, name):
        path = directory / name
        if path.exists():
            self.sources.append({'path': str(path), 'size_at_capture': path.stat().st_size})
        return self.source_rows.read(path, self.warnings, line_limit=32 * 1024 * 1024)

    def context(self, directory):
        return {'session_id': self.session_id, 'run_id': self.run_ids[str(directory)], 'run': str(directory)}

    def event(self, context, kind, moment, source, index, data=None, tool_id=None, turn_id=None):
        row = SpilledDict(self.detail_store, {**context, 'event_id': stable_id(context.get('run_id'), str(source), index, kind),
               'kind': kind, 'wall': wall(moment), 'time': timestamp(moment), 'tool_id': tool_id,
               'turn_id': turn_id, 'source': str(source), 'record_index': index, 'details_omitted': False})
        row.spill({'data': bounded_tree(data or {}, [6000, False])})
        self.events.append(row)
        return row

    def tool(self, directory, ident, name=None):
        context = self.context(directory) if directory is not None else {'session_id': self.session_id, 'run_id': None, 'run': None}
        key = stable_id(context['run_id'], self.session_id, 'tool', ident)
        if key not in self.tools:
            self.tools[key] = SpilledDict(self.detail_store, {**context, 'tool_id': key, 'call_id': ident, 'tool': name or None,
                               'turn_id': None, 'native_turn_id': None, 'requested_wall': None, 'result_wall': None,
                               'requested_time_source': None, 'result_time_source': None,
                               'timing_ended_wall': None, 'active_s': None, 'runtime_duration_s': None,
                               'thinking_before_s': None, 'thinking_interval_start_wall': None,
                               'parameters': None, 'parameters_meta': {'available': False}, 'result': None,
                               'result_meta': {'available': False}, 'success': None, 'sources': []})
        row = self.tools[key]
        if name and not row['tool']:
            row['tool'] = str(name)
        if context['run_id'] is not None:
            self.call_runs[ident].add(str(directory))
        return row

    def provenance(self, row, source, index, kind):
        ref = {'path': str(source), 'record_index': index, 'kind': kind}
        if len(row['sources']) >= 32:
            row['source_references_truncated'] = True
            return
        if ref not in row['sources']:
            row['sources'].append(ref)

    def apply(self, row, projected, source, index, priority):
        ident = row['tool_id']
        if projected.get('tool') and (not row['tool'] or priority >= 3):
            row['tool'] = projected['tool']
        if projected.get('parameters') is not None and priority >= self.parameter_priority.get(ident, 0):
            row.spill({'parameters': projected['parameters']})
            row['parameters_meta'] = projected.get('parameters_meta', {'available': True})
            self.parameter_priority[ident] = priority
        if projected.get('result') is not None and priority >= self.result_priority.get(ident, 0):
            row.spill({'result': projected['result']})
            row['result_meta'] = projected.get('result_meta', {'available': True})
            self.result_priority[ident] = priority
        if isinstance(projected.get('success'), bool) and priority >= row.get('_success_priority', 0):
            row['success'], row['_success_priority'] = projected['success'], priority
        for key in ('runtime_duration_s', 'exit_code', 'file_changes', 'error', 'details_omitted', 'item_type'):
            if projected.get(key) is not None or key == 'exit_code' and key in projected:
                if key in ('file_changes', 'error'):
                    row.spill({key: projected[key]})
                else:
                    row[key] = projected[key]
        if projected.get('native_turn_id'):
            row['native_turn_id'] = projected['native_turn_id']
        if row['requested_wall'] is None and wall(projected.get('started_wall')):
            row['requested_wall'] = projected['started_wall']
            row['requested_time_source'] = 'native.response_item.timestamp' if projected.get('item_type') == 'CodeModeWrapper' else 'native.started_at_ms'
        if wall(projected.get('ended_wall')):
            row['result_wall'] = projected['ended_wall']
            row['result_time_source'] = 'native.response_item.timestamp' if projected.get('item_type') == 'CodeModeWrapper' else 'native.completed_at_ms'
        # Keep the most informative state projection, rather than duplicating
        # local/native copies of the same returned snapshot.
        if projected.get('game_states') and priority >= row.get('_states_priority', 0):
            row.spill({'_state_facts': projected['game_states']})
            row['_states_priority'] = priority
        self.provenance(row, source, index, 'native_tool' if priority == 4 else 'runtime_tool' if priority == 3 else 'safe_summary')

    def timings(self, directory):
        context = self.context(directory)
        running, boundary = defaultdict(set), {}
        current_turn_ids = {}
        begun_turn_ids = set()
        for index, event in self.read(directory, 'decision_timing.jsonl'):
            number = event.get('turn')
            kind, moment = event.get('event'), wall(event.get('wall'))
            turn_id = current_turn_ids.setdefault(number, stable_id(context['run_id'], 'controller_turn', number)) if number is not None else None
            if kind == 'turn_requested' and turn_id in begun_turn_ids:
                # A controller restarted in the same run directory may reset its
                # local counter. Preserve its earlier acknowledged turn and tools.
                turn_id = stable_id(context['run_id'], 'controller_turn', number, 'request', index)
                current_turn_ids[number] = turn_id
            if turn_id is not None and kind in ('turn_requested', 'turn_acknowledged'):
                begun_turn_ids.add(turn_id)
            turn = self.turns.setdefault(turn_id, {**context, 'turn_id': turn_id, 'controller_turn': number,
                                                   'native_turn_id': None, 'requested_wall': None, 'ended_wall': None,
                                                   'active_s': None, 'thinking_before_tools_s': 0.0}) if turn_id else {}
            ident = event.get('tool_id')
            tool = self.tool(directory, ident, str(event.get('tool', '')).split(' · ')[0]) if isinstance(ident, str) else None
            if kind == 'turn_requested':
                turn.update(requested_wall=moment,
                            requested_configuration={key: event.get(key) for key in ('runtime', 'model', 'effort', 'fast_requested', 'service_tier_requested')})
                boundary[turn_id] = moment
            elif kind == 'turn_acknowledged' and isinstance(event.get('turn_id'), str):
                turn['native_turn_id'] = event['turn_id']
                turn['acknowledged_wall'] = moment
                turn['request_ack_s'] = event.get('request_ack_s')
                self.native_turns[event['turn_id']].add((str(directory), turn_id))
            elif kind == 'turn_ended':
                turn.update(ended_wall=moment, active_s=event.get('active_s'), pending_tools=event.get('pending_tools'))
                for pending in event.get('pending_tools') or []:
                    self.tool(directory, pending)['interrupted_by_turn_end'] = True
            elif kind == 'tool_requested' and tool:
                tool.update(requested_wall=moment, turn_id=turn_id, game_tool=event.get('game_tool'),
                            requested_time_source='controller.tool_requested',
                            thinking_before_s=event.get('decision_wait_s'),
                            thinking_interval_start_wall=boundary.get(turn_id) if not running[turn_id] else None)
                running[turn_id].add(ident)
                if finite(event.get('decision_wait_s')):
                    turn['thinking_before_tools_s'] += event['decision_wait_s']
                self.provenance(tool, directory / 'decision_timing.jsonl', index, 'controller_timing')
            elif kind == 'tool_completed' and tool:
                tool.update(timing_ended_wall=moment, active_s=event.get('tool_active_s'))
                running[turn_id].discard(ident)
                if not running[turn_id]:
                    boundary[turn_id] = moment
                self.provenance(tool, directory / 'decision_timing.jsonl', index, 'controller_timing')
            allowed = ('tool_id', 'tool', 'game_tool', 'playing', 'decision_wait_s', 'decision_active_s',
                       'success', 'result', 'request_ack_s', 'active_s', 'pending_tools', 'turn_id', 'reason', 'outcome')
            self.event(context, 'controller.' + str(kind), moment, directory / 'decision_timing.jsonl', index,
                       {key: event[key] for key in allowed if key in event}, tool['tool_id'] if tool else None, turn_id)

    def codex(self, directory):
        context = self.context(directory)
        names = {'commandExecution': 'Bash', 'fileChange': 'apply_patch', 'imageView': 'view_image'}
        for index, event in self.read(directory, 'codex_events.jsonl'):
            params = event.get('params') if isinstance(event.get('params'), dict) else {}
            if params.get('threadId') not in (None, self.session_id):
                continue
            method, moment = event.get('method', ''), wall(event.get('wall'))
            item = params.get('item') if isinstance(params.get('item'), dict) else {}
            fact = params.get('fact') if isinstance(params.get('fact'), dict) else {}
            source = directory / 'codex_events.jsonl'
            if method == 'tool/history' and isinstance(fact.get('id'), str):
                row = self.tool(directory, fact['id'], fact.get('tool'))
                if fact.get('phase') == 'requested':
                    parameters = fact.get('parameters')
                    self.apply(row, {'parameters': bounded_tree(parameters) if isinstance(parameters, dict) else None,
                                     'parameters_meta': {'available': parameters is not None, 'source': 'runtime_safe_summary', 'already_summarized': True}}, source, index, 1)
                    row['requested_wall'] = row['requested_wall'] or moment
                    row['requested_time_source'] = row['requested_time_source'] or 'safe_summary.requested.wall'
                elif fact.get('phase') == 'returned':
                    self.apply(row, {'result': {'runtime_summary': bounded_tree(fact.get('result'))},
                                     'result_meta': {'available': False, 'summary_available': fact.get('result') is not None, 'already_summarized': True}}, source, index, 1)
                    row['result_wall'] = moment
                    row['result_time_source'] = 'safe_summary.returned.wall'
                    if isinstance(fact.get('success'), bool):
                        row['success'], row['_success_priority'] = fact['success'], 1
                row['native_turn_id'] = params.get('turnId') or row['native_turn_id']
                self.provenance(row, source, index, 'safe_summary')
            elif method in ('item/started', 'item/completed') and item.get('type') in ('dynamicToolCall', 'commandExecution', 'fileChange', 'mcpToolCall', 'imageView') and isinstance(item.get('id'), str):
                ident, kind = item['id'], item['type']
                if params.get('threadId') is None and str(directory) not in self.call_runs.get(ident, set()):
                    self.warnings.append(f'{directory}: 未关联到本次计时的无会话身份工具记录已跳过')
                    continue
                row = self.tool(directory, ident, item.get('tool') or names.get(kind) or 'MCP')
                row['native_turn_id'] = params.get('turnId') or row['native_turn_id']
                stamp = params.get('startedAtMs') if method == 'item/started' else params.get('completedAtMs')
                instant = stamp / 1000 if finite(stamp) else moment
                if method == 'item/started':
                    row['requested_wall'] = row['requested_wall'] or instant
                    row['requested_time_source'] = row['requested_time_source'] or ('runtime.startedAtMs' if finite(stamp) else 'runtime.event.wall')
                else:
                    row['result_wall'] = instant
                    row['result_time_source'] = 'runtime.completedAtMs' if finite(stamp) else 'runtime.event.wall'
                if finite(item.get('durationMs')):
                    row['runtime_duration_s'] = item['durationMs'] / 1000
                if kind != 'dynamicToolCall':
                    args = item.get('arguments')
                    content = item.get('content')
                    success = item.get('success')
                    if kind == 'commandExecution':
                        args = {key: item[key] for key in ('command', 'cwd') if key in item}
                        content = item.get('aggregatedOutput') if method == 'item/completed' else None
                        if type(item.get('exitCode')) is int:
                            success = item['exitCode'] == 0
                            row['exit_code'] = item['exitCode']
                    elif kind == 'fileChange':
                        changes = item.get('changes') or []
                        changes = [{key: entry[key] for key in ('path', 'kind', 'status') if key in entry}
                                   for entry in changes if isinstance(entry, dict)]
                        args = {'path': [entry.get('path') for entry in changes if isinstance(entry, dict)]}
                    elif kind == 'imageView':
                        args = {'path': item.get('path')}
                    if item.get('status') in ('failed', 'declined') or item.get('error') is not None:
                        success = False
                    elif method == 'item/completed' and item.get('status') == 'completed' and success is None:
                        success = True
                    projected = project_tool(row['tool'], args, content, success)
                    if kind == 'fileChange':
                        projected['file_changes'] = bounded_tree(changes)
                    if item.get('error') is not None:
                        projected['error'] = bounded_tree(item['error'], [2000, False])
                    self.apply(row, projected, source, index, 3)
                elif isinstance(item.get('success'), bool):
                    row['success'], row['_success_priority'] = item['success'], 2
                self.provenance(row, source, index, 'runtime_item')
                if kind == 'dynamicToolCall' and item.get('error') is not None:
                    self.apply(row, {'error': bounded_tree(item['error'], [2000, False])}, source, index, 2)
            elif item.get('type') == 'contextCompaction' and method in ('item/started', 'item/completed'):
                self.event(context, 'compaction.' + method.split('/')[1], moment, source, index,
                           {'item_id': item.get('id'), 'native_turn_id': params.get('turnId')})
            elif method == 'thread/tokenUsage/updated':
                usage = params.get('tokenUsage') if isinstance(params.get('tokenUsage'), dict) else {}
                counts = {scope: {key: value for key, value in (usage.get(scope) or {}).items() if finite(value)}
                          for scope in ('last', 'total') if isinstance(usage.get(scope), dict)}
                self.event(context, 'tokens', moment, source, index,
                           {'native_turn_id': params.get('turnId'), 'counts': counts, 'context_window': usage.get('modelContextWindow')})
            elif method in ('error', 'warning', 'thread/status/changed', 'turn/started', 'turn/completed'):
                value = params.get('turn') or {}
                data = {key: params[key] for key in ('message', 'error', 'status', 'willRetry', 'turnId') if key in params}
                if isinstance(value, dict):
                    data['turn'] = {key: value[key] for key in ('id', 'status', 'error') if key in value}
                    native_id = value.get('id')
                    if method.startswith('turn/') and isinstance(native_id, str):
                        matches = [turn_id for owner, turn_id in self.native_turns.get(native_id, set()) if owner == str(directory)]
                        turn_id = matches[0] if len(matches) == 1 else stable_id(context['run_id'], 'native_turn', native_id)
                        turn = self.turns.setdefault(turn_id, {**context, 'turn_id': turn_id, 'controller_turn': None,
                                                               'native_turn_id': native_id, 'requested_wall': None,
                                                               'ended_wall': None, 'active_s': None, 'thinking_before_tools_s': None})
                        self.native_turns[native_id].add((str(directory), turn_id))
                        target = 'requested_wall' if method == 'turn/started' else 'ended_wall'
                        turn[target] = turn[target] if turn.get(target) is not None else moment
                        turn['runtime_status'] = value.get('status')
                self.event(context, 'runtime.' + method, moment, source, index, data)

    def claude(self, directory):
        context = self.context(directory)
        for index, event in self.read(directory, 'agent_events.jsonl'):
            source = directory / 'agent_events.jsonl'
            moment = wall(event.get('wall')) or iso_epoch(event.get('timestamp'))
            message = event.get('message') if isinstance(event.get('message'), dict) else {}
            for block in message.get('content') or []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and isinstance(block.get('id'), str):
                    row = self.tool(directory, block['id'], block.get('name'))
                    row['requested_wall'] = row['requested_wall'] or moment
                    row['requested_time_source'] = row['requested_time_source'] or 'claude.message.timestamp'
                    projected = project_tool(row['tool'], block.get('input'), None)
                    row['_sensitive_file_access'] = projected['parameters_meta'].get('sensitive_file_access', False)
                    self.apply(row, projected, source, index, 3)
                elif block.get('type') == 'tool_result' and isinstance(block.get('tool_use_id'), str):
                    row = self.tool(directory, block['tool_use_id'])
                    row['result_wall'] = moment
                    row['result_time_source'] = 'claude.message.timestamp'
                    # Request and result are separate protocol messages. Keep the
                    # sensitive-file guard even when its parameter detail was omitted.
                    args = {'path': '.env'} if row.get('_sensitive_file_access') else None
                    projected = project_tool(row['tool'], args, block.get('content'), block.get('is_error') is not True)
                    projected.pop('parameters', None)
                    self.apply(row, projected, source, index, 3)
            if event.get('type') == 'result':
                self.event(context, 'runtime.claude_result', moment, source, index,
                           {'is_error': event.get('is_error'), 'duration_ms': event.get('duration_ms'),
                            'duration_api_ms': event.get('duration_api_ms'), 'num_turns': event.get('num_turns'),
                            'usage': {key: value for key, value in (event.get('usage') or {}).items() if finite(value)}})
            elif event.get('type') == 'system' and event.get('subtype') in ('compact_boundary', 'status'):
                self.event(context, 'runtime.claude_' + event['subtype'], moment, source, index,
                           {'status': event.get('status'), 'compact_metadata': {key: value for key, value in (event.get('compact_metadata') or {}).items() if finite(value)}})

    def deliveries(self, directory):
        context = self.context(directory)
        for index, event in self.read(directory, 'tool_deliveries.jsonl'):
            if event.get('thread') not in (None, self.session_id):
                continue
            ident = event.get('call_id')
            row = self.tool(directory, ident, event.get('tool')) if isinstance(ident, str) else None
            allowed = ('receipt', 'event', 'path', 'file', 'truncated', 'next_offset', 'warnings', 'text_chars', 'images', 'item_id', 'proof', 'reason')
            data = {key: event[key] for key in allowed if key in event}
            if row:
                receipts = row.get('deliveries', {})
                receipt = str(event.get('receipt') or 'observation')
                projected = bounded_tree(data)
                if receipt in receipts or len(receipts) < 64:
                    previous = receipts.setdefault(receipt, {})
                    if previous.get('event') != 'recorded' or event.get('event') == 'recorded':
                        previous.update(projected, wall=wall(event.get('wall')))
                else:
                    row['delivery_index_truncated'] = True
                row.spill({'deliveries': receipts})
                self.provenance(row, directory / 'tool_deliveries.jsonl', index, 'delivery_receipt')
            self.event(context, 'delivery.' + str(event.get('event')), event.get('wall'), directory / 'tool_deliveries.jsonl', index,
                       {key: data[key] for key in ('receipt', 'event', 'truncated', 'next_offset', 'text_chars', 'item_id', 'proof') if key in data},
                       row['tool_id'] if row else None)

    def controls(self, directory):
        db = directory / 'control.sqlite3'
        if not db.exists():
            return
        try:
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                copied_actions = self.source_rows.copied_actions(directory, self.warnings)
                for entry in connection.execute('SELECT * FROM actions ORDER BY id'):
                    entry = dict(entry)
                    if self.source_rows.action_key(entry) in copied_actions:
                        continue
                    if entry.get('kind') == 'message':
                        continue
                    self.event(self.context(directory), 'control.requested', entry.get('wall'), str(db) + '#actions', entry.get('id'),
                               {key: entry.get(key) for key in ('id', 'kind', 'text', 'status', 'detail')}
                               | {'time_semantics': 'request_time; status is captured latest, not an application timestamp'})
                try:
                    captured = connection.execute('SELECT data FROM state WHERE id=1').fetchone()
                    state = json.loads(captured[0]) if captured else {}
                    if isinstance(state, dict):
                        self.event(self.context(directory), 'control.snapshot', state.get('wall'), str(db) + '#state', 1,
                                   {key: state.get(key) for key in ('phase', 'paused', 'chat_only', 'connected', 'wall')})
                except (sqlite3.Error, ValueError):
                    pass
        except sqlite3.Error as exc:
            self.warnings.append(f'{db}: {exc}')

    def decisions(self, directory):
        context = self.context(directory)
        for index, event in self.read(directory, 'public_events.jsonl'):
            if isinstance(event.get('id'), str) and event['id'].startswith('act:'):
                self.public_actions.setdefault((context['run_id'], event['id']), wall(event.get('wall')))
        for index, entry in self.read(directory, 'decisions.jsonl'):
            public_id = f"act:{entry.get('i')}:{entry.get('attempt', 0)}"
            action = SpilledDict(self.detail_store, {**context, 'action_id': stable_id(context['run_id'], public_id), 'public_event_id': public_id,
                      'public_event_wall': self.public_actions.get((context['run_id'], public_id)),
                      'call_id': None, 'source': str(directory / 'decisions.jsonl'), 'record_index': index,
                      'elapsed_s': entry.get('t'), 'i': entry.get('i'), 'attempt': entry.get('attempt', 0)})
            budget = [24000, False]
            detail = bounded_tree({key: entry[key] for key in ('effort', 'thought', 'inputs', 'plan_file',
                         'input_format', 'stop_on', 'stop', 'planned', 'consumed', 'events', 'game_s', 'auto_frames',
                         'decision_wait_s', 'input_cursor', 'deviation') if key in entry}, budget)
            action.spill(detail or {})
            action['details_truncated'] = budget[1]
            action['details_omitted'] = False
            action['input_fingerprint'] = stable_id(entry.get('inputs'), entry.get('plan_file'), entry.get('input_format'), entry.get('stop_on'))
            self.actions.append(action)
            self.event(context, 'action.public_mention', action['public_event_wall'], directory / 'decisions.jsonl', index,
                       {'action_id': action['action_id'], 'public_event_id': public_id,
                        'time_semantics': 'public action announcement; not an execution completion time'})

    def token_advances(self, directory):
        context = self.context(directory)
        source = directory / 'token_advances.jsonl'
        from .session_export_projection import game_state
        for index, entry in self.read(directory, source.name):
            kind, batch = entry.get('event'), entry.get('batch') or {}
            row = None
            if kind in ('requested', 'committed', 'unknown') and type(batch.get('id')) is int:
                row = self.tool(directory, 'token-batch:' + str(batch['id']), 'system.token_advance')
                row['item_type'] = 'SystemTokenAdvance'
                if kind == 'requested':
                    row['requested_wall'] = wall(entry.get('wall'))
                    row['requested_time_source'] = 'token_advances.requested.wall'
                    row.spill({'parameters': {'frames': batch.get('requested'), 'before': batch.get('before'),
                                               'origin': batch.get('origin')}})
                    row['parameters_meta'] = {'available': True}
                else:
                    row['result_wall'] = wall(entry.get('wall'))
                    row['result_time_source'] = 'token_advances.' + str(kind) + '.wall'
                    row.spill({'result': batch})
                    row['result_meta'] = {'available': True}
                    row['success'] = True if kind == 'committed' else None
                    row['execution_unknown'] = kind == 'unknown'
                    if kind == 'committed' and isinstance(batch.get('after'), dict):
                        row.spill({'_state_facts': [{'kind': 'after', 'evidence': 'token_advances.committed.after',
                                                   'state': game_state(batch['after'])}]})
                self.provenance(row, source, index, 'system_advance')
            self.event(context, 'token.' + str(kind), entry.get('wall'), source, index,
                       entry, tool_id=row['tool_id'] if row is not None else None)

    def snapshot(self, directory, state):
        context = self.context(directory)
        canonical, data = self.snapshots[directory]
        paths = [canonical]
        if (directory / 'summary.json').exists() and canonical != directory / 'summary.json':
            paths.append(directory / 'summary.json')
        for path in paths:
            if path != canonical:
                try:
                    data = json.loads(path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError):
                    continue
            if not isinstance(data, dict):
                continue
            fields = (*TIME_FIELDS, 'decisions', 'deaths', 'frames_played', 'frames_auto', 'sim_calls', 'sim_rollouts',
                      'tool_calls', 'compactions', 'retries', 'output_tokens', 'thinking_tokens', 'token_frames', 'token_s',
                      'network_retry_episodes')
            row = {**context, 'snapshot_id': stable_id(context['run_id'], path.name, data.get('written_wall')),
                   'wall': wall(data.get('written_wall')), 'source': str(path), 'canonical': path == canonical,
                   'metrics_authoritative': state.get('_export_source_kind', 'run') == 'run',
                   'source_kind': state.get('_export_source_kind', 'run'),
                   'counters': {key: data.get(key) for key in fields},
                   'game_clock': bounded_tree(data.get('game_clock')),
                   'chapter_base': ({key: value for key, value in data['chapter_base'].items() if key in fields and finite(value)}
                                    if isinstance(data.get('chapter_base'), dict) else None),
                   **{key: bounded_tree(data.get(key)) for key in ('chapter', 'chapter_side', 'room', 'status', 'activity_status',
                      'network_retrying', 'network_retry_since_wall', 'network_retry_reason', 'chapter_time_ticks',
                      'completed_ticks', 'piece_done', 'splits', 'stop_reason', 'completed', 'user_stopped', 'context_tokens', 'context_window', 'token_drift')},
                   'resumed_from': state.get('resumed_from')}
            self.metrics.append(row)
            self.event(context, 'metrics.snapshot', row['wall'], path, 0, {'snapshot_id': row['snapshot_id'], 'status': row['status']})

    def blockages(self, directory):
        context = self.context(directory)
        stopped = directory / 'agent_stop.json'
        if stopped.exists():
            try:
                data = json.loads(stopped.read_text(encoding='utf-8-sig'))
                for index, entry in enumerate(data.get('reports') or [], 1):
                    if isinstance(entry, dict):
                        self.event(context, 'agent.reported_blocked', entry.get('wall'), stopped, index,
                                   {key: entry.get(key) for key in ('reason', 'decision', 'category')})
            except (OSError, ValueError, AttributeError):
                self.warnings.append(f'{stopped}: 无法读取阻塞报告')

        source = 'stop_run_diagnostics.jsonl'
        for index, entry in self.read(directory, source):
            if not isinstance(entry.get('status'), str) or not entry['status']:
                continue
            fields = ('status', 'reason', 'decision', 'category', 'frame', 'control_note', 'step', 'required_steps')
            data = {key: entry.get(key) for key in fields}
            transition = entry.get('transition')
            data['transition'] = ({key: transition.get(key) for key in
                                   ('kind', 'start_frame', 'frame', 'advanced_frames', 'remaining_frames', 'stalled')}
                                  if isinstance(transition, dict) else None)
            self.event(context, 'agent.blockage_attempt', entry.get('wall'), directory / source, index, data)

    def native_lifecycle(self):
        compact_ids = {row['data'].get('item_id') for row in self.events if row['kind'].startswith('compaction.')}
        for entry in self.native.get('lifecycle', []):
            matches = self.native_turns.get(entry.get('native_turn_id'), set())
            if not matches and isinstance(entry.get('native_turn_id'), str):
                native_id = entry['native_turn_id']
                turn_id = stable_id(self.session_id, 'native_turn', native_id)
                self.turns.setdefault(turn_id, {'session_id': self.session_id, 'run_id': None, 'run': None, 'turn_id': turn_id,
                                                'controller_turn': None, 'native_turn_id': native_id,
                                                'requested_wall': None, 'ended_wall': None, 'active_s': None,
                                                'thinking_before_tools_s': None})
                self.native_turns[native_id].add((None, turn_id))
                matches = self.native_turns[native_id]
            directory, turn_id = next(iter(matches)) if len(matches) == 1 else (None, None)
            context = self.context(Path(directory)) if directory else {'session_id': self.session_id, 'run_id': None, 'run': None}
            if entry.get('kind') == 'compaction.completed' and entry.get('item_id') in compact_ids:
                continue
            if turn_id in self.turns:
                turn = self.turns[turn_id]
                target = 'requested_wall' if entry.get('kind') == 'task_started' else 'ended_wall'
                if entry.get('kind') in ('task_started', 'task_complete', 'turn_aborted') and turn.get(target) is None:
                    turn[target] = wall(entry.get('wall'))
                if entry.get('kind') == 'turn_aborted':
                    turn['runtime_status'] = 'aborted'
            self.event(context, 'native.' + str(entry.get('kind')), entry.get('wall'), entry.get('source'), entry.get('record_index'),
                       {key: entry.get(key) for key in ('native_turn_id', 'item_id')}, turn_id=turn_id)

    def native_tools(self):
        for record in self.native.get('tool_calls', []):
            ident = record.get('call_id')
            if not isinstance(ident, str):
                continue
            candidates = set(self.call_runs.get(ident, set()))
            turns = self.native_turns.get(record.get('native_turn_id'), set())
            owners = {directory for directory, _ in turns if isinstance(directory, str)}
            if not candidates and len(owners) == 1:
                candidates = owners
            if candidates and owners:
                candidates &= owners
            directory = Path(next(iter(candidates))) if len(candidates) == 1 else None
            row = self.tool(directory, ident, record.get('tool'))
            if len(candidates) > 1:
                row['run_ownership'] = 'ambiguous'
                self.warnings.append('原生调用 ID 对应多个运行段，未猜测其归属')
            elif directory is None:
                row['run_ownership'] = 'native_session_only'
            if len(turns) == 1 and row.get('turn_id') is None:
                row['turn_id'] = next(iter(turns))[1]
            self.apply(row, record, record.get('source'), record.get('record_index'), 4)

    def finish(self):
        for directory, state in self.chain:
            self.timings(directory)
            self.codex(directory) if state.get('engine') == 'codex' else self.claude(directory)
            self.deliveries(directory)
            self.controls(directory)
            self.decisions(directory)
            self.token_advances(directory)
            self.snapshot(directory, state)
            self.blockages(directory)
        self.native_lifecycle()
        self.native_tools()
        for row in self.tools.values():
            row['schema_version'] = 1
            if row['turn_id'] is None and row['native_turn_id'] is not None:
                matches = [turn_id for owner, turn_id in self.native_turns.get(row['native_turn_id'], set())
                           if owner == row['run']]
                if len(matches) == 1:
                    row['turn_id'] = matches[0]
            if row['native_turn_id'] is None and row.get('turn_id') in self.turns:
                row['native_turn_id'] = self.turns[row['turn_id']].get('native_turn_id')
            row['record_kind'] = ('system_advance' if row.get('item_type') == 'SystemTokenAdvance' else
                                  'code_mode_wrapper' if row.get('item_type') == 'CodeModeWrapper' else 'tool_execution')
            row['status'] = ('execution_unknown' if row.get('execution_unknown') else
                             'failed' if row['success'] is False else 'returned' if row['success'] is True
                             else 'returned_unverified' if row['result_wall'] is not None
                             else 'interrupted_result_missing' if row.get('interrupted_by_turn_end')
                             else 'result_missing' if row['timing_ended_wall'] is not None else 'pending_or_missing')
            if row['requested_wall'] is not None and row['result_wall'] is not None:
                span = row['result_wall'] - row['requested_wall']
                row['wall_span_s'] = span if span >= 0 else None
                if span < 0:
                    row['clock_order_warning'] = True
            else:
                row['wall_span_s'] = None
            for index, fact in enumerate(row.pop('_state_facts', [])):
                state = fact.get('state') or {}
                record = SpilledDict(self.detail_store, {'state_id': stable_id(row['tool_id'], index), 'tool_id': row['tool_id'],
                                    'run_id': row['run_id'], 'run': row['run'], 'session_id': self.session_id,
                                    'projection_index': index,
                                    'wall': row['result_wall'], 'kind': fact.get('kind'), 'evidence': fact.get('evidence'),
                                    'wall_semantics': 'tool result available time; not an inferred state capture time',
                                    'sources': row['sources']})
                record.spill({'state': state})
                self.states.append(record)
            for key in list(row):
                if key.startswith('_'):
                    row.pop(key)
        self.states.sort(key=lambda row: (row['wall'] is None, row['wall'] or 0, row['tool_id'], row['projection_index']))
        state_by_run = defaultdict(list)
        state_by_tool = defaultdict(list)
        for state in self.states:
            state_by_tool[state['tool_id']].append(state)
            if state['kind'] != 'before' and state['wall'] is not None and 'room' in state['state']:
                state_by_run[state['run_id']].append(state)
        state_walls = {run_id: [state['wall'] for state in states] for run_id, states in state_by_run.items()}

        def prior(run_id, moment):
            if moment is None:
                return None
            index = bisect_right(state_walls.get(run_id, []), moment) - 1
            return state_by_run[run_id][index] if index >= 0 else None

        for row in self.tools.values():
            before = prior(row['run_id'], row['requested_wall'])
            before_state = before['state'] if before else {}
            row['context_state_id'] = before['state_id'] if before else None
            row['last_observed_room'] = before_state.get('room')
            after = state_by_tool[row['tool_id']]
            after_state = after[-1]['state'] if after else {}
            row['after_state_ids'] = [state['state_id'] for state in after]
            row.spill({'observed_delta': ({key: {'before': before_state[key], 'after': after_state[key]}
                                          for key in sorted(before_state.keys() & after_state.keys())
                                          if before_state[key] != after_state[key]} if before and after else None)})
            for phase, moment in [('requested', row['requested_wall']), ('result', row['result_wall'])]:
                self.event({key: row[key] for key in ('session_id', 'run_id', 'run')}, 'tool.' + phase, moment,
                           'tool_calls.jsonl', row['tool_id'] + ':' + phase, {'status': row['status']}, row['tool_id'], row['turn_id'])
        for action in self.actions:
            before = prior(action['run_id'], action['public_event_wall'])
            action['context_state_id'] = before['state_id'] if before else None
            action['last_observed_room'] = before['state'].get('room') if before else None
        for index, message in enumerate(self.report['public_text']):
            run_id = self.run_ids.get(message['run'])
            self.event({'session_id': self.session_id, 'run_id': run_id, 'run': message['run']}, 'public.message', message.get('wall'),
                       'public_text.csv', stable_id(run_id, message['id'], message['text']),
                       {'public_event_id': message['id'], 'role': message['role'], 'kind': message['kind'],
                        'text_sha256': hashlib.sha256(message['text'].encode()).hexdigest()})
        selected_tools = []
        for row in self.tools.values():
            scope = window_status(row['requested_wall'], row['result_wall'], self.since, self.until)
            if scope != 'outside':
                row['window_membership'] = scope
                selected_tools.append(row)
        selected_turns = []
        for row in self.turns.values():
            row['window_membership'] = window_status(row['requested_wall'], row['ended_wall'], self.since, self.until)
            if row['window_membership'] != 'outside':
                selected_turns.append(row)
        selected_actions = []
        for row in self.actions:
            row['window_membership'] = window_status(row['public_event_wall'], row['public_event_wall'], self.since, self.until)
            if row['window_membership'] != 'outside':
                selected_actions.append(row)
        selected_events = []
        for row in self.events:
            row['window_membership'] = window_status(row['wall'], row['wall'], self.since, self.until)
            if row['window_membership'] != 'outside':
                selected_events.append(row)
        selected_events.sort(key=lambda row: (row['wall'] is None, row['wall'] or 0, row['event_id']))
        selected_tools.sort(key=lambda row: (row['requested_wall'] is None, row['requested_wall'] or 0, row['tool_id']))
        for sequence, row in enumerate(selected_tools, 1):
            row['sequence'] = sequence
        selected_turns.sort(key=lambda row: (row['requested_wall'] is None, row['requested_wall'] or 0, row['turn_id']))
        records = {'tool_calls': selected_tools, 'timeline': selected_events, 'turns': selected_turns,
                   'actions': selected_actions, 'game_states': self.states, 'metric_snapshots': self.metrics}
        _, replaced = null_nonfinite(records)
        if replaced:
            self.warnings.append(f'分析记录中 {replaced} 个非有限数字置为 null，未作为零参与统计')
        for collection in records.values():
            for row in collection:
                row['schema_version'] = 1
        summary = summarize(records, self.chain, self.run_ids, self.since, self.until)
        clipped = sum(bool(row['parameters_meta'].get('truncated') or row['result_meta'].get('truncated'))
                      for row in records['tool_calls'])
        omitted = sum(bool(row.get('details_omitted')) for row in records['tool_calls'])
        summary['coverage']['single_record_truncated_tools'] = clipped
        summary['coverage']['source_detail_omitted_tools'] = omitted
        if clipped:
            self.warnings.append(f'{clipped} 条工具记录达到单条参数／结果投影限制；详见 parameters_meta／result_meta.truncated。累计详情不再截尾，状态索引独立保留。')
        if omitted:
            self.warnings.append(f'{omitted} 条工具记录的原始来源已省略详情，不能从缺失内容补造状态。')
        if self.detail_store.nonfinite_replaced:
            self.warnings.append(f'详情投影中 {self.detail_store.nonfinite_replaced} 个非有限数字置为 null，未作为零参与统计')
        summary['coverage']['source_warnings'] = list(dict.fromkeys(self.warnings))
        summary['coverage']['detail_storage'] = {'mode': 'temporary_chunked_spool',
            'chunk_bytes': self.detail_store.CHUNK_BYTES, 'chunks': len(self.detail_store.chunks),
            'projected_bytes': self.detail_store.bytes, 'records': self.detail_store.records,
            'cumulative_budget_omissions': False}
        summary['sources'] = self.sources
        return summary, records


def summarize(records, chain, run_ids, since, until):
    tools, actions, snapshots = records['tool_calls'], records['actions'], records['metric_snapshots']
    by_path = {row['run']: row for row in snapshots if row['canonical'] and row.get('metrics_authoritative', True)}
    comparisons = []
    for directory, state in chain:
        before = by_path.get(str(Path(state['resumed_from']).resolve())) if isinstance(state.get('resumed_from'), str) else None
        after = by_path.get(str(directory))
        if before and after:
            counters = {}
            for key in before['counters'].keys() & after['counters'].keys():
                a, b = before['counters'][key], after['counters'][key]
                counters[key] = {'before': a, 'after': b, 'delta': b - a if finite(a) and finite(b) and b >= a else None,
                                 'reset_or_decrease': finite(a) and finite(b) and b < a}
            a, b = (before.get('game_clock') or {}).get('committed'), (after.get('game_clock') or {}).get('committed')
            comparisons.append({'run_id': run_ids[str(directory)], 'base_snapshot_id': before['snapshot_id'], 'end_snapshot_id': after['snapshot_id'],
                                'base_wall': before['wall'], 'end_wall': after['wall'], 'counters': counters,
                                'game_clock_seconds_delta': (b - a) / 60 if finite(a) and finite(b) and b >= a else None,
                                'semantics': 'snapshot comparison along resumed_from; not a proven resume baseline or exact time-window total'})
    rooms = {}
    for row in tools:
        key = (row['run_id'], row.get('last_observed_room'))
        record = rooms.setdefault(key, {'run_id': key[0], 'last_observed_room': key[1], 'tools': 0, 'code_mode_wrappers': 0,
                                        'thinking_before_tools_s': 0.0, 'actions': 0, 'consumed_frames': 0})
        if row['record_kind'] == 'code_mode_wrapper':
            record['code_mode_wrappers'] += 1
        else:
            record['tools'] += 1
            start = row.get('thinking_interval_start_wall')
            if finite(row.get('thinking_before_s')) and row['window_membership'] != 'time_unknown' and (since is None or start is not None and start >= since):
                record['thinking_before_tools_s'] += row['thinking_before_s']
    patterns = defaultdict(list)
    for row in actions:
        key = (row['run_id'], row.get('last_observed_room'))
        record = rooms.setdefault(key, {'run_id': key[0], 'last_observed_room': key[1], 'tools': 0, 'code_mode_wrappers': 0,
                                        'thinking_before_tools_s': 0.0, 'actions': 0, 'consumed_frames': 0})
        record['actions'] += 1
        if type(row.get('consumed')) is int:
            record['consumed_frames'] += row['consumed']
        patterns[(key, row['input_fingerprint'])].append(row)
    repeats = [{'run_id': key[0][0], 'last_observed_room': key[0][1], 'input_fingerprint': key[1], 'count': len(group),
                'action_ids': [row['action_id'] for row in group], 'stops': dict(Counter(str(row.get('stop')) for row in group))}
               for key, group in patterns.items() if len(group) > 1]
    return {'schema_version': 1, 'scope': {'since_wall': since, 'until_wall': until,
             'selection': 'intersecting known tool/turn intervals; boundary evidence retained; unknown times retained and labeled',
             'snapshots_and_state_evidence': 'all available session snapshots/states retained for references; not restricted to window'},
            'counts': {name: len(value) for name, value in records.items()},
            'files': {name: name + '.jsonl' for name in records},
            'coverage': {'tool_statuses': dict(Counter(row['status'] for row in tools)),
                         'time_unknown_tools': sum(row['window_membership'] == 'time_unknown' for row in tools),
                         'tools_with_parameters': sum(row['parameters'] is not None for row in tools),
                         'tools_with_result_details': sum(row['result_meta'].get('available') is True and row['result'] is not None for row in tools),
                         'code_mode_wrappers': sum(row['record_kind'] == 'code_mode_wrapper' for row in tools),
                         'actions_without_exact_call_id': sum(row['call_id'] is None for row in actions),
                         'limitations': ['工具计时闭合不证明执行成功；结果缺失不等于失败。',
                             'thinking_before_s 是主控计量差，包含输出/压缩等主控思考阶段；不是私有推理时长。',
                             'decision_wait_s 与 thinking_before_s 可能覆盖相同时间，不相加；末次工具后的思考可能尚未闭合。',
                             'active_s 并发可重叠且包含游戏工具占用，不等于累计 tool_s；不将消息间隔当思考或断网。',
                             'decisions.elapsed_s 是有效累计耗时，不作为墙钟；public_event_wall 仅为公开动作说明时间。',
                             'last_observed_room 仅为最近已保存观测的房间，不能保证未观测期间状态不变。',
                             '相同输入不等于相同初始状态或重复错误；不自动判断学习、理解或无效研究。',
                             '暂停/恢复控制记录仅有请求时间和当前确认状态，无法还原全部实际生效时刻。']},
            'snapshot_deltas': comparisons, 'room_workload': list(rooms.values()),
            'repeated_inputs': sorted(repeats, key=lambda row: -row['count']),
            'longest_thinking_before_tools': [{key: row.get(key) for key in ('tool_id', 'tool', 'run_id', 'native_turn_id',
                'requested_wall', 'thinking_before_s', 'thinking_interval_start_wall', 'last_observed_room', 'window_membership')}
                for row in sorted((row for row in tools if finite(row.get('thinking_before_s'))), key=lambda row: -row['thinking_before_s'])[:20]]}


def collect_analysis(report, chain, snapshots, native, since=None, until=None):
    analysis = Analysis(report, chain, snapshots, native, since, until)
    try:
        summary, records = analysis.finish()
    except BaseException:
        analysis.detail_store.close()
        raise
    finally:
        if isinstance(native.get('tool_calls'), SpoolRows):
            native['tool_calls'].close()
    summary['coverage']['native_sources_available'] = bool(native.get('sources'))
    if not native.get('sources') and report['session']['engine'] == 'codex':
        summary['coverage']['limitations'].append('未取得原生会话日志；动态工具参数与结果可能只有本机摘要，code-mode 包装调用可能缺失。')
    result = AnalysisReport(report, records, analysis.detail_store)
    _, replaced = null_nonfinite(result)
    if replaced:
        result['warnings'].append(f'兼容字段中 {replaced} 个非有限数字置为 null')
    result['analysis'] = summary
    return result
