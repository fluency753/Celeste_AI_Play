"""RPC transport failures preserve diagnostics without starting a model or game."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from orchestrator.codex_rpc import CodexRPC, CodexRPCError


class CodexRPCReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def rpc_with_stream(self, data, *, limit=128):
        rpc = CodexRPC(self.directory)
        stream = asyncio.StreamReader(limit=limit)
        stream.feed_data(data)
        stream.feed_eof()
        rpc.proc = SimpleNamespace(stdout=stream)
        rpc.pending[1] = asyncio.get_running_loop().create_future()
        return rpc

    async def test_oversized_actual_readline_propagates_original_type_and_message_to_request(self):
        rpc = self.rpc_with_stream(b'x' * 100 + b'\n', limit=16)
        with self.assertRaises(ValueError):
            await rpc._read()
        with self.assertRaisesRegex(ConnectionError, 'ValueError:.*[Ll]imit'):
            await rpc.pending[1]

    async def test_malformed_protocol_json_identifies_parse_failure(self):
        rpc = self.rpc_with_stream(b'{broken json}\n')
        with self.assertRaises(ValueError):
            await rpc._read()
        with self.assertRaisesRegex(ConnectionError, 'JSONDecodeError'):
            await rpc.pending[1]

    async def test_clean_eof_reports_disconnect_to_every_unresolved_request(self):
        rpc = self.rpc_with_stream(b'')
        rpc.pending[2] = asyncio.get_running_loop().create_future()
        rpc.pending[3] = asyncio.get_running_loop().create_future()
        rpc.pending[3].set_result('already completed')
        await rpc._read()
        for ident in (1, 2):
            with self.subTest(ident=ident), self.assertRaises(ConnectionError):
                await rpc.pending[ident]
        self.assertEqual(await rpc.pending[3], 'already completed')

    async def test_unexpected_eof_records_exit_code_this_servers_stderr_and_container_state(self):
        import json
        from unittest.mock import patch
        log = self.directory / 'codex_stderr.log'
        log.write_text('上一个 App Server 的旧输出\n', encoding='utf-8')
        rpc = self.rpc_with_stream(b'')
        rpc._stderr_start = log.stat().st_size
        with log.open('a', encoding='utf-8') as stream:
            stream.write('ERROR codex_core: stream disconnected before completion\n')
        async def wait():
            return 137
        rpc.proc.wait, rpc.container = wait, 'celeste-agent-x'
        with patch('orchestrator.codex_container.container_state', return_value='exited 退出码=137 OOM=false'):
            await rpc._read()
        with self.assertRaises(ConnectionError) as caught:
            await rpc.pending[1]
        message = str(caught.exception)
        for part in ('进程退出码 137', '容器 exited', 'app_server_exits.jsonl'):
            self.assertIn(part, message)
        record = json.loads((self.directory / 'app_server_exits.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(record['exit_code'], 137)
        self.assertIn('stream disconnected', record['stderr_tail'])
        self.assertNotIn('旧输出', record['stderr_tail'])

    async def test_own_close_is_not_recorded_as_a_disconnect(self):
        rpc = self.rpc_with_stream(b'')
        rpc.closing = True
        await rpc._read()
        with self.assertRaises(ConnectionError):
            await rpc.pending[1]
        self.assertFalse((self.directory / 'app_server_exits.jsonl').exists())
        self.assertIsNone(rpc.disconnect)

    async def test_valid_response_remains_successful_when_stream_ends(self):
        rpc = self.rpc_with_stream(b'{"id":1,"result":{"thread":{"id":"original"}}}\n')
        await rpc._read()
        self.assertEqual(await rpc.pending[1], {'thread': {'id': 'original'}})

    async def test_explicit_refusal_retains_structured_error_for_retry_classification(self):
        import json
        from orchestrator.codex_recovery import is_model_unavailable
        error = {'code': -32000, 'message': 'Selected model is at capacity',
                 'data': {'httpStatusCode': 503}}
        rpc = self.rpc_with_stream((json.dumps({'id': 1, 'error': error}) + '\n').encode())
        await rpc._read()
        with self.assertRaises(CodexRPCError) as caught:
            await rpc.pending[1]
        self.assertEqual(caught.exception.error, error)
        self.assertTrue(is_model_unavailable(caught.exception))


if __name__ == '__main__':
    unittest.main()
