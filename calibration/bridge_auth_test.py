"""桥接客户端的连接口令：有口令文件时第一行先报口令；旧的常驻核心不认识 auth 时照常继续；
新核心拒绝时给出能照着处理的说明。常驻核心一侧的行为由 calibration/ipc_auth（C#）核对。"""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from tools import bridge_client
from tools.bridge_client import BridgeAuthError, BridgeClient

TOKEN = 'ab' * 32


class Resident:
    """mode：guarded 像常驻核心 1.1.0（先核对口令）；legacy 像旧核心（所有指令都交给桥接，不认识 auth）。"""

    def __init__(self, mode, token=TOKEN):
        self.mode, self.token = mode, token
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.received = []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        stream = conn.makefile('rwb')

        def send(reply):
            stream.write((json.dumps(reply) + '\n').encode())
            stream.flush()
        first = True
        for line in stream:
            request = json.loads(line)
            self.received.append(request)
            if self.mode == 'guarded' and first:
                if request.get('cmd') != 'auth' or request.get('token') != self.token:
                    send({'id': request.get('id', 0), 'ok': False, 'error': 'Authentication required', 'auth': 'required'})
                    conn.close()
                    return
                send({'id': request['id'], 'ok': True, 'auth': True})
            elif request.get('cmd') == 'auth':
                send({'id': request['id'], 'ok': False, 'error': "unknown cmd 'auth'"})
            else:
                send({'id': request['id'], 'ok': True, 'obs': {'frame': 7}})
            first = False

    def close(self):
        self.server.close()


class BridgeAuthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tokens = Path(folder.name)
        patcher = patch.dict(os.environ, {bridge_client.TOKEN_DIR_ENV: str(self.tokens)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def resident(self, mode, token=TOKEN):
        server = Resident(mode, token)
        self.addCleanup(server.close)
        return server

    def write_token(self, port, text=TOKEN):
        (self.tokens / f'ipc-{port}.token').write_text(text + '\n', encoding='utf-8')

    def connect(self, port):
        client = BridgeClient(port=port, timeout=5)
        self.addCleanup(client.close)
        return client

    def test_token_is_presented_first_and_reconnect_presents_it_again(self):
        server = self.resident('guarded')
        self.write_token(server.port)
        client = self.connect(server.port)
        self.assertTrue(client.authenticated)
        self.assertEqual(client.call('observe', image=False)['obs']['frame'], 7)
        client.reconnect()
        self.assertTrue(client.authenticated)
        self.assertEqual(client.call('hello')['obs']['frame'], 7)
        self.assertEqual([row['cmd'] for row in server.received], ['auth', 'observe', 'auth', 'hello'])
        self.assertEqual(server.received[0]['token'], TOKEN)

    def test_legacy_core_with_a_leftover_token_file_still_works(self):
        server = self.resident('legacy')
        self.write_token(server.port)
        client = self.connect(server.port)
        self.assertFalse(client.authenticated)
        self.assertEqual(client.call('observe', image=False)['obs']['frame'], 7)

    def test_no_token_file_and_legacy_core_sends_no_auth(self):
        server = self.resident('legacy')
        client = self.connect(server.port)
        client.call('hello')
        self.assertEqual([row['cmd'] for row in server.received], ['hello'])

    def test_wrong_token_is_reported_at_connect(self):
        server = self.resident('guarded', token='cd' * 32)
        self.write_token(server.port)
        with redirect_stderr(StringIO()), self.assertRaisesRegex(BridgeAuthError, '没有接受连接口令') as raised:
            BridgeClient(port=server.port, timeout=5)
        self.assertIn(f'ipc-{server.port}.token', str(raised.exception))

    def test_core_that_needs_a_token_explains_when_none_is_found(self):
        server = self.resident('guarded')
        client = self.connect(server.port)
        with self.assertRaisesRegex(BridgeAuthError, '要求连接口令') as raised:
            client.call('hello')
        self.assertIn(str(self.tokens / f'ipc-{server.port}.token'), str(raised.exception))
        self.assertIsInstance(raised.exception, ConnectionError)

    def test_preflight_warns_when_the_core_has_no_token_protection(self):
        from contextlib import redirect_stdout
        from unittest.mock import Mock
        from orchestrator import bridge_requirements
        for authenticated, expected in ((False, True), (True, False)):
            client = Mock(authenticated=authenticated)
            client.call.return_value = {'process_id': 1}
            with patch('tools.bridge_client.BridgeClient', return_value=client), \
                 patch.object(bridge_requirements, 'validate_bridge'), \
                 patch('orchestrator.action_policy.require_capability'), \
                 patch('orchestrator.game_watch.require_live_steam'), \
                 redirect_stdout(StringIO()) as output:
                bridge_requirements.preflight_bridge(47774)
            self.assertEqual('连接口令保护' in output.getvalue(), expected, output.getvalue())

    def test_only_a_well_formed_token_is_used(self):
        for text in ('', 'short', 'AB' * 32, 'zz' * 32, 'ab' * 33):
            with self.subTest(text=text):
                self.write_token(4321, text)
                self.assertIsNone(bridge_client.read_token(4321))
        self.write_token(4321)
        self.assertEqual(bridge_client.read_token(4321), TOKEN)
        self.assertIsNone(bridge_client.read_token(4322))

    def test_token_directory_matches_the_resident_core(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': r'C:\Users\me\AppData\Local', 'XDG_DATA_HOME': '/data'}):
            os.environ.pop(bridge_client.TOKEN_DIR_ENV)
            with patch.object(bridge_client.sys, 'platform', 'win32'):
                self.assertEqual(bridge_client.token_dir(), Path(r'C:\Users\me\AppData\Local') / 'CelesteAIPlay')
            with patch.object(bridge_client.sys, 'platform', 'linux'):
                self.assertEqual(bridge_client.token_path(47774), Path('/data/CelesteAIPlay/ipc-47774.token'))
            with patch.object(bridge_client.sys, 'platform', 'darwin'):
                self.assertEqual(bridge_client.token_dir(),
                                 Path.home() / 'Library' / 'Application Support' / 'CelesteAIPlay')


if __name__ == '__main__':
    unittest.main()
