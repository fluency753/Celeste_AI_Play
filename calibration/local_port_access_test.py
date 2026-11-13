"""本机端口只给本机页面与本机钩子用：HUD／控制台服务核对 Host、跨源读取只给本机页面；
工具门禁只认本机钩子的 JSON 请求。浏览器里的其他网页读不到、也取不走提醒。"""
import asyncio
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from overlay.server import make_handler


class HudServerAccessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Path(folder.name)))
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (self.server.shutdown(), worker.join(timeout=3), self.server.server_close()))
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def get(self, path, **headers):
        try:
            with urlopen(Request(self.base + path, headers=headers), timeout=15) as response:
                return response.status, response.headers.get('Access-Control-Allow-Origin'), response.read()
        except HTTPError as error:
            with error:
                return error.code, error.headers.get('Access-Control-Allow-Origin'), error.read()

    def test_requests_under_a_foreign_host_name_are_refused(self):
        # A page whose own domain was pointed at 127.0.0.1 still sends its domain as Host.
        for path in ('/hud/state', '/hud', '/console', '/api/console/about', '/hud/tools', '/api/control'):
            with self.subTest(path=path):
                status, allowed, body = self.get(path, Host=f'evil.example:{self.server.server_port}')
                self.assertEqual((status, allowed), (403, None))
                self.assertNotIn(b'"run"', body)
        request = Request(self.base + '/api/control', data=b'{"run":"x","kind":"pause"}',
                          headers={'Content-Type': 'application/json', 'Host': 'evil.example'})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request, timeout=3)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_local_names_are_served(self):
        for host in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}', 'localhost',
                     f'[::1]:{self.server.server_port}', 'LOCALHOST:1'):
            with self.subTest(host=host):
                self.assertEqual(self.get('/hud/state', Host=host)[0], 200)

    def test_other_sites_cannot_read_across_origins(self):
        for origin in ('https://example.com', 'http://example.com', 'null', 'http://127.0.0.1.example.com',
                       'https://127.0.0.1:47790'):
            for path in ('/hud/state', '/hud/history', '/hud/tools', '/hud'):
                with self.subTest(origin=origin, path=path):
                    status, allowed, _ = self.get(path, Origin=origin)
                    self.assertEqual((status, allowed), (200, None))

    def test_local_pages_on_another_port_can_read(self):
        for origin in ('http://127.0.0.1:47792', 'http://localhost:47790', 'http://127.0.0.1'):
            with self.subTest(origin=origin):
                self.assertEqual(self.get('/hud/state', Origin=origin)[:2], (200, origin))
                self.assertEqual(self.get('/hud/tools', Origin=origin)[:2], (200, origin))
        # 控制台的接口对本机的其他页面也不开放跨源读取（选一个不调外部程序的接口）。
        self.assertEqual(self.get('/api/console/stop-categories', Origin='http://127.0.0.1:47792')[:2], (200, None))

    def test_request_without_host_header_is_not_a_browser(self):
        import socket
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as wire:
            wire.sendall(b'GET /hud/state HTTP/1.0\r\n\r\n')
            head = wire.recv(200)
        self.assertIn(b' 200 ', head.split(b'\r\n', 1)[0])


class GateAccessTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, port, body, *, host='host.docker.internal', content_type='application/json'):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write((f'POST /gate HTTP/1.1\r\nHost: {host}\r\nContent-Type: {content_type}\r\n'
                          f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            await writer.wait_closed()
        head, _, payload = raw.partition(b'\r\n\r\n')
        return int(head.split()[1]), json.loads(payload)

    async def test_gate_only_serves_local_json_hooks_and_keeps_reminders_for_them(self):
        from starlette.applications import Starlette
        from orchestrator.http_service import http_service
        from orchestrator.tool_gate import add_gate_route

        class Notes:
            def __init__(self):
                self.pending = [(('after', 1), '（系统·留言重发）原文')]

            def peek(self):
                return tuple(self.pending)

            def acknowledge(self, offered):
                self.pending = [row for row in self.pending if row not in offered]

        class Session:
            note_reminders = Notes()

            @staticmethod
            def workspace_hook_guard(data):
                return None

        app = Starlette()
        add_gate_route(app, session=Session)
        body = b'{"tool_name":"Bash","tool_use_id":"one"}'
        async with http_service(app, port=0) as service:
            port = service.actual_port
            for host, content_type in (('host.docker.internal', 'text/plain'), ('evil.example', 'application/json'),
                                       ('127.0.0.1.evil.example', 'application/json'), ('host.docker.internal', '')):
                with self.subTest(host=host, content_type=content_type):
                    status, reply = await self.post(port, body, host=host, content_type=content_type)
                    self.assertEqual((status, reply['decision'], reply['context']), (403, 'deny', ''))
            self.assertEqual(len(Session.note_reminders.pending), 1)      # Strangers took nothing.
            for host in (f'host.docker.internal:{port}', '127.0.0.1', f'localhost:{port}'):
                status, reply = await self.post(port, body, host=host, content_type='application/json; charset=utf-8')
                self.assertEqual((status, reply['decision']), (200, 'allow'))
            self.assertEqual(Session.note_reminders.pending, [])          # The real hook received it once.


if __name__ == '__main__':
    unittest.main()
