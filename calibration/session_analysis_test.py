"""Behavior export over real file readers; no controller, model or game connection."""
import csv
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from orchestrator.session_export import collect_session, write_export
from orchestrator.session_export_native import scan_native, reader_code, stream_reader_code, read_native_stream
from orchestrator.session_export_spool import ExportSpool, SpilledDict
from orchestrator.session_export_projection import project_tool, clean_text
from orchestrator.session_export_workspace import workspace_snapshot
from orchestrator.session_analysis import Analysis


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sid = '01a19fbf-10fc-7902-b2ac-6a5f73bc2c1a'
        self.run = self.make_run('one')

    def put(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def append(self, path, value):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value) + '\n')

    def make_run(self, name):
        run = self.root / 'runs' / 'agent' / name
        run.mkdir(parents=True)
        self.put(run / 'run_state.json', {'engine': 'codex', 'session_id': self.sid, 'agent': 'agent'})
        self.put(run / 'metrics.json', {'written_wall': 1100, 'thinking_s': 60, 'tool_s': 3, 'game_clock': {'committed': 120}})
        (run / 'public_events.jsonl').touch()
        return run

    def timing(self, event, wall, **fields):
        self.append(self.run / 'decision_timing.jsonl', {'event': event, 'wall': wall, 'turn': 1, **fields})

    def runtime(self, method, wall, **params):
        self.append(self.run / 'codex_events.jsonl', {'method': method, 'wall': wall, 'params': {'threadId': self.sid, **params}})

    def collect(self, **kwargs):
        return collect_session(self.run, include_analysis=True, **kwargs)

    def native(self):
        home = self.root / 'native'
        path = home / 'sessions' / '2026' / '11' / '03' / (self.sid + '.jsonl')
        path.parent.mkdir(parents=True)
        self.append(path, {'type': 'session_meta', 'payload': {'id': self.sid}})
        return home, path

    def test_token_advances_export_actual_frames_and_state_without_inferred_unknown_state(self):
        path = self.run/'token_advances.jsonl'
        self.append(path, {'event': 'requested', 'wall': 1001,
                           'batch': {'id': 1, 'requested': 2, 'origin': 'deferred', 'before': {'frame': 100}}})
        self.append(path, {'event': 'committed', 'wall': 1002,
                           'batch': {'id': 1, 'requested': 2, 'consumed': 1, 'stop': 'room',
                                     'after': {'frame': 101, 'room': 'next', 'player': {'pos': [20, 30]}}}})
        self.append(path, {'event': 'requested', 'wall': 1003, 'batch': {'id': 2, 'requested': 1}})
        self.append(path, {'event': 'unknown', 'wall': 1004,
                           'batch': {'id': 2, 'status': 'unknown', 'error': 'ACK missing'}})
        with closing(self.collect()) as report:
            rows = report.analysis_records['tool_calls']
            self.assertEqual([r['record_kind'] for r in rows], ['system_advance', 'system_advance'])
            self.assertEqual(rows[0]['result']['consumed'], 1)
            self.assertEqual(rows[0]['parameters']['origin'], 'deferred')
            self.assertEqual(rows[1]['status'], 'execution_unknown')
            self.assertIsNone(rows[1]['success'])
            states = report.analysis_records['game_states']
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0]['state']['room'], 'next')
            self.assertEqual(states[0]['tool_id'], rows[0]['tool_id'])

    def test_blockage_timeline_preserves_category_when_recorded(self):
        self.put(self.run / 'agent_stop.json', {'reports': [
            {'wall': 1001, 'reason': '旧记录', 'decision': 1},
            {'wall': 1002, 'reason': '入场恢复不符合工具约定', 'decision': 2, 'category': 'transition_recovery'},
        ]})
        with closing(self.collect()) as report:
            events = [e for e in report.analysis_records['timeline'] if e['kind'] == 'agent.reported_blocked']
            self.assertEqual([e['data']['category'] for e in events], [None, 'transition_recovery'])

    def test_turn_parallel_tools_cleanup_failure_and_no_private_reasoning(self):
        self.timing('turn_requested', 1000)
        self.timing('turn_acknowledged', 1001, turn_id='uuid')
        self.timing('tool_requested', 1010, tool_id='shell', tool='Bash', decision_wait_s=10)
        self.timing('tool_requested', 1011, tool_id='missing', tool='Read', decision_wait_s=0)
        self.runtime('item/completed', 1012, turnId='uuid', item={'type': 'commandExecution', 'id': 'shell',
                     'command': 'echo test --token SECRET_TOKEN', 'aggregatedOutput': 'failed', 'exitCode': 2, 'durationMs': 250})
        self.timing('tool_completed', 1013, tool_id='shell', tool_active_s=3)
        self.timing('turn_ended', 1020, active_s=20, pending_tools=['missing'])
        self.timing('tool_completed', 1020, tool_id='missing', tool_active_s=9)
        self.runtime('item/completed', 1015, item={'type': 'reasoning', 'text': 'PRIVATE_REASONING_CANARY'})
        self.runtime('item/completed', 1016, item={'type': 'contextCompaction', 'id': 'compact', 'summary': 'COMPACTION_CANARY'})
        self.runtime('thread/tokenUsage/updated', 1017, tokenUsage={'last': {'inputTokens': 22, 'reasoningOutputTokens': 7}})
        self.runtime('tool/history', 1018, turnId='uuid', fact={'id': 'without-timing', 'tool': 'Read', 'phase': 'returned'})
        report = self.collect()
        records = report.analysis_records
        tools = {row['call_id']: row for row in records['tool_calls']}
        self.assertEqual(tools['shell']['status'], 'failed')
        self.assertEqual(tools['shell']['exit_code'], 2)
        self.assertEqual(tools['missing']['status'], 'interrupted_result_missing')
        self.assertIsNone(tools['missing']['result_wall'])
        self.assertIsNone(tools['missing']['thinking_interval_start_wall'])
        self.assertEqual(records['turns'][0]['thinking_before_tools_s'], 10)
        self.assertEqual(tools['shell']['native_turn_id'], 'uuid')
        self.assertEqual(tools['without-timing']['turn_id'], tools['shell']['turn_id'])
        body = json.dumps(records)
        for marker in ('PRIVATE_REASONING_CANARY', 'COMPACTION_CANARY', 'SECRET_TOKEN'):
            self.assertNotIn(marker, body)
        self.assertIn('reasoningOutputTokens', body)

    def test_native_isolated_reader_all_tools_wrappers_state_and_simulation_link(self):
        home, path = self.native()
        self.timing('turn_requested', 1000)
        self.timing('turn_acknowledged', 1001, turn_id='uuid')
        self.timing('tool_requested', 1002, tool_id='act', tool='celeste_act', decision_wait_s=2)
        self.append(path, {'type': 'turn_context', 'payload': {'turn_id': 'uuid'}})
        self.append(path, {'type': 'response_item', 'timestamp': '1970-01-01T00:16:42Z', 'payload': {
            'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'wrapper', 'input': 'await tools.act()'}})
        state = {'frame': 200, 'room': 'd7', 'player': {'x': 4, 'y': 8, 'dead': True},
                 'entities': [{'type': 'dream_block', 'active': False}], 'reasoning': 'STATE_PRIVATE',
                 'before': {'room': 'OLD', 'frame': 100}, 'after': {'room': 'd7', 'frame': 200}}
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'uuid',
            'started_at_ms': 1002000, 'completed_at_ms': 1003000, 'item': {'type': 'DynamicToolCall', 'id': 'act',
            'tool': 'celeste_act', 'arguments': {'inputs': ['10,R']}, 'success': True,
            'duration': {'secs': 1, 'nanos': 0}, 'content_items': [{'type': 'text', 'text': json.dumps(state)}]}}})
        self.append(path, {'type': 'response_item', 'timestamp': '1970-01-01T00:16:43Z', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'wrapper', 'output': 'done'}})
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'uuid', 'started_at_ms': 1004000, 'completed_at_ms': 1005000, 'item': {
            'type': 'CommandExecution', 'id': 'sh', 'command': 'python plan.py', 'exit_code': 0, 'aggregated_output': 'OK'}}})
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'uuid', 'item': {
            'type': 'FileChange', 'id': 'file', 'status': 'completed', 'changes': [{'path': 'plan.py', 'kind': 'add', 'diff': 'DIFF_PRIVATE'}]}}})
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'uuid', 'item': {
            'type': 'DynamicToolCall', 'id': 'sim', 'tool': 'celeste_simulate', 'arguments': {'candidates': [['10,R']]},
            'success': True, 'content_items': [{'type': 'text', 'text': json.dumps({'receipt': 'r', 'cost': 3, 'energy': {'balance': 7},
            'results': [{'stop': 'dead', 'frames': [{'x': 5}]}]})}]}}})
        code = reader_code(True) + '\nprint(json.dumps(scan_native(sys.argv[1],sys.argv[2],True)))'
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(home), self.sid], capture_output=True, text=True, check=True)
        native = json.loads(result.stdout)
        report = self.collect(native_reader=lambda state: native)
        tools = {row['call_id']: row for row in report.analysis_records['tool_calls']}
        self.assertEqual(set(tools), {'act', 'wrapper', 'sh', 'file', 'sim'})
        self.assertEqual(tools['wrapper']['record_kind'], 'code_mode_wrapper')
        self.assertEqual(tools['act']['runtime_duration_s'], 1)
        self.assertEqual(tools['sh']['last_observed_room'], 'd7')
        self.assertEqual(tools['sim']['result']['structured']['receipt'], report['simulations'][0]['receipt'])
        states = report.analysis_records['game_states']
        self.assertEqual(states[0]['state']['room'], 'd7')
        self.assertFalse(states[0]['state']['dream_blocks'][0]['active'])
        body = json.dumps(report.analysis_records)
        self.assertNotIn('STATE_PRIVATE', body)
        self.assertNotIn('DIFF_PRIVATE', body)

    def test_window_unknown_boundaries_cross_run_ids_and_snapshot_resets(self):
        other = self.make_run('two')
        data = json.loads((other / 'run_state.json').read_text(encoding='utf-8'))
        self.put(other / 'run_state.json', {**data, 'resumed_from': str(self.run)})
        self.put(other / 'metrics.json', {'written_wall': 1200, 'thinking_s': 50, 'tool_s': 4, 'game_clock': {'committed': 180}})
        for run in (self.run, other):
            self.append(run / 'decision_timing.jsonl', {'turn': 1, 'event': 'tool_requested', 'wall': 1000, 'tool_id': 'same', 'tool': 'Read'})
            self.append(run / 'decision_timing.jsonl', {'turn': 1, 'event': 'tool_completed', 'wall': 1030, 'tool_id': 'same', 'tool_active_s': 30})
        self.runtime('tool/history', 1020, fact={'id': 'endonly', 'tool': 'Read', 'phase': 'returned', 'result': {}})
        report = self.collect(since='1970-01-01T00:16:50+00:00', until='1970-01-01T00:17:10+00:00')
        tools = report.analysis_records['tool_calls']
        same = [row for row in tools if row['call_id'] == 'same']
        self.assertEqual(len({row['tool_id'] for row in same}), 2)
        self.assertTrue(all(row['window_membership'] == 'time_unknown' for row in same))
        self.assertEqual(next(row for row in tools if row['call_id'] == 'endonly')['window_membership'], 'end_observed_in_window')
        delta = report['analysis']['snapshot_deltas'][0]
        self.assertIsNone(delta['counters']['thinking_s']['delta'])
        self.assertTrue(delta['counters']['thinking_s']['reset_or_decrease'])
        self.assertEqual(delta['game_clock_seconds_delta'], 1)

    def test_actions_full_input_fingerprint_no_guessed_wall_or_call_id(self):
        prefix = ['1,R'] * 200
        for n in (1, 2):
            self.append(self.run / 'decisions.jsonl', {'i': n, 't': 5, 'inputs': prefix + [f'{n},L'], 'consumed': 2, 'stop': 'dead'})
        self.append(self.run / 'public_events.jsonl', {'id': 'act:2:0', 'text': '公开', 'wall': 1020})
        report = self.collect()
        one, two = report.analysis_records['actions']
        self.assertIsNone(one['public_event_wall'])
        self.assertIsNone(one['call_id'])
        self.assertEqual(two['public_event_wall'], 1020)
        self.assertNotEqual(one['input_fingerprint'], two['input_fingerprint'])
        self.assertEqual(report['analysis']['repeated_inputs'], [])

    def test_large_outputs_rotate_chunks_without_losing_later_states_or_details(self):
        home, path = self.native()
        for n in range(1800):
            self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'id': str(n), 'type': 'CommandExecution', 'command': 'echo ' + 'x ' * 4000, 'exit_code': 0, 'aggregated_output': ('x ' * 8000)}}})
        self.timing('turn_requested', 1000)
        self.timing('turn_acknowledged', 1000, turn_id='uuid')
        for n, room in enumerate(('b-08', 'c-00', 'd-10')):
            self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'uuid',
                'started_at_ms': (1001+n*2)*1000, 'completed_at_ms': (1002+n*2)*1000, 'item': {
                'id': room, 'type': 'DynamicToolCall', 'tool': 'celeste_act', 'arguments': {'inputs': ['1,R']},
                'success': True, 'content_items': [{'type': 'text', 'text': json.dumps({'room': room, 'frame': n})}]}}})
        self.timing('tool_requested', 1010, tool_id='tail-shell', tool='Bash')
        self.runtime('item/completed', 1011, item={'type': 'commandExecution', 'id': 'tail-shell',
                     'command': 'tail experiment', 'aggregatedOutput': 'TAIL_RESULT', 'exitCode': 0})
        native = read_native_stream([sys.executable, '-I', '-S', '-c', stream_reader_code(home), self.sid])
        self.assertEqual(len(native['tool_calls']), 1803)
        self.assertGreater(native['tool_calls'].store.bytes, 24 * 1024 * 1024)
        self.assertGreater(len(native['tool_calls'].store.chunks), 1)
        native_root = native['tool_calls'].store.root
        report = self.collect(native_reader=lambda state: native)
        self.assertFalse(native_root.exists())
        tools = {r['call_id']: r for r in report.analysis_records['tool_calls']}
        self.assertEqual(tools['tail-shell']['last_observed_room'], 'd-10')
        self.assertEqual(tools['1799']['result']['text_excerpt'], 'x ' * 4000 + '…')
        store_root = report.detail_store.root
        archive_path = write_export(report, self.root / 'large-export')
        self.assertFalse(store_root.exists())
        with zipfile.ZipFile(archive_path) as output:
            saved = {r['call_id']: r for r in map(json.loads, output.read('tool_calls.jsonl').splitlines())}
            states = [json.loads(line)['state']['room'] for line in output.read('game_states.jsonl').splitlines()]
        self.assertEqual(states, ['b-08', 'c-00', 'd-10'])
        self.assertEqual(saved['tail-shell']['result']['text_excerpt'], 'TAIL_RESULT')
        self.assertEqual(saved['1799']['parameters']['command'], ('echo ' + 'x ' * 4000)[:8000] + '…')
        self.assertFalse(any(r.get('details_omitted') for r in saved.values()))
        projected = project_tool('Read', {'path': '.env'}, [{'type': 'text', 'text': 'SENSITIVE_FILE'}, {'type': 'image', 'data': 'IMAGE_CANARY'}])
        self.assertNotIn('SENSITIVE_FILE', json.dumps(projected))
        self.assertNotIn('IMAGE_CANARY', json.dumps(projected))
        projected = project_tool('celeste_act', {'inputs': ['1,R']}, '[帧 123] 第2章 · 房间 d7 · 死亡 4\nMadeline：位置 (1, 2) 速度 (3, 4) 状态 Normal')
        self.assertEqual(projected['game_states'][0]['state']['player']['vx'], 3)

    def test_stream_wrappers_explicit_room_clear_and_prediction_isolation(self):
        home, path = self.native()
        self.timing('turn_requested', 1000)
        self.timing('turn_acknowledged', 1000, turn_id='uuid')
        self.append(path, {'type': 'turn_context', 'payload': {'turn_id': 'uuid'}})
        self.append(path, {'type': 'response_item', 'timestamp': '1970-01-01T00:16:41Z', 'payload': {
            'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'wrapper', 'input': 'game experiment'}})
        for n, (tool, state) in enumerate((('celeste_act', {'room': 'd-10', 'frame': 10}),
                                          ('celeste_simulate', {'room': 'PREDICTION_ONLY', 'frame': 20}),
                                          ('celeste_observe', {'room': None, 'scene': 'AreaComplete', 'frame': 30}))):
            self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'uuid',
                'started_at_ms': (1002+n)*1000, 'completed_at_ms': (1003+n)*1000, 'item': {
                'type': 'DynamicToolCall', 'id': str(n), 'tool': tool, 'content_items': [{'type': 'text', 'text': json.dumps(state)}]}}})
        self.append(path, {'type': 'response_item', 'timestamp': '1970-01-01T00:16:46Z', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'wrapper', 'output': 'WRAPPER_PUBLIC_RESULT'}})
        self.timing('tool_requested', 1010, tool_id='after-clear', tool='Bash')
        self.runtime('item/completed', 1011, item={'type': 'commandExecution', 'id': 'after-clear',
                     'command': 'echo room', 'aggregatedOutput': '[帧 999] 房间 SHELL_ONLY', 'exitCode': 0})
        native = read_native_stream([sys.executable, '-I', '-S', '-c', stream_reader_code(home), self.sid])
        report = self.collect(native_reader=lambda state: native)
        self.addCleanup(report.close)
        tools = {r['call_id']: r for r in report.analysis_records['tool_calls']}
        self.assertEqual(tools['wrapper']['parameters']['code'], 'game experiment')
        self.assertEqual(tools['wrapper']['result']['text_excerpt'], 'WRAPPER_PUBLIC_RESULT')
        self.assertIsNone(tools['after-clear']['last_observed_room'])
        self.assertEqual([r['state']['room'] for r in report.analysis_records['game_states']], ['d-10', None])

    def test_incomplete_native_stream_and_failed_write_clean_private_chunks(self):
        roots = []
        original = tempfile.TemporaryDirectory
        def temporary(*args, **kwargs):
            result = original(*args, **kwargs)
            roots.append(Path(result.name))
            return result
        for code in ("print('{\"tool\":{}}')", "print('{\"summary\":{},\"tool_count\":1}')", "raise SystemExit(2)"):
            with self.subTest(code=code), patch('orchestrator.session_export_spool.tempfile.TemporaryDirectory', side_effect=temporary):
                with self.assertRaises((ValueError, RuntimeError)):
                    read_native_stream([sys.executable, '-I', '-S', '-c', code])
            self.assertFalse(any(root.exists() for root in roots))
        self.runtime('item/completed', 1000, item={'type': 'commandExecution', 'id': 'sh',
                     'command': 'experiment', 'aggregatedOutput': 'public result', 'exitCode': 0})
        report = self.collect()
        root = report.detail_store.root
        original_open = Path.open
        def opened(path, *args, **kwargs):
            if path.name == 'tool_calls.jsonl':
                raise PermissionError('injected output write failure')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', opened), self.assertRaises(PermissionError):
            write_export(report, self.root / 'failed-write')
        self.assertFalse(root.exists())

    def test_spooled_mapping_copy_mutation_and_json_keep_real_values(self):
        store = ExportSpool()
        self.addCleanup(store.close)
        row = SpilledDict(store, {'id': 'record'})
        row.spill({'result': {'room': 'd-10'}, 'parameters': ['1,R']})
        for copied in (dict(row), {**row}, row.copy(), row | {}, {} | row, json.loads(json.dumps(row))):
            self.assertEqual(copied['result'], {'room': 'd-10'})
            self.assertEqual(copied['parameters'], ['1,R'])
        self.assertIn({'room': 'd-10'}, row.values())
        self.assertEqual(row, {'id': 'record', 'result': {'room': 'd-10'}, 'parameters': ['1,R']})
        different = SpilledDict(store, {'id': 'record'})
        different.spill({'result': {'room': 'c-00'}, 'parameters': ['1,R']})
        self.assertNotEqual(row, different)
        self.assertIn('d-10', repr(row))
        self.assertEqual(row.setdefault('result'), {'room': 'd-10'})
        row |= {'result': 'new inline result'}
        self.assertEqual(row['result'], 'new inline result')
        del row['parameters']
        self.assertNotIn('parameters', row.references)
        row.spill({'tail': 7})
        self.assertEqual(row.popitem(), ('tail', 7))
        row.clear()
        self.assertEqual(row.references, {})

    def test_nonfinite_native_metadata_is_unknown_without_losing_the_stream(self):
        home, path = self.native()
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'started_at_ms': 1000000,
            'completed_at_ms': 1001000, 'item': {'type': 'CommandExecution', 'id': 'bad-exit',
            'command': 'experiment', 'exit_code': float('nan'), 'aggregated_output': 'PUBLIC_RESULT'}}})
        native = read_native_stream([sys.executable, '-I', '-S', '-c', stream_reader_code(home), self.sid])
        self.assertTrue(any('非有限数字' in warning for warning in native['warnings']))
        report = self.collect(native_reader=lambda state: native)
        tool = report.analysis_records['tool_calls'][0]
        self.assertIsNone(tool['exit_code'])
        self.assertEqual(tool['result']['text_excerpt'], 'PUBLIC_RESULT')
        write_export(report, self.root / 'nonfinite-native')

    def test_restarted_controller_counter_does_not_overwrite_historical_turn(self):
        for start, native_id, ident in ((1000, 'first-turn', 'first-call'), (1020, 'later-turn', 'later-call')):
            self.timing('turn_requested', start)
            self.timing('turn_acknowledged', start+1, turn_id=native_id)
            self.timing('tool_requested', start+2, tool_id=ident, tool='Bash')
            self.runtime('item/completed', start+3, turnId=native_id, item={'type': 'commandExecution',
                'id': ident, 'command': 'experiment', 'aggregatedOutput': 'PUBLIC_RESULT', 'exitCode': 0})
            self.timing('turn_ended', start+4, active_s=4, pending_tools=[])
        report = self.collect(until='1970-01-01T00:16:50+00:00')
        self.addCleanup(report.close)
        turns = report.analysis_records['turns']
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0]['requested_wall'], turns[0]['ended_wall'], turns[0]['native_turn_id']),
                         (1000, 1004, 'first-turn'))
        self.assertEqual(report.analysis_records['tool_calls'][0]['turn_id'], turns[0]['turn_id'])

    def test_counter_reuse_preserves_acknowledged_turn_with_unknown_request_time(self):
        for request_present in (True, False):
            with self.subTest(request_present=request_present):
                (self.run / 'decision_timing.jsonl').write_text('')
                if request_present:
                    self.timing('turn_requested', None)
                self.timing('turn_acknowledged', 1001, turn_id='earlier')
                self.timing('turn_ended', 1004, active_s=4, pending_tools=[])
                self.timing('turn_requested', 1020)
                self.timing('turn_acknowledged', 1021, turn_id='later')
                report = self.collect()
                self.addCleanup(report.close)
                turns = {r['native_turn_id']: r for r in report.analysis_records['turns']}
                self.assertEqual(set(turns), {'earlier', 'later'})
                self.assertIsNone(turns['earlier']['requested_wall'])
                self.assertEqual(turns['earlier']['ended_wall'], 1004)

    def test_readonly_controls_workspace_legacy_compat_and_artifact_integrity(self):
        db = self.run / 'control.sqlite3'
        with closing(sqlite3.connect(db)) as connection:
            connection.executescript('CREATE TABLE actions(id,kind,text,status,detail,wall); CREATE TABLE replies(id,text,wall); CREATE TABLE state(id,data);')
            connection.execute('INSERT INTO actions VALUES(1,?,?,?,?,?)', ('pause', '', 'applied', '', 1010))
            connection.execute('INSERT INTO state VALUES(1,?)', (json.dumps({'paused': True, 'wall': 1020}),))
            connection.commit()
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        workspace = self.root / 'workspace'
        workspace.mkdir()
        (workspace / 'notes.md').write_text('学习记录 password=SECRET_NOTE', encoding='utf-8')
        (workspace / 'plan.py').write_text('raise RuntimeError("DO_NOT_EXECUTE")', encoding='utf-8')
        (workspace / '.env').write_text('SECRET_ENV', encoding='utf-8')
        legacy = collect_session(self.run)
        self.assertNotIn('analysis', legacy)
        report = self.collect(workspace=workspace)
        for key in legacy.keys() - {'exported_at'}:
            self.assertEqual(legacy[key], report[key])
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
        self.assertNotIn('SECRET_NOTE', json.dumps(report['workspace']))
        self.assertNotIn('SECRET_ENV', json.dumps(report['workspace']))
        self.assertIsNone(report['workspace']['git']['worktree_status'])
        with self.assertRaises(ValueError):
            write_export(report, workspace / 'export')
        output = self.root / 'export'
        archive = write_export(report, output)
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn('tool_calls.jsonl', bundle.namelist())
            self.assertIn('SCHEMA.md', bundle.namelist())
            self.assertIn('git_changes.md', bundle.namelist())
            self.assertIn('notes.md', bundle.namelist())
            self.assertEqual(bundle.read('notes.md').decode(), report['workspace']['memory_documents'][0]['text'])
            self.assertNotIn(b'SECRET_NOTE', bundle.read('notes.md'))
        for name, digest in json.loads((output / 'manifest.json').read_text(encoding='utf-8')).items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
        events = report.analysis_records['timeline']
        self.assertTrue(any(row['kind'] == 'control.requested' for row in events))
        known = [row['wall'] for row in events if row['wall'] is not None]
        self.assertEqual(known, sorted(known))
        with (output / 'tool_calls.csv').open(encoding='utf-8-sig') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), report['analysis']['counts']['tool_calls'])

    @unittest.skipUnless(shutil.which('git'), 'Git is unavailable')
    def test_workspace_git_window_and_isolated_current_status(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

        def git(*args):
            return subprocess.check_output(['git', *args], cwd=workspace, env=env, text=True).strip()

        def commit(second, subject):
            wall = f'2025-01-01T00:00:{second:02d}Z'
            env.update(GIT_AUTHOR_DATE=wall, GIT_COMMITTER_DATE=wall)
            git('add', 'plan.py')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', subject)
            return git('rev-parse', 'HEAD')

        git('init', '--quiet', '--template=')
        plan = workspace / 'plan.py'
        plan.write_text('start\n', encoding='utf-8')
        first = commit(0, 'start')
        plan.write_text('second\n', encoding='utf-8')
        second = commit(1, 'password=SECRET_SUBJECT')
        plan.write_text('third\n', encoding='utf-8')
        third = commit(2, 'excluded end')
        (workspace / '.gitattributes').write_text('plan.py filter=probe\n', encoding='utf-8')
        git('config', 'filter.probe.clean', 'echo FILTER_EXECUTED > FILTER_CANARY')
        git('config', 'core.fsmonitor', 'echo FSMONITOR_EXECUTED > FSMONITOR_CANARY')
        plan.write_text('a different size for unstaged status\n', encoding='utf-8')
        (workspace / 'new.py').touch()
        index = workspace / '.git' / 'index'
        index_before = index.read_bytes()

        report = self.collect(workspace=workspace, since='2025-01-01T00:00:01Z', until='2025-01-01T00:00:02Z')
        result = report['workspace']['git']
        wall = datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(result['commits'], [{'commit': second, 'wall': wall, 'subject': 'password=[隐藏]'}])
        self.assertEqual(result['head_events'], [{'old_head': first, 'new_head': second, 'wall': wall}])
        self.assertEqual((result['window_start_head'], result['window_end_head'], result['head']), (first, second, third))
        self.assertEqual(result['window_changes'], [{'status': 'M', 'path': 'plan.py'}])
        self.assertEqual(result['worktree_status'], [{'status': ' M', 'path': 'plan.py'},
                                                     {'status': '??', 'path': '.gitattributes'},
                                                     {'status': '??', 'path': 'new.py'}])
        self.assertEqual(index.read_bytes(), index_before)
        self.assertFalse(list(workspace.glob('*CANARY')))
        archive = write_export(report, self.root / 'git-export')
        with zipfile.ZipFile(archive) as bundle:
            markdown = bundle.read('git_changes.md').decode()
            self.assertIn(second, markdown)
            self.assertIn('plan.py', markdown)
            self.assertNotIn('SECRET_SUBJECT', markdown)
            self.assertNotIn('notes.md', bundle.namelist())

        result = workspace_snapshot(workspace, wall + 0.5, wall + 1)['git']
        self.assertEqual(result['commits'], [])
        self.assertEqual(result['head_events'], [])
        self.assertEqual(result['window_changes'], [])
        (workspace / '.git' / 'logs' / 'HEAD').unlink()
        result = workspace_snapshot(workspace, wall, wall + 1)['git']
        self.assertIsNone(result['window_changes'])
        self.assertEqual([commit['commit'] for commit in result['commits']], [second])

    @unittest.skipUnless(shutil.which('git'), 'Git is unavailable')
    def test_nested_git_repositories_keep_root_notes_and_export_separate_readonly_evidence(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        note = b'Current player notes\n'
        (workspace / 'notes.md').write_bytes(note)
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_DATE='2025-01-01T00:00:01Z', GIT_COMMITTER_DATE='2025-01-01T00:00:01Z')

        def git(root, *args):
            return subprocess.check_output(['git', *args], cwd=root, env=env, text=True).strip()

        git(workspace, 'init', '--quiet', '--template=')
        expected = {}
        for name in ('celeste-lab', 'other-program'):
            root = workspace / name
            root.mkdir()
            git(root, 'init', '--quiet', '--template=')
            (root / 'plan.py').write_text('start\n', encoding='utf-8')
            git(root, 'add', 'plan.py')
            git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', name)
            expected[name] = (git(root, 'rev-parse', 'HEAD'), (root/'.git/index').read_bytes())
            git(root, 'config', 'core.fsmonitor', 'echo EXECUTED > FSMONITOR_CANARY')
        (workspace/'celeste-lab/plan.py').write_text('modified plan with different length\n', encoding='utf-8')
        (workspace/'pointer').mkdir()
        (workspace/'pointer/.git').write_text('gitdir: ../../outside/.git\n', encoding='utf-8')
        (workspace/'broken/.git').mkdir(parents=True)
        (workspace/'broken/.git/HEAD').write_text('invalid\n', encoding='utf-8')
        (workspace/'node_modules/ignored/.git').mkdir(parents=True)

        report = self.collect(workspace=workspace, since='2025-01-01T00:00:01Z', until='2025-01-01T00:00:02Z')
        evidence = report['workspace']
        self.assertIsNone(evidence['git']['head'])
        repos = {row['path']: row for row in evidence['git_repositories']}
        self.assertEqual(set(repos), {'.', 'broken', 'celeste-lab', 'other-program'})
        self.assertIsNone(repos['broken']['head'])
        for name, (head, index) in expected.items():
            self.assertEqual(repos[name]['head'], head)
            self.assertEqual([row['commit'] for row in repos[name]['commits']], [head])
            self.assertEqual((workspace/name/'.git/index').read_bytes(), index)
            self.assertFalse((workspace/name/'FSMONITOR_CANARY').exists())
        self.assertEqual(repos['celeste-lab']['worktree_status'], [{'status': ' M', 'path': 'plan.py'}])
        self.assertEqual(repos['other-program']['worktree_status'], [])
        self.assertTrue(any('pointer/.git' in warning for warning in evidence['warnings']))
        archive = write_export(report, self.root/'nested-export')
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(bundle.read('notes.md'), note)
            markdown = bundle.read('git_changes.md').decode('utf-8')
            self.assertIn('celeste-lab', markdown)
            self.assertIn('other-program', markdown)
            for head, _ in expected.values():
                self.assertIn(head, markdown)
            self.assertIn('pointer/.git', bundle.read('README.md').decode('utf-8'))
        git(workspace, 'add', 'notes.md')
        git(workspace, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'root notes')
        root_head = git(workspace, 'rev-parse', 'HEAD')
        evidence = workspace_snapshot(workspace)
        self.assertEqual(evidence['git']['head'], root_head)
        self.assertEqual({row['path']: row['head'] for row in evidence['git_repositories'] if row['head']},
                         {'.': root_head, **{name: head for name, (head, _) in expected.items()}})

    def test_workspace_repository_and_empty_directory_scan_limits_are_reported(self):
        workspace = self.root/'workspace'
        workspace.mkdir()
        for name in ('a', 'b', 'c', 'd'):
            (workspace/name/'.git').mkdir(parents=True)
        with patch('orchestrator.session_export_workspace.REPOSITORY_LIMIT', 2):
            evidence = workspace_snapshot(workspace)
        self.assertEqual([repo['path'] for repo in evidence['git_repositories']], ['.', 'a'])
        self.assertTrue(any('Git 仓库达到' in warning for warning in evidence['warnings']))
        with patch('orchestrator.session_export_workspace.DIRECTORY_LIMIT', 2):
            evidence = workspace_snapshot(workspace)
        self.assertEqual([repo['path'] for repo in evidence['git_repositories']], ['.', 'a'])
        self.assertTrue(any('目录访问达到' in warning for warning in evidence['warnings']))

    def test_nested_git_links_do_not_follow_metadata_outside_the_workspace(self):
        workspace = self.root/'workspace'
        (workspace/'program').mkdir(parents=True)
        outside = self.root/'outside-git'
        outside.mkdir()
        (outside/'HEAD').write_text('f'*40+'\n', encoding='utf-8')
        try:
            os.symlink(outside, workspace/'program/.git', target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Directory symlink unavailable: {exc}')
        evidence = workspace_snapshot(workspace)
        self.assertEqual([repo['path'] for repo in evidence['git_repositories']], ['.'])
        self.assertTrue(any('program/.git' in warning for warning in evidence['warnings']))
        self.assertNotIn('f'*40, json.dumps(evidence))

    def test_nested_git_executable_and_temporary_directory_must_be_outside_entire_workspace(self):
        from orchestrator.session_export_git import git_snapshot
        workspace = self.root/'workspace'
        repository = workspace/'program'
        (repository/'.git').mkdir(parents=True)
        for shim in (workspace/'git.exe', workspace/'sibling/git.exe'):
            shim.parent.mkdir(exist_ok=True)
            shim.write_text('DO_NOT_EXECUTE', encoding='utf-8')
            with patch('orchestrator.session_export_git.shutil.which', return_value=str(shim)), \
                    patch('orchestrator.session_export_git.subprocess.run') as execute:
                evidence = git_snapshot(repository, 'a'*40, None, None, workspace_root=workspace)
            self.assertIsNone(evidence['commits'])
            self.assertIn('Git 可执行文件', evidence['status_unavailable_reason'])
            execute.assert_not_called()
        with patch('orchestrator.session_export_git.shutil.which', return_value=str(self.root/'trusted-git.exe')), \
                patch('orchestrator.session_export_git.tempfile.gettempdir', return_value=str(workspace)), \
                patch('orchestrator.session_export_git.tempfile.TemporaryDirectory') as temporary:
            evidence = git_snapshot(repository, 'a'*40, None, None, workspace_root=workspace)
        self.assertIn('临时目录', evidence['status_unavailable_reason'])
        temporary.assert_not_called()

    def test_native_unavailable_preserves_local_evidence_and_incomplete_line(self):
        self.timing('tool_requested', 1000, tool_id='pending', tool='Read', decision_wait_s=0)
        with (self.run / 'codex_events.jsonl').open('ab') as stream:
            stream.write(b'{"method":')
        def unavailable(state):
            raise RuntimeError('container unavailable')
        report = self.collect(native_reader=unavailable)
        self.assertEqual(report.analysis_records['tool_calls'][0]['status'], 'pending_or_missing')
        self.assertTrue(any('container unavailable' in item for item in report['warnings']))
        self.assertTrue(any('未完成尾行' in item for item in report['analysis']['coverage']['source_warnings']))

    def test_bearer_header_redaction_through_cli_archive_and_prediction_not_observation(self):
        secret = 'BearerSecretCanaryABC123'
        for text in (f'Authorization: Bearer {secret}', f'curl -H "authorization: bEaReR {secret}"',
                     f'{{"Authorization":"Bearer {secret}"}}', f'Authorization: Basic {secret}'):
            self.assertNotIn(secret, clean_text(text))
        self.runtime('item/completed', 1001, item={'type': 'commandExecution', 'id': 'sh',
                     'command': f'curl -H "Authorization: Bearer {secret}"',
                     'aggregatedOutput': f'Authorization: Bearer {secret}', 'exitCode': 0})
        output = self.root / 'cli-export'
        result = subprocess.run([sys.executable, '-X', 'utf8', 'tools/export_session.py', '--run', str(self.run),
                                 '--analysis', '--local-only', '--output', str(output)], capture_output=True, check=True)
        self.assertNotIn(secret, result.stdout.decode())
        with zipfile.ZipFile(output / 'export.zip') as bundle:
            for name in bundle.namelist():
                self.assertNotIn(secret.encode(), bundle.read(name), name)
        predicted = project_tool('celeste_simulate', {}, json.dumps({'frame': 99, 'room': 'predicted',
                                 'before': {'room': 'before'}, 'after': {'room': 'after'}}))
        self.assertEqual(predicted['game_states'], [])

    def test_actual_observation_fields_header_pairing_and_spooled_delivery(self):
        result = project_tool('celeste_observe', {}, json.dumps({'level': {'room': 'd7', 'chapterTimeTicks': 20, 'deathsInRoom': 2},
                              'player': {'pos': [3, 4], 'speed': [1, 2], 'inControl': True}}))
        state = result['game_states'][0]['state']
        self.assertEqual(state['player']['pos'], [3, 4])
        self.assertEqual(state['deathsInRoom'], 2)
        text = '[帧 1] 第2章 · 房间 OLD\nMadeline：位置 (1, 2) 速度 (3, 4) 状态 Normal\n[帧 2] 第2章 · 房间 NEW\nMadeline：位置 (30, 40) 速度 (5, 6) 状态 Dash'
        state = project_tool('celeste_observe', {}, text)['game_states'][0]['state']
        self.assertEqual((state['room'], state['player']['x']), ('NEW', 30))
        self.runtime('item/completed', 1000, item={'type': 'fileChange', 'id': 'file', 'status': 'completed',
                     'changes': [{'path': 'a.py', 'kind': 'add', 'diff': 'PRIVATE_DIFF_CANARY'}]})
        self.append(self.run / 'tool_deliveries.jsonl', {'event': 'recorded', 'call_id': 'file', 'wall': 1001,
                    'receipt': 'r', 'file': {'filePath': 'a.py'}, 'proof': 'native-record'})
        with patch.object(ExportSpool, 'CHUNK_BYTES', 256):
            report = self.collect()
        tool = report.analysis_records['tool_calls'][0]
        self.assertEqual(tool['file_changes'], [{'path': 'a.py', 'kind': 'add'}])
        self.assertEqual(tool['deliveries']['r']['event'], 'recorded')
        self.assertGreater(len(report.detail_store.chunks), 1)
        self.assertEqual(tool['call_id'], 'file')
        self.assertNotIn('PRIVATE_DIFF_CANARY', json.dumps(self.collect().analysis_records))

    def test_claude_separate_sensitive_file_result_and_compaction(self):
        self.put(self.run / 'run_state.json', {'engine': 'claude', 'provider': 'anthropic', 'session_id': 'claude-test'})
        self.append(self.run / 'agent_events.jsonl', {'type': 'assistant', 'wall': 1000, 'message': {'content': [
            {'type': 'thinking', 'thinking': 'CLAUDE_PRIVATE_REASONING'},
            {'type': 'tool_use', 'id': 'read', 'name': 'Read', 'input': {'file_path': '.env'}}]}})
        self.append(self.run / 'agent_events.jsonl', {'type': 'user', 'wall': 1001, 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'read', 'content': 'CLAUDE_ENV_SECRET_CANARY'}]}})
        self.append(self.run / 'agent_events.jsonl', {'type': 'system', 'subtype': 'compact_boundary', 'wall': 1002,
                    'compact_metadata': {'pre_tokens': 500, 'summary': 'CLAUDE_COMPACT_PRIVATE'}})
        report = self.collect()
        self.assertEqual(report.analysis_records['tool_calls'][0]['result'], {'omitted': 'sensitive_file'})
        archive = write_export(report, self.root / 'claude-export')
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                for marker in (b'CLAUDE_PRIVATE_REASONING', b'CLAUDE_ENV_SECRET_CANARY', b'CLAUDE_COMPACT_PRIVATE'):
                    self.assertNotIn(marker, bundle.read(name), name)

    def test_nonfinite_timing_values_become_unknown_and_sources_are_reported(self):
        self.timing('turn_requested', 1000)
        self.timing('tool_requested', 1001, tool_id='bad', tool='Bash', decision_wait_s=float('nan'))
        self.timing('tool_completed', 1002, tool_id='bad', tool_active_s=float('inf'))
        self.timing('turn_ended', 1003, active_s=float('nan'), pending_tools=[])
        report = self.collect()
        self.assertIsNone(report.analysis_records['tool_calls'][0]['thinking_before_s'])
        self.assertIsNone(report.analysis_records['tool_calls'][0]['active_s'])
        self.assertTrue(any('非有限数字' in item for item in report['analysis']['coverage']['source_warnings']))
        write_export(report, self.root / 'nonfinite-export')


if __name__ == '__main__':
    unittest.main()
