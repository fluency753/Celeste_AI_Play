"""Actual runtime event consumers; no bridge, model or user configuration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import threading
from urllib.request import urlopen

from overlay.tool_history import BoundedTail, ToolHistory, request_summary, safe_text


class ToolHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root/'runs'/'agent'/'one'
        self.run.mkdir(parents=True)
        self.home = self.root/'codex'
        self.sid = '01a177ec-21ce-72c3-aefa-fc55fa4c4a6a'
        self.native = self.home/'sessions'/'2026'/'10'/'26'/('rollout-'+self.sid+'.jsonl')
        self.native.parent.mkdir(parents=True)
        self.append(self.native, {'type': 'session_meta', 'payload': {'id': self.sid, 'cwd': str(self.run/'codex-control')}})
        (self.run/'run_state.json').write_text(json.dumps({'engine': 'codex', 'session_id': self.sid}))
        self.reader = ToolHistory(self.run, self.home)
        self.root_patch = patch('overlay.tool_history.ROOT', self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def append(self, path, item):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, ensure_ascii=False)+'\n')

    def begin(self, ident, name='celeste_observe', wall=10):
        self.append(self.run/'decision_timing.jsonl', {'event': 'tool_requested', 'tool_id': ident, 'tool': name, 'wall': wall})

    def complete(self, ident, success=True, args=None, name='celeste_observe'):
        self.append(self.native, {'payload': {'type': 'item_completed', 'item': {
            'type': 'DynamicToolCall', 'id': ident, 'tool': name, 'arguments': args or {'detail': 'geometry', 'region': [1, 2, 3, 4]},
            'success': success, 'content_items': [{'type': 'inputText', 'text': '{"frame":12,"returned":3,"matched_total":9}'}]}}})

    def app_event(self, method, item=None, **extra):
        self.append(self.run/'codex_events.jsonl', {'method': method, 'wall': 19,
            'params': {'threadId': self.sid, **({'item': item} if item else {}), **extra}})

    def test_run_owned_app_events_show_native_tools_and_safe_dynamic_projection(self):
        self.begin('dynamic', wall=10)
        self.app_event('tool/history', fact={'phase': 'requested', 'id': 'dynamic', 'tool': 'read',
            'text': '读取文件', 'parameters': {'file_path': '/kb/README.md', 'offset': '1'}})
        self.app_event('tool/history', fact={'phase': 'returned', 'id': 'dynamic', 'tool': 'read',
            'success': False, 'result': {'文字字符数': 35}})
        self.app_event('item/completed', {'type': 'dynamicToolCall', 'id': 'dynamic', 'tool': 'read', 'success': False})
        self.app_event('item/started', {'type': 'commandExecution', 'id': 'shell', 'command': 'python task.py',
            'cwd': '/workspace', 'status': 'inProgress'}, startedAtMs=11000)
        self.app_event('item/completed', {'type': 'commandExecution', 'id': 'shell', 'command': 'python task.py',
            'aggregatedOutput': 'PRIVATE_OUTPUT', 'exitCode': 2, 'status': 'completed', 'durationMs': 300}, completedAtMs=11300)
        self.app_event('item/completed', {'type': 'fileChange', 'id': 'edit', 'changes': [
            {'path': '/workspace/notes.md', 'diff': 'PRIVATE_PATCH'}], 'status': 'completed'}, completedAtMs=12000)
        result = self.reader.snapshot()
        self.assertFalse(result['warning'])
        rows = {r['id']: r for r in result['items']}
        self.assertEqual(rows['dynamic']['parameters']['file_path'], '/kb/README.md')
        self.assertEqual(rows['dynamic']['status'], 'failed')
        self.assertEqual(rows['shell']['wall'], 11)
        self.assertEqual(rows['shell']['duration_s'], .3)
        self.assertEqual(rows['shell']['status'], 'failed')
        self.assertEqual(rows['shell']['result']['程序退出码'], 2)
        self.assertEqual(rows['edit']['tool'], 'apply_patch')
        self.assertIn('/workspace/notes.md', rows['edit']['parameters']['path'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(self.reader.snapshot(), result)

    def test_app_events_foreign_thread_and_old_unknown_arguments_are_not_inferred(self):
        self.begin('old', 'celeste_act · private label', 10)
        self.app_event('item/completed', {'type': 'dynamicToolCall', 'id': 'old', 'tool': 'celeste_act', 'success': True})
        self.app_event('item/completed', {'type': 'commandExecution', 'id': 'foreign', 'command': 'PRIVATE'}, threadId='foreign')
        self.append(self.run/'codex_events.jsonl', {'method': 'item/completed', 'params': {'item': {
            'type': 'dynamicToolCall', 'id': 'unowned', 'tool': 'celeste_act', 'success': True}}})
        rows = self.reader.snapshot()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'returned')
        self.assertIsNone(rows[0]['parameters'])
        self.assertIsNone(rows[0]['result'])

    def test_delivery_before_call_and_replays_do_not_invent_receipt_or_duplicate_images(self):
        path = self.run/'tool_deliveries.jsonl'
        (self.run/'codex_events.jsonl').touch()
        for phase in ('sending', 'queued'):
            self.append(path, {'thread': self.sid, 'event': phase, 'receipt': 'r', 'call_id': 'read', 'images': 1})
        self.reader.snapshot()
        self.app_event('tool/history', fact={'phase': 'requested', 'id': 'read', 'tool': 'read', 'text': 'read', 'parameters': {}})
        item = self.reader.snapshot()['items'][0]
        self.assertEqual(item['attached_images'], 1)
        self.assertEqual(item['attachment_status'], 'queued')
        self.append(path, {'thread': self.sid, 'event': 'recorded', 'receipt': 'r'})
        self.append(path, {'thread': self.sid, 'event': 'queued', 'receipt': 'r'})
        item = self.reader.snapshot()['items'][0]
        self.assertEqual(item['attachment_status'], 'recorded')
        original = path.read_bytes(); path.write_bytes(b''); self.reader.snapshot(); path.write_bytes(original)
        self.assertEqual(self.reader.snapshot()['items'][0], item)

    def test_app_waits_for_timing_backlog_and_tolerates_malformed_native_changes(self):
        self.begin('old')
        self.begin('second', wall=11)
        self.reader.timing.budget = 180
        self.append(self.run/'codex_events.jsonl', {'method': 'item/completed', 'params': {'item': {
            'type': 'dynamicToolCall', 'id': 'second', 'tool': 'celeste_act', 'success': True}}})
        self.app_event('item/completed', {'type': 'fileChange', 'id': 'edit', 'changes': None, 'status': 'completed'})
        self.app_event('item/completed', {'type': {}, 'id': 'invalid'})
        for _ in range(10):
            result = self.reader.snapshot()
            if not result['loading']: break
        rows = {r['id']: r for r in result['items']}
        self.assertEqual(rows['second']['status'], 'returned')
        self.assertEqual(rows['edit']['status'], 'returned')
        self.assertNotIn('invalid', rows)

    def test_dynamic_item_without_success_does_not_erase_real_return_fact(self):
        self.app_event('tool/history', fact={'phase': 'returned', 'id': 'read', 'tool': 'read',
            'success': False, 'result': {'文字字符数': 5}})
        self.app_event('item/completed', {'type': 'dynamicToolCall', 'id': 'read', 'tool': 'read', 'status': 'completed'})
        self.assertEqual(self.reader.snapshot()['items'][0]['status'], 'failed')

    def test_native_completion_then_late_started_does_not_regress_status(self):
        self.app_event('item/completed', {'type': 'fileChange', 'id': 'edit', 'changes': [], 'status': 'completed'}, completedAtMs=12000)
        self.reader.snapshot()
        self.app_event('item/started', {'type': 'fileChange', 'id': 'edit', 'changes': [], 'status': 'inProgress'}, startedAtMs=11000)
        row = self.reader.snapshot()['items'][0]
        self.assertEqual(row['status'], 'returned')
        self.assertEqual(row['wall'], 11)

    def test_codex_scope_parameters_actual_result_and_separate_image_attachment(self):
        self.begin('mine')
        self.complete('unrelated')  # Same conversation reused by another run: not owned here.
        self.complete('mine')
        self.append(self.native, {'timestamp': 't1', 'payload': {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': '（应用观测附件；属于本次工具结果，不是新任务）'+json.dumps({'call_id': 'mine'})},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,PRIVATE'}]}})
        result = self.reader.snapshot()
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['status'], 'returned')
        self.assertEqual(item['text'], '区域碰撞精查')
        self.assertEqual(item['parameters']['region'], '[1, 2, 3, 4]')
        self.assertEqual(item['result']['本页对象数'], '3')
        self.assertEqual(item['attached_images'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(self.reader.snapshot(), result)
        self.assertEqual(ToolHistory(self.run, self.home).snapshot(), result)

    def test_legacy_rollout_fills_gaps_even_when_old_app_log_exists(self):
        self.begin('legacy')
        self.app_event('item/completed', {'type': 'dynamicToolCall', 'id': 'legacy',
            'tool': 'celeste_observe', 'success': True})
        self.complete('legacy')
        row = self.reader.snapshot()['items'][0]
        self.assertEqual(row['parameters']['detail'], 'geometry')
        self.assertEqual(row['result']['本页对象数'], '3')

    def test_timing_end_is_not_success_and_late_duplicate_begin_does_not_regress(self):
        self.begin('one')
        self.append(self.run/'decision_timing.jsonl', {'event': 'tool_completed', 'tool_id': 'one', 'tool_active_s': .4})
        self.assertEqual(self.reader.snapshot()['items'][0]['status'], 'unknown')
        self.complete('one', success=False)
        self.begin('one', wall=20)
        row = self.reader.snapshot()['items'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['wall'], 10)
        self.assertEqual(row['duration_s'], .4)

    def test_pending_tool_turn_end_is_interrupted_not_paused_or_success(self):
        self.begin('one')
        self.assertEqual(self.reader.snapshot()['items'][0]['status'], 'running')
        self.append(self.run/'decision_timing.jsonl', {'event': 'turn_ended', 'pending_tools': ['one']})
        self.assertEqual(self.reader.snapshot()['items'][0]['status'], 'interrupted')

    def test_claude_begin_and_result_use_same_safe_projection(self):
        (self.run/'run_state.json').write_text('{"engine":"claude"}')
        self.begin('claude', 'Read')
        events = self.run/'agent_events.jsonl'
        self.append(events, {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'claude', 'name': 'Read',
                     'input': {'file_path': '/workspace/game/latest_obs.json', 'offset': 1, 'limit': 20}}]}})
        self.assertEqual(self.reader.snapshot()['items'][0]['status'], 'running')
        self.append(events, {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'claude',
                     'is_error': True, 'content': 'private error body'}]}})
        row = self.reader.snapshot()['items'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['parameters']['file_path'], '/workspace/game/latest_obs.json')
        self.assertNotIn('private error body', json.dumps(row))

    def test_rejects_rollout_with_wrong_session_or_foreign_cwd(self):
        self.begin('one')
        self.native.write_text(json.dumps({'payload': {'id': self.sid, 'cwd': str(self.root/'foreign')}})+'\n')
        self.complete('one')
        result = self.reader.snapshot()
        self.assertIsNone(result['items'][0]['parameters'])
        self.assertTrue(result['warning'])

    def test_long_line_is_discarded_bounded_partial_waits_and_truncate_recovers(self):
        path = self.root/'tail.jsonl'
        path.write_bytes(b'x'*200+b'\n'+b'{"ok":1}\n'+b'{"partial":')
        tail = BoundedTail(path, budget=31, line_limit=80)
        rows = []
        for _ in range(20):
            rows += tail.read()
            if not tail.loading: break
        self.assertEqual(rows, [{'ok': 1}])
        self.assertEqual(tail.skipped, 1)
        self.assertLessEqual(len(tail.pending), 80)
        with path.open('ab') as stream: stream.write(b'2}\n')
        self.assertEqual(tail.read(), [{'partial': 2}])
        path.write_text('{"new":3}\n')
        self.assertEqual(tail.read(), [{'new': 3}])

    def test_parameters_do_not_expose_write_content_or_unbounded_secrets(self):
        label, params = request_summary('celeste_write', {'path': '/workspace/a', 'content': 'PRIVATE'})
        self.assertNotIn('PRIVATE', json.dumps(params))
        self.assertEqual(params['写入字符数'], 7)
        value = safe_text('Authorization: Bearer TOPSECRET password="two secrets" '+('a'*300))
        self.assertNotIn('TOPSECRET', value)
        self.assertNotIn('two secrets', value)
        self.assertNotIn('a'*200, value)

    def test_invalid_observation_detail_remains_visible_without_crashing(self):
        for detail in (['geometry'], {'detail': 'geometry'}, None):
            label, params = request_summary('celeste_observe', {'detail': detail})
            self.assertIn('参数异常', label)
            self.assertIn('detail', params)

    def test_parallel_tools_remain_separate_and_no_args_are_inferred_from_label(self):
        self.begin('one', 'celeste_bash · agent says checked collision', 10)
        self.begin('two', 'celeste_read · /workspace/notes.md', 11)
        result = self.reader.snapshot()['items']
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r['parameters'] is None for r in result))
        self.complete('two', args={'path': '/workspace/notes.md'}, name='celeste_read')
        self.assertEqual(self.reader.snapshot()['items'][0]['status'], 'running')

    def test_late_completion_after_eviction_is_safe_and_id_caches_are_bounded(self):
        for n in range(4200):
            self.begin(str(n), wall=n+1)
        self.reader.snapshot()
        self.assertLessEqual(len(self.reader.rows), 240)
        self.assertLessEqual(len(self.reader.owned), 4096)
        self.append(self.run/'decision_timing.jsonl', {'event': 'tool_completed', 'tool_id': '500', 'tool_active_s': 12})
        self.assertEqual(len(self.reader.snapshot()['items']), 50)

    def test_attachment_replay_after_file_truncate_does_not_double_count(self):
        self.begin('mine')
        self.complete('mine')
        self.append(self.native, {'timestamp': 'stable', 'payload': {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': '（应用观测附件；属于本次工具结果，不是新任务）'+json.dumps({'call_id': 'mine'})},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,PRIVATE'}]}})
        self.assertEqual(self.reader.snapshot()['items'][0]['attached_images'], 1)

        saved = self.native.read_bytes()
        self.native.write_bytes(b'')
        self.reader.snapshot()
        self.native.write_bytes(saved)
        self.assertEqual(self.reader.snapshot()['items'][0]['attached_images'], 1)

    def test_http_tools_endpoint_consumes_claude_events_without_touching_game(self):
        from overlay.server import make_handler
        (self.run/'run_state.json').write_text('{"engine":"claude"}')
        self.begin('read', 'Read')
        self.append(self.run/'agent_events.jsonl', {'message': {'content': [{'type': 'tool_use', 'id': 'read',
            'name': 'Read', 'input': {'file_path': '/workspace/game/collision.json', 'limit': 8}}]}})
        with patch('overlay.server.current_run', return_value=self.run):
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with urlopen('http://127.0.0.1:'+str(server.server_port)+'/hud/tools') as response:
                    data = json.load(response)
                self.assertEqual(data['run'], str(self.run))
                self.assertEqual(data['items'][0]['parameters']['file_path'], '/workspace/game/collision.json')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_malformed_timing_lines_do_not_break_the_batch(self):
        self.begin('one')
        self.append(self.run/'decision_timing.jsonl', {'event': 'turn_ended', 'pending_tools': 5})
        self.append(self.run/'decision_timing.jsonl', {'event': 'tool_completed', 'tool_id': {'bad': 1}})
        self.begin('two', wall=11)
        items = self.reader.snapshot()['items']
        self.assertEqual([item['id'] for item in items], ['one', 'two'])

    def test_rows_without_time_keep_reading_order(self):
        reader = ToolHistory(self.run, self.home)
        for ident in ('toolu_zzz', 'toolu_aaa', 'toolu_mmm'):
            reader._row(ident, 'Read')
        ordered = sorted(reader.rows.values(), key=lambda row: (row['wall'], row.get('seq', 0), row['id']))
        self.assertEqual([row['id'] for row in ordered], ['toolu_zzz', 'toolu_aaa', 'toolu_mmm'])

    def test_tools_endpoint_is_readable_only_from_local_pages(self):
        from urllib.request import Request
        from overlay.server import make_handler
        (self.run/'run_state.json').write_text('{"engine":"claude"}')
        with patch('overlay.server.current_run', return_value=self.run):
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = 'http://127.0.0.1:'+str(server.server_port)+'/hud/tools'
                with urlopen(Request(url, headers={'Origin': 'https://example.com'})) as response:
                    self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
                with urlopen(Request(url, headers={'Origin': 'http://127.0.0.1:47790'})) as response:
                    self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:47790')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
