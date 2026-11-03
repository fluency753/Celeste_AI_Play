"""Keep the real setup/reload lifecycle; replace only the native WebView2 boundary."""
from types import SimpleNamespace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from overlay.webview_host import disable_unused_response_observer


class Signal:
    def __init__(self):
        self.handlers = []
        self.removals = 0
        self.failure = False

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        if self.failure:
            raise RuntimeError('native unsubscription failed')
        self.handlers.remove(handler)
        self.removals += 1
        return self

    def emit(self):
        for handler in self.handlers:
            handler()

    def __len__(self):
        return len(self.handlers)


def browser_fixture():
    response = Signal()
    observed = []
    browser = SimpleNamespace(webview=SimpleNamespace(CoreWebView2=SimpleNamespace(WebResourceResponseReceived=response)),
                              on_web_resource_response=lambda: observed.append('response'))
    response += browser.on_web_resource_response
    return browser, response, observed


class WebviewHostTests(unittest.TestCase):
    def setUp(self):
        self.platform = patch('overlay.webview_host.sys.platform', 'win32')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.system = patch.dict('sys.modules', {'System': SimpleNamespace(Action=lambda callback: callback)})
        self.system.start()
        self.addCleanup(self.system.stop)
        self.browser, self.response, self.observed = browser_fixture()
        self.window = SimpleNamespace(events=SimpleNamespace(before_load=Signal(), response_received=Signal()),
                                      native=SimpleNamespace(browser=self.browser, InvokeRequired=True,
                                                             Invoke=lambda callback: callback()))

    def test_response_observer_removed_before_polling_and_not_again_on_reload(self):
        self.response.emit()
        self.assertEqual(self.observed, ['response'])
        disable_unused_response_observer(self.window)
        self.assertEqual(self.response.removals, 0)  # native initialization has not reached before_load
        self.window.events.before_load.emit()
        for _ in range(100):
            self.response.emit()
        self.window.events.before_load.emit()
        self.assertEqual(self.observed, ['response'])
        self.assertEqual(self.response.removals, 1)

    def test_new_native_browser_is_configured_after_recreation(self):
        disable_unused_response_observer(self.window)
        self.window.events.before_load.emit()
        browser, response, observed = browser_fixture()
        self.window.native.browser = browser
        self.window.events.before_load.emit()
        response.emit()
        self.assertEqual(observed, [])
        self.assertEqual(response.removals, 1)

    def test_new_core_in_same_browser_is_configured_and_ui_thread_needs_no_invoke(self):
        self.window.native.InvokeRequired = False
        self.window.native.Invoke = lambda callback: self.fail('Already on the UI thread')
        disable_unused_response_observer(self.window)
        self.window.events.before_load.emit()
        replacement, response, _ = browser_fixture()
        self.browser.webview.CoreWebView2 = replacement.webview.CoreWebView2
        response.handlers = [self.browser.on_web_resource_response]
        self.window.events.before_load.emit()
        self.assertEqual(response.removals, 1)

    def test_application_response_consumer_is_preserved_and_removal_can_run_when_unused(self):
        consumer = lambda: None
        self.window.events.response_received += consumer
        disable_unused_response_observer(self.window)
        with self.assertLogs('overlay.webview_host', level='WARNING'):
            self.window.events.before_load.emit()
        self.assertEqual(self.response.removals, 0)
        self.window.events.response_received -= consumer
        self.window.events.before_load.emit()
        self.assertEqual(self.response.removals, 1)

    def test_failed_native_removal_reports_warning_and_can_retry_next_navigation(self):
        self.response.failure = True
        disable_unused_response_observer(self.window)
        with self.assertLogs('overlay.webview_host', level='WARNING') as logs:
            self.window.events.before_load.emit()
        self.assertIn('无法停用', logs.output[0])
        self.response.emit()
        self.assertEqual(self.observed, ['response'])
        self.response.failure = False
        self.window.events.before_load.emit()
        self.response.emit()
        self.assertEqual(self.response.removals, 1)
        self.assertEqual(self.observed, ['response'])

    def test_other_renderer_and_non_windows_need_no_native_changes(self):
        self.window.native.browser = SimpleNamespace()
        disable_unused_response_observer(self.window)
        self.window.events.before_load.emit()
        self.assertEqual(self.response.removals, 0)
        with patch('overlay.webview_host.sys.platform', 'linux'):
            other = SimpleNamespace()
            disable_unused_response_observer(other)

    def test_console_registers_the_guard_before_starting_the_native_window(self):
        from overlay.console import main
        webview = MagicMock()
        webview.create_window.return_value = self.window
        webview.start.side_effect = self.window.events.before_load.emit
        with patch.dict('sys.modules', {'webview': webview}), \
             patch('sys.argv', ['overlay.console']), \
             patch('overlay.console.ensure_service'), \
             patch('overlay.console.threading.Thread'):
            self.assertEqual(main(), 0)
        self.assertEqual(self.response.removals, 1)

    @unittest.skipUnless(sys.platform == 'win32', 'Native WebView2 check requires Windows')
    def test_real_native_host_keeps_fetch_request_events_and_js_bridge_across_reload(self):
        import importlib.util
        if importlib.util.find_spec('webview') is None:
            self.skipTest('pywebview is not installed')
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'result.json'
            result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'calibration.webview_host_fixture', str(output)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                    text=True, encoding='utf-8', timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(output.read_text(encoding='utf-8'))
        self.assertNotIn('error', data)
        self.assertEqual(data['phases'], [{'ack': 'ack:ready', 'fetches': 100}] * 2)
        self.assertGreaterEqual(data['requests'], 200)
        self.assertEqual(data['responses'], 0)


if __name__ == '__main__':
    unittest.main()
