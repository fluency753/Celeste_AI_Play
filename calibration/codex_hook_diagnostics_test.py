"""Run the production hook as a process; only the local HTTP gate is substituted."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import socket
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HOOK = Path(__file__).resolve().parents[1] / 'agent-container/codex-tool-hook.py'


class HookDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.secret = 'private-token-"诊断"'
        self.env = {**os.environ, 'CODEX_HOME':str(self.home), 'MCP_PORT':'invalid',
                    'CELESTE_NATIVE_TOKEN':self.secret}

    def run_hook(self, event='PostToolUse', ident='call-1', timeout=8):
        bootstrap = ("import runpy,socket,sys; lookup=socket.getaddrinfo; "
            "socket.getaddrinfo=lambda host,*a,**kw:lookup("
            "'127.0.0.1' if host=='host.docker.internal' else host,*a,**kw); "
            "runpy.run_path(sys.argv[1],run_name='__main__')")
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', bootstrap, str(HOOK)],
            input=json.dumps({'hook_event_name':event, 'session_id':'session-1',
                'turn_id':'turn-1', 'tool_use_id':ident, 'tool_name':'Bash',
                'tool_input':{'command':'DO-NOT-LOG-COMMAND'}}), env=self.env,
            text=True, encoding='utf-8', capture_output=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)  # stderr diagnostics must not corrupt the protocol.
        if event == 'PostToolUse':
            self.assertIn('已执行', payload['systemMessage'])
            self.assertNotIn('未执行', payload['systemMessage'])
        else:
            self.assertEqual(payload['hookSpecificOutput']['permissionDecision'], 'deny')
        return result

    def rows(self):
        return [json.loads(line) for line in
                (self.home / 'diagnostics/codex-hook-errors.jsonl').read_text(encoding='utf-8').splitlines()]

    @contextmanager
    def gate(self, response, *, status=200, stall_body=False):
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                handler.rfile.read(int(handler.headers['Content-Length']))
                handler.send_response(status)
                handler.end_headers()
                if stall_body:
                    release.wait(40)
                else:
                    handler.wfile.write(response)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.env['MCP_PORT'] = str(server.server_port)
        try:
            yield
        finally:
            release.set()
            server.shutdown()
            thread.join()
            server.server_close()

    def test_original_exception_and_correlation_survive_both_hook_phases(self):
        for phase in ('PreToolUse', 'PostToolUse'):
            result = self.run_hook(phase)
            row = self.rows()[-1]
            self.assertEqual(row['hook_event_name'], phase)
            self.assertEqual((row['session_id'], row['turn_id'], row['tool_use_id']),
                             ('session-1', 'turn-1', 'call-1'))
            self.assertEqual(row['stage'], 'configuration')
            self.assertEqual(row['exception_type'], 'ValueError')
            self.assertIn('invalid', row['message'])
            self.assertIn('Traceback', row['traceback'])
            self.assertGreaterEqual(row['elapsed_s'], 0)
            self.assertGreater(row['wall'], 0)
            self.assertIn('[celeste-hook]', result.stderr)

    def test_connection_refusal_retains_underlying_error(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            # Bound but not listening: connection is deterministically refused.
            with self.gate(b''):
                self.env['MCP_PORT'] = str(port)
                self.run_hook()
        row = self.rows()[0]
        self.assertEqual(row['stage'], 'request')
        self.assertEqual(row['exception_type'], 'URLError')
        self.assertIn('ConnectionRefusedError', row['traceback'])

    def test_response_decode_failure_is_distinct_from_transport(self):
        with self.gate(b'broken-json'):
            self.run_hook()
        row = self.rows()[0]
        self.assertEqual(row['stage'], 'response_json')
        self.assertEqual(row['exception_type'], 'JSONDecodeError')

    def test_http_failure_retains_status(self):
        with self.gate(b'', status=503):
            self.run_hook()
        row = self.rows()[0]
        self.assertEqual(row['stage'], 'request')
        self.assertEqual(row['exception_type'], 'HTTPError')
        self.assertIn('503', row['message'])

    def test_actual_25_second_read_timeout_keeps_stage_and_exception(self):
        with self.gate(b'', stall_body=True):
            self.run_hook(timeout=35)
        row = self.rows()[0]
        self.assertEqual(row['stage'], 'response_json')
        self.assertEqual(row['exception_type'], 'TimeoutError')
        self.assertIn('timed out', row['message'])
        self.assertGreaterEqual(row['elapsed_s'], 24)

    def test_rejected_receipt_preserves_reason_and_redacts_token(self):
        with self.gate(json.dumps({'decision':'deny', 'reason':'rejected ' + self.secret,
                                  'context':'DO-NOT-LOG-CONTEXT'}).encode()):
            result = self.run_hook()
        row = self.rows()[0]
        self.assertEqual(row['stage'], 'decision')
        self.assertEqual(row['decision'], 'deny')
        self.assertEqual(row['reason'], 'rejected [REDACTED]')
        self.assertNotIn('context', row)
        for text in (result.stdout, result.stderr, json.dumps(self.rows(), ensure_ascii=False)):
            self.assertNotIn(self.secret, text)
            self.assertNotIn('DO-NOT-LOG', text)

    def test_exception_message_and_traceback_redact_token(self):
        self.env['MCP_PORT'] = self.secret
        result = self.run_hook()
        row = self.rows()[0]
        self.assertIn('[REDACTED]', row['message'])
        self.assertIn('[REDACTED]', row['traceback'])
        self.assertNotIn(self.secret, result.stderr)

    def test_unwritable_diagnostics_preserves_original_failure_and_result(self):
        (self.home / 'diagnostics').write_text('not a directory')
        result = self.run_hook()
        self.assertIn('diagnostic write failed', result.stderr)
        self.assertIn('ValueError', result.stderr)
        self.assertIn('configuration', result.stderr)

    def test_broken_stderr_cannot_suppress_post_result_with_either_file_state(self):
        helper = runpy.run_path(str(HOOK))

        class BrokenStderr:
            def write(self, value):
                raise BrokenPipeError('diagnostic stderr closed')

        closed = io.StringIO()
        closed.close()
        for index, (blocked, stderr) in enumerate((
                (False, BrokenStderr()), (False, closed),
                (True, BrokenStderr()), (True, closed))):
            root = self.home / str(index)
            root.mkdir()
            if blocked:
                (root / 'diagnostics').write_text('not a directory')
            output = io.StringIO()
            with patch.dict(os.environ, {**self.env, 'CODEX_HOME':str(root)}), \
                    patch('sys.stdin', io.StringIO('{"hook_event_name":"PostToolUse"}')), \
                    patch('sys.stdout', output), patch('sys.stderr', stderr):
                self.assertEqual(helper['main'](), 0)
            self.assertIn('已执行', json.loads(output.getvalue())['systemMessage'])
            if not blocked:
                row = json.loads((root / 'diagnostics/codex-hook-errors.jsonl').read_text(encoding='utf-8'))
                self.assertEqual(row['exception_type'], 'ValueError')

    def test_concurrent_processes_append_complete_records(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda n:self.run_hook(ident=f'call-{n}'), range(18)))
        self.assertEqual({row['tool_use_id'] for row in self.rows()}, {f'call-{n}' for n in range(18)})
        self.assertEqual(len(self.rows()), 18)


if __name__ == '__main__':
    unittest.main()
