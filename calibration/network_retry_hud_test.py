"""Production metrics -> isolated HTTP server -> real HUD/console and native view model."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlparse

from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from overlay.modhud import view_model
from overlay.server import HudServer, make_handler
from calibration.network_retry_test import ERROR


class NetworkRetryHudTests(unittest.TestCase):
    def test_real_pages_show_retry_and_freeze_thinking_without_freezing_tools(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics = Metrics(root/'metrics.json', 'fixture', 'fixture', 'medium', 600)
            cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine='codex', sim=False)
            runner = CodexRunner(cfg, root, None, metrics, 'no-container')
            runner.session_id, runner.turn_id, runner.turn_active = 'session', 'turn', True
            metrics.turn_begin()
            metrics.update(status='thinking')
            runner._event('error', ERROR)
            # A concurrent local program still owns its tool interval.
            metrics.tool_begin('shell', label='Bash')
            metrics.update(status='tool', replaying=True)

            class Handler(make_handler(root)):
                def do_GET(self):
                    if self.path.startswith('/api/'):
                        self._json(200, {'control':{'connected':False}, 'running':False})
                    else:
                        try:
                            super().do_GET()
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            pass  # A page navigation can close an in-flight read-only request.

                def do_POST(self):
                    self._json(403, {'error':'isolated read-only test'})

                def log_message(self, *args):
                    pass

            server = HudServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with sync_playwright() as p:
                    try:
                        browser = p.chromium.launch()
                    except Exception as error:
                        self.skipTest(f'Chromium unavailable: {error}')
                    try:
                        page = browser.new_page()
                        page.route('**/*', lambda route:route.continue_()
                            if urlparse(route.request.url).netloc==urlparse(base).netloc else route.abort())
                        page.goto(base+'/hud')
                        page.wait_for_function("document.querySelector('#status').textContent.includes('模型连接中断')")
                        first = [page.locator('#'+key).inner_text() for key in ('thinking', 'tool')]
                        page.wait_for_timeout(1200)
                        second = [page.locator('#'+key).inner_text() for key in ('thinking', 'tool')]
                        self.assertEqual(first[0], second[0])
                        self.assertNotEqual(first[1], second[1])
                        snapshot = json.loads((root/'metrics.json').read_text())
                        before = view_model(snapshot, {}, [], time.time())
                        after = view_model(snapshot, {}, [], time.time()+2)
                        self.assertIn('模型连接中断', before.status)
                        self.assertEqual(dict((k,v) for k,v,*_ in before.stats)['思考'],
                                         dict((k,v) for k,v,*_ in after.stats)['思考'])
                        self.assertNotEqual(dict((k,v) for k,v,*_ in before.stats)['工具'],
                                            dict((k,v) for k,v,*_ in after.stats)['工具'])

                        page.goto(base+'/console')
                        render = """async () => {
                            const data = await (await fetch('/hud/state')).json();
                            renderRun(data.metrics);
                            return ['runStatus','mThinking','mTool'].map(id=>document.getElementById(id).textContent);
                        }"""
                        first = page.evaluate(render)
                        page.wait_for_timeout(1200)
                        second = page.evaluate(render)
                        self.assertIn('模型连接中断', first[0])
                        self.assertEqual(first[1], second[1])
                        self.assertNotEqual(first[2], second[2])

                        metrics.update(replaying=False)
                        metrics.tool_end('shell')
                        runner._event('item/agentMessage/delta', {
                            'threadId':'session', 'turnId':'turn', 'itemId':'public', 'delta':'恢复后的公开文本'})
                        first = page.evaluate(render)
                        self.assertEqual(first[0], '思考中')
                        page.wait_for_timeout(1200)
                        second = page.evaluate(render)
                        self.assertNotEqual(first[1], second[1])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
