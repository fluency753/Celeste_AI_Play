"""Export real persisted sources without UI limits or writes to the source run."""
import csv
from contextlib import closing
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from orchestrator.session_export import TZ, collect_session, write_export, rows
from orchestrator.session_export_native import scan_native, read_container_simulations, reader_code


class SessionExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.sid = '01a19fbf-10fc-7902-b2ac-6a5f73bc2c1a'
        self.one = self.make_run('20261103-100000', 10)
        self.two = self.make_run('20261103-110000', 15)

    def put(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def append(self, path, data):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(data, ensure_ascii=False) + '\n')

    def make_run(self, name, elapsed):
        run = self.runs / 'tag' / name
        run.mkdir(parents=True)
        self.put(run / 'run_state.json', {'engine': 'codex', 'provider': 'codex', 'session_id': self.sid,
                                         'save': 'Aster', 'agent': 'tag', 'knowledge_profile': 'explore'})
        self.put(run / 'summary.json', {'elapsed_s': elapsed, 'started_wall': 1000, 'written_wall': 1000 + elapsed,
                                        'sim_calls': 1, 'sim_rollouts': 2, 'sim_energy': {'balance': 17}})
        (run / 'public_events.jsonl').touch()
        return run

    def test_full_history_readonly_wal_cross_run_scope_and_no_cumulative_double_count(self):
        for run in (self.one, self.two):
            self.append(run / 'public_events.jsonl', {'id': 'act:1:0', 'text': '相同文字仍是两次事件', 'wall': 1001, 'kind': '尝试'})
            self.append(run / 'public_events.jsonl', {'id': 'public:shared-message', 'text': '同一真实消息', 'wall': 1002})
        foreign = self.make_run('20261103-120000', 99)
        self.put(foreign / 'run_state.json', {'engine': 'codex', 'session_id': 'other-session'})
        self.append(foreign / 'public_events.jsonl', {'id': 'foreign', 'text': 'FOREIGN'})
        for n in range(100):
            self.append(self.one / 'public_events.jsonl', {'id': f'p{n}', 'text': '公开文本' + str(n), 'wall': 1003 + n})
        db = self.one / 'control.sqlite3'
        connection = sqlite3.connect(db)
        self.addCleanup(connection.close)
        connection.executescript('PRAGMA journal_mode=WAL; CREATE TABLE actions(id,kind,text,status,wall); CREATE TABLE replies(id,text,wall);')
        connection.executemany('INSERT INTO actions VALUES(?,?,?,?,?)', [(n, 'message', '用户' + str(n), 'submitted', 1004 + n) for n in range(90)])
        connection.execute('INSERT INTO replies VALUES(?,?,?)', ('shared-message', '同一真实消息', 1002))
        connection.commit()
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        report = collect_session(self.one, runs_root=self.runs)
        self.assertEqual(len(report['public_text']), 193)
        self.assertEqual(len(report['runs']), 2)
        self.assertEqual(report['time_summary']['elapsed_s'], 15)
        self.assertNotIn('FOREIGN', json.dumps(report))
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)

    def test_ledger_union_status_refund_and_exact_native_receipt_link(self):
        self.put(self.one / 'sim_energy.json', {'receipts': [{'id': 'a', 'cost': 9, 'status': 'reserved'}]})
        self.put(self.two / 'sim_energy.json', {'receipts': [
            {'id': 'a', 'cost': 9, 'status': 'used'}, {'id': 'b', 'cost': 3, 'status': 'refunded'}]})
        native = lambda state: {'calls': [{'call_id': 'call', 'receipt': 'a', 'cost': 9, 'success': True, 'balance_after': 17}]}
        report = collect_session(self.two, native_reader=native)
        self.assertEqual(report['simulation_summary']['net_spent'], 9)
        self.assertEqual(report['simulation_summary']['refunded'], 3)
        self.assertEqual(report['simulation_summary']['matched_receipts'], 1)
        self.assertEqual(len(report['simulations']), 2)
        self.assertEqual(report['simulations'][0]['ledger_status'], 'used')
        self.assertTrue(any('没有可关联' in warning for warning in report['warnings']))

    def test_claude_local_public_fallback_and_simulator_projection(self):
        run = self.make_run('20261103-130000', 20)
        self.put(run / 'run_state.json', {'engine': 'claude', 'provider': 'anthropic', 'session_id': 'claude-session', 'agent': 'tag'})
        (run / 'public_events.jsonl').unlink()
        events = run / 'agent_events.jsonl'
        self.append(events, {'type': 'assistant', 'message': {'id': 'msg', 'content': [
            {'type': 'text', 'text': '公开解说'}, {'type': 'thinking', 'thinking': 'PRIVATE_REASONING'},
            {'type': 'tool_use', 'id': 'sim', 'name': 'mcp__celeste__simulate', 'input': {'candidates': [['60,R']]}}]}})
        self.append(events, {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'sim', 'content': json.dumps({
            'receipt': 'r', 'cost': 3, 'energy': {'balance': 17}, 'private': 'PRIVATE_BODY'})}]}})
        self.put(run / 'sim_energy.json', {'receipts': [{'id': 'r', 'cost': 3, 'status': 'used'}]})
        report = collect_session(run)
        self.assertEqual(len(report['public_text']), 1)
        self.assertEqual(report['public_text'][0]['text'], '公开解说')
        self.assertEqual(report['simulation_summary']['net_spent'], 3)
        self.assertEqual(report['simulation_summary']['matched_receipts'], 1)
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_invalid_session_id_cannot_be_used_as_output_directory_component(self):
        self.put(self.one / 'run_state.json', {'engine': 'codex', 'session_id': '../../escape'})
        with self.assertRaises(ValueError):
            collect_session(self.one)

    def test_time_format_affects_markdown_csv_only_and_preserves_subsecond_order(self):
        for ident, wall in [('earlier', 1000.125), ('later', 1000.875)]:
            self.append(self.one / 'public_events.jsonl', {'id': ident, 'text': ident, 'wall': wall})
        self.put(self.one / 'sim_energy.json', {'receipts': [{'id': 'r', 'cost': 3, 'status': 'used'}]})
        report = collect_session(self.one, native_reader=lambda state: {'calls': [
            {'call_id': 'sim', 'receipt': 'r', 'cost': 3, 'success': True, 'wall': 1001.25}]})
        original = json.dumps(report, ensure_ascii=False)
        for name, kwargs, expected in [
            ('default', {}, '01-01 08:16:40'),
            ('custom', {'time_format': '%Y/%m/%d %H:%M:%S.%f %z'}, '1970/01/01 08:16:40.125000 +0800'),
        ]:
            output = self.root / name
            archive = write_export(report, output, **kwargs)
            markdown = (output / 'README.md').read_text(encoding='utf-8')
            self.assertIn('### ' + expected + ' ·', markdown)
            with (output / 'public_text.csv').open(encoding='utf-8-sig', newline='') as stream:
                public = list(csv.DictReader(stream))
            self.assertEqual(public[0]['time'], expected)
            self.assertEqual([row['id'] for row in public], ['earlier', 'later'])
            with (output / 'simulations.csv').open(encoding='utf-8-sig', newline='') as stream:
                simulation = next(csv.DictReader(stream))
            self.assertEqual(simulation['time'], '01-01 08:16:41' if name == 'default' else '1970/01/01 08:16:41.250000 +0800')
            self.assertIn('统计起点：01-01 08:16:40' if name == 'default' else '统计起点：1970/01/01 08:16:40.000000 +0800', markdown)
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(json.loads(bundle.read('session.json')), report)
            self.assertEqual(json.dumps(report, ensure_ascii=False), original)
        with self.assertRaises(ValueError):
            write_export(report, self.root / 'invalid', time_format='')
        self.assertFalse((self.root / 'invalid').exists())

    def test_cli_passes_python_strftime_option_to_renderer(self):
        self.append(self.one / 'public_events.jsonl', {'id': 'p', 'text': '公开', 'wall': 1000.25})
        output = self.root / 'cli-format'
        cli = Path(__file__).resolve().parents[1] / 'tools/export_session.py'
        result = subprocess.run([sys.executable, '-X', 'utf8', str(cli), '--run', str(self.one), '--local-only',
                                 '--output', str(output), '--time-format', '%Y/%m/%d %H:%M:%S'],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('### 1970/01/01 08:16:40 ·', (output / 'README.md').read_text(encoding='utf-8'))

    def test_message_time_range_is_half_open_precise_and_keeps_session_statistics(self):
        for ident, wall in [('before', 1000.0001), ('start', 1000.0002), ('inside', 1000.0003),
                            ('end', 1000.0004), ('unknown', None)]:
            self.append(self.one / 'public_events.jsonl', {'id': ident, 'text': ident, 'wall': wall})
        self.put(self.two / 'sim_energy.json', {'receipts': [{'id': 'r', 'cost': 3, 'status': 'used'}]})
        reader = lambda state: {'calls': [{'call_id': 'outside', 'receipt': 'r', 'cost': 3, 'wall': 2000}]}
        full = collect_session(self.two, native_reader=reader)
        selected = collect_session(self.two, native_reader=reader,
                                   since='1970-01-01T08:16:40.000200', until='1970-01-01T08:16:40.000400+08:00')
        self.assertEqual([row['id'] for row in selected['public_text']], ['start', 'inside'])
        self.assertEqual(len(full['public_text']), 5)
        self.assertIsNone(full['public_text_filter'])
        self.assertEqual(selected['public_text_filter']['excluded_unknown_time'], 1)
        self.assertEqual(selected['public_text_filter']['since'], '1970-01-01T08:16:40.000200+08:00')
        self.assertEqual(selected['public_text_filter']['statistics_scope'], 'entire_session')
        self.assertTrue(any('缺少有效时间' in warning for warning in selected['warnings']))
        for key in ('time_summary', 'simulations', 'simulation_summary', 'runs', 'segments'):
            self.assertEqual(selected[key], full[key], key)

    def test_message_time_range_one_sided_timezones_and_empty_results(self):
        for ident, wall in [('a', 1000), ('b', 1001), ('c', 1002)]:
            self.append(self.one / 'public_events.jsonl', {'id': ident, 'text': ident, 'wall': wall})
        for bounds, expected in [
            ({'since': '1970-01-01T00:16:41Z'}, ['b', 'c']),
            ({'since': '1970-01-01T08:16:41+08:00'}, ['b', 'c']),
            ({'since': '1970-01-01 08:16:41'}, ['b', 'c']),
            ({'until': '1970-01-01T01:16:41+01:00'}, ['a']),
            ({'since': '1970-01-02'}, []),
        ]:
            with self.subTest(bounds=bounds):
                report = collect_session(self.one, **bounds)
                self.assertEqual([row['id'] for row in report['public_text']], expected)

    def test_message_time_range_uses_canonical_event_before_filtering_reply_and_resume_copies(self):
        self.append(self.one / 'public_events.jsonl', {'id': 'public:shared', 'text': '范围前的消息', 'wall': 1000.1})
        self.append(self.two / 'public_events.jsonl', {'id': 'public:shared', 'text': '范围前的消息', 'wall': 1000.3})
        self.append(self.two / 'public_events.jsonl', {'id': 'public:shared', 'text': '范围内的更新', 'wall': 1000.4})
        with closing(sqlite3.connect(self.one / 'control.sqlite3')) as connection:
            connection.executescript('CREATE TABLE actions(id,kind,text,status,wall); CREATE TABLE replies(id,text,wall);')
            connection.execute('INSERT INTO replies VALUES(?,?,?)', ('shared', '范围前的消息', 1000.2))
            connection.execute('INSERT INTO actions VALUES(?,?,?,?,?)', (1, 'message', '用户消息', 'submitted', 1000.5))
            connection.commit()
        self.append(self.two / 'decisions.jsonl', {'i': 1, 'thought': '公开动作说明', 't': 0.6})
        report = collect_session(self.two, since='1970-01-01T08:16:40.150000', until='1970-01-01T08:16:41')
        self.assertEqual([row['text'] for row in report['public_text']], ['范围内的更新', '用户消息', '公开动作说明'])

    def test_message_time_range_cli_artifacts_agree_and_invalid_bounds_create_no_output(self):
        self.append(self.one / 'public_events.jsonl', {'id': 'before', 'text': 'RANGE_OUTSIDE_CANARY', 'wall': 1000})
        self.append(self.two / 'public_events.jsonl', {'id': 'inside', 'text': '范围内的可见消息', 'wall': 1001})
        self.append(self.two / 'public_events.jsonl', {'id': 'end', 'text': 'RANGE_OUTSIDE_CANARY', 'wall': 1002})
        cli = Path(__file__).resolve().parents[1] / 'tools/export_session.py'
        output = self.root / 'cli-range'
        command = [sys.executable, '-X', 'utf8', str(cli), '--run', str(self.two), '--local-only', '--output', str(output)]
        result = subprocess.run(command + ['--since', '1970-01-01 08:16:41', '--until', '1970-01-01T00:16:42Z'],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('公开文本 1 条', result.stdout)
        self.assertIn('仍为整个会话', result.stdout)
        markdown = (output / 'README.md').read_text(encoding='utf-8')
        self.assertIn('含起点、不含终点', markdown)
        self.assertIn('1970-01-01T08:16:41+08:00', markdown)
        self.assertIn('模拟调用与额度仍为整个会话', markdown)
        with (output / 'public_text.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual([row['id'] for row in csv.DictReader(stream)], ['inside'])
        report = json.loads((output / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual([row['id'] for row in report['public_text']], ['inside'])
        with zipfile.ZipFile(output / 'export.zip') as bundle:
            for name in bundle.namelist():
                self.assertNotIn('RANGE_OUTSIDE_CANARY', bundle.read(name).decode('utf-8-sig'))
        for args in [['--since', 'not-a-date'], ['--until', ''],
                     ['--since', '1970-01-02', '--until', '1970-01-01'],
                     ['--since', '1970-01-01T08:16:41', '--until', '1970-01-01T00:16:41Z']]:
            with self.subTest(args=args):
                invalid_output = self.root / 'invalid-range'
                result = subprocess.run(command[:-1] + [str(invalid_output)] + args,
                                        capture_output=True, text=True, encoding='utf-8', timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('导出失败', result.stderr)
                self.assertFalse(invalid_output.exists())
        empty = collect_session(self.one, until='1970-01-01T00:00:00Z')
        self.assertTrue(write_export(empty, self.root / 'empty-range').is_file())

    def test_chat_entry_finds_parent_runs_and_prefers_live_metrics_over_old_summary(self):
        chat = self.one / 'chats' / 'aaa'
        chat.mkdir(parents=True)
        self.put(chat / 'run_state.json', json.loads((self.one / 'run_state.json').read_text()))
        self.put(chat / 'metrics.json', {'elapsed_s': 19, 'written_wall': 1100})
        self.put(chat / 'summary.json', {'elapsed_s': 3, 'written_wall': 999})
        (chat / 'public_events.jsonl').touch()
        report = collect_session(chat)
        self.assertEqual(len(report['runs']), 3)
        self.assertEqual(report['time_summary']['elapsed_s'], 19)
        self.assertEqual(Path(report['runs'][-1]), chat.resolve())

    def test_fixed_reader_does_not_import_writable_working_directory_modules(self):
        trap = self.root / 'trap'
        trap.mkdir()
        marker = trap / 'side-effect'
        for module in ('json.py', 'sitecustomize.py'):
            (trap / module).write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n')
        code = reader_code() + '\nprint(json.dumps(scan_native(sys.argv[1], sys.argv[2])))\n'
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(self.root / 'empty-home'), self.sid],
                                cwd=trap, capture_output=True, text=True, check=True)
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(result.stdout)['calls'], [])

    def test_native_filter_artifact_canaries_and_csv_formula_escaping(self):
        home = self.root / 'home'
        path = home / 'sessions/2026/11/03' / (self.sid + '.jsonl')
        path.parent.mkdir(parents=True)
        self.append(path, {'type': 'session_meta', 'payload': {'id': self.sid}})
        self.append(path, {'payload': {'type': 'item_completed', 'item': {'type': 'Reasoning', 'text': 'PRIVATE_REASONING'}}})
        self.append(path, {'type': 'compacted', 'payload': {'message': 'PRIVATE_COMPACTION'}})
        self.append(path, {'payload': {'type': 'item_completed', 'item': {'type': 'DynamicToolCall', 'tool': 'read', 'content_items': [{'text': 'PRIVATE_TOOL_BODY'}]}}})
        self.append(path, {'payload': {'type': 'item_completed', 'started_at_ms': 1100000, 'item': {
            'type': 'DynamicToolCall', 'id': 'sim', 'tool': 'celeste_simulate', 'arguments': {'candidates': [['60,R']]},
            'success': True, 'content_items': [{'type': 'inputText', 'text': json.dumps({'cost': 3, 'receipt': 'r',
                'energy': {'balance': 17}, 'private_state': 'PRIVATE_SIM_STATE', 'results': [{'stop': 'done', 'frames': [[1, 'PRIVATE_TRAJECTORY']], 'last_frame': 'PRIVATE_TRAJECTORY'}]})}]}}})
        native = scan_native(home, self.sid)
        self.assertEqual(len(native['calls']), 1)
        self.assertEqual(native['calls'][0]['results'][0]['frames'], 1)
        self.assertIsNone(native['calls'][0]['duration_s'])
        self.put(self.two / 'sim_energy.json', {'receipts': [{'id': 'r', 'cost': 3, 'status': 'used'}]})
        self.append(self.two / 'public_events.jsonl', {'id': 'public1', 'text': '=1+1', 'wall': 1001})
        report = collect_session(self.two, native_reader=lambda state: native)
        archive = write_export(report, self.root / 'export')
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                self.assertNotIn('PRIVATE', bundle.read(name).decode('utf-8-sig'))
        with (archive.parent / 'public_text.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['text'], "'=1+1")
        self.assertEqual(json.loads((archive.parent / 'session.json').read_text(encoding='utf-8'))['public_text'][0]['text'], '=1+1')
        with self.assertRaises(FileExistsError):
            write_export(report, archive.parent)
        with self.assertRaises(ValueError):
            write_export(report, self.one / 'export')

    def test_native_failure_and_incomplete_lines_are_reported_without_invented_times(self):
        self.put(self.one / 'sim_energy.json', {'receipts': [{'id': 'r', 'cost': 3, 'status': 'used'}]})
        with (self.one / 'public_events.jsonl').open('ab') as stream:
            stream.write(b'not-json\n{"text":"partial')
        report = collect_session(self.two, native_reader=lambda state: (_ for _ in ()).throw(RuntimeError('missing container')))
        self.assertEqual(report['simulation_summary']['net_spent'], 3)
        self.assertIsNone(report['simulations'][0]['time'])
        self.assertTrue(any('未完成尾行' in warning for warning in report['warnings']))
        self.assertTrue(any('missing container' in warning for warning in report['warnings']))

    def test_container_reader_ownership_rejection_never_executes_or_starts(self):
        state = {'agent': 'tag', 'knowledge_profile': 'explore', 'session_id': self.sid}
        with patch('orchestrator.container.docker') as docker:
            docker.return_value.stdout = json.dumps([{'State': {'Running': True}, 'Config': {'Labels': {'celeste-ai-play.workspace_tag': 'foreign'}}}])
            with self.assertRaises(ValueError):
                read_container_simulations(state)
            self.assertEqual(len(docker.call_args_list), 1)
            self.assertEqual(docker.call_args.args[:2], ('inspect', 'celeste-agent-tag-explore'))

    def test_native_docker_timeout_keeps_local_export_available(self):
        self.append(self.one / 'public_events.jsonl', {'id': 'p', 'text': '可见文字', 'wall': 1001})
        def timeout(state):
            raise subprocess.TimeoutExpired('docker exec', 60)
        report = collect_session(self.one, native_reader=timeout)
        self.assertEqual(report['public_text'][0]['text'], '可见文字')
        self.assertTrue(any('timed out' in warning for warning in report['warnings']))
        self.assertTrue(write_export(report, self.root / 'timeout-export').is_file())

    def test_container_reader_has_internal_deadline_before_host_timeout(self):
        from types import SimpleNamespace
        state = {'agent': 'tag', 'knowledge_profile': 'explore', 'session_id': self.sid}
        metadata = [{'State': {'Running': True}, 'Config': {'Labels': {
            'celeste-ai-play.workspace_tag': 'tag-explore', 'celeste-ai-play.codex_home': '1'}}}]
        with patch('orchestrator.container.docker', side_effect=[SimpleNamespace(stdout=json.dumps(metadata)), SimpleNamespace(stdout='{"calls":[]}')]) as docker:
            self.assertEqual(read_container_simulations(state)['calls'], [])
        execution = docker.call_args
        self.assertIn('signal.alarm(40)', execution.args[-2])
        self.assertGreater(execution.kwargs['timeout'], 40)

    def test_file_truncation_during_snapshot_read_exits_without_spinning(self):
        class Truncated(io.BytesIO):
            def seek(self, offset, whence=0):
                actual = super().seek(offset, whence)
                return actual + 50 if whence == 2 else actual

            def readline(self, size=-1):
                self.read_count = getattr(self, 'read_count', 0) + 1
                if self.read_count > 3:
                    raise AssertionError('snapshot reader spun after EOF')
                return super().readline(size)

        path = self.root / 'truncated.jsonl'
        path.touch()
        warnings = []
        with patch.object(Path, 'open', return_value=Truncated(b'{"ok":1}\n')):
            self.assertEqual(list(rows(path, warnings)), [{'ok': 1}])
        self.assertTrue(any('截短' in warning for warning in warnings))


if __name__ == '__main__':
    unittest.main()
