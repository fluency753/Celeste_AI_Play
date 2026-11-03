"""HUD launch routing and native single-instance lifetime, isolated from live services."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import shutil
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from overlay.hud_startup import HudInstance, current_hud_url, find_hud_window
from overlay.window import main, open_window


class HudStartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run = self.root / 'runs/tag/current'
        self.run.mkdir(parents=True)
        (self.root / 'runs/latest.txt').write_text(str(self.run), encoding='utf-8')

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')

    def service(self, run):
        data = {'config': {}, 'metrics': {}, 'run': str(run) if run else None}
        visits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                visits.append(self.path)
                body = json.dumps(data).encode()
                self.send_response(200 if data else 503)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_port, data, visits

    def test_manifest_port_is_only_used_after_verifying_its_actual_run(self):
        port, _, visits = self.service(self.run)
        host = self.root / 'host'
        self.write(self.run / 'run_state.json', {'controller_host': str(host)})
        self.write(host / 'launch.json', {'arguments': ['--hud-port', str(port)]})
        with patch('overlay.hud_startup.DEFAULT_PORTS', ()), patch('overlay.console.ensure_service') as ensure:
            self.assertEqual(current_hud_url(self.root), f'http://127.0.0.1:{port}/hud')
        self.assertEqual(visits, ['/hud/state'])
        ensure.assert_not_called()

    def test_selected_chat_is_preferred_over_an_old_pinned_anchor(self):
        chat = self.run / 'chats/selected'
        self.write(chat / 'run_state.json', {'engine': 'codex', 'session_id': 'fixture'})
        self.write(chat / 'summary.json', {'completed': True})
        self.write(self.run / 'hud_session.json', {'relative': 'chats/selected'})
        old, _, _ = self.service(self.run)
        selected, _, _ = self.service(chat)
        with patch('overlay.hud_startup.DEFAULT_PORTS', (old, selected)), patch('overlay.console.ensure_service') as ensure:
            self.assertEqual(current_hud_url(self.root), f'http://127.0.0.1:{selected}/hud')
        ensure.assert_not_called()

    def test_fallback_starts_service_then_checks_identity_again(self):
        port, data, visits = self.service(self.run)
        good = dict(data)
        data.clear()
        with patch('overlay.hud_startup.DEFAULT_PORTS', (port,)), \
             patch('overlay.console.ensure_service', side_effect=lambda p: data.update(good)) as ensure:
            self.assertEqual(current_hud_url(self.root), f'http://127.0.0.1:{port}/hud')
        ensure.assert_called_once_with(port)
        self.assertEqual(visits, ['/hud/state', '/hud/state'])

    def test_healthy_default_service_pinned_to_another_run_is_not_silently_accepted(self):
        port, _, visits = self.service(self.root / 'other')
        with patch('overlay.hud_startup.DEFAULT_PORTS', (port,)), patch('overlay.console.ensure_service'):
            with self.assertRaisesRegex(RuntimeError, '其他运行'):
                current_hud_url(self.root)
        self.assertEqual(visits, ['/hud/state', '/hud/state'])

    def test_service_startup_failure_becomes_an_error_visible_to_the_gui_entry(self):
        port, data, _ = self.service(self.run)
        data.clear()
        with patch('overlay.hud_startup.DEFAULT_PORTS', (port,)), \
             patch('overlay.console.ensure_service', side_effect=SystemExit('service could not start')):
            with self.assertRaisesRegex(RuntimeError, 'service could not start'):
                current_hud_url(self.root)

    def test_invalid_manifest_port_and_empty_history_use_available_default(self):
        (self.root / 'runs/latest.txt').unlink()
        port, _, _ = self.service(None)
        with patch('overlay.hud_startup.DEFAULT_PORTS', (port,)), patch('overlay.console.ensure_service') as ensure:
            self.assertEqual(current_hud_url(self.root), f'http://127.0.0.1:{port}/hud')
        ensure.assert_not_called()
        (self.root / 'runs/latest.txt').write_text(str(self.run))
        host = self.root / 'host'
        self.write(self.run / 'run_state.json', {'controller_host': str(host)})
        self.write(host / 'launch.json', {'arguments': ['--hud-port=-5', '--hud-port', 'broken']})
        valid, _, _ = self.service(self.run)
        with patch('overlay.hud_startup.DEFAULT_PORTS', (valid,)):
            self.assertEqual(current_hud_url(self.root), f'http://127.0.0.1:{valid}/hud')

    def test_duplicate_opener_only_requests_restore_without_service_or_gui_startup(self):
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.is_owner = False
        with patch('sys.argv', ['overlay.window', '--url', 'http://another.test/hud']), \
             patch('overlay.window.sys.platform', 'win32'), \
             patch('overlay.window.HudInstance', return_value=instance), \
             patch('overlay.window.current_hud_url') as select, patch('overlay.window.open_window') as start:
            self.assertEqual(main(), 0)
        instance.request_restore.assert_called_once()
        instance.__exit__.assert_called_once()
        select.assert_not_called()
        start.assert_not_called()

    def test_owner_closes_handles_if_startup_fails_and_explicit_url_is_preserved(self):
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        with patch('sys.argv', ['overlay.window', '--url', 'http://explicit.test/hud']), \
             patch('overlay.window.sys.platform', 'win32'), \
             patch('overlay.window.HudInstance', return_value=instance), \
             patch('overlay.window.find_hud_window', return_value=None), patch('overlay.window.current_hud_url') as select, \
             patch('overlay.window.open_window', side_effect=RuntimeError('GUI failed')) as start:
            with self.assertRaisesRegex(RuntimeError, 'GUI failed'):
                main()
        self.assertEqual(start.call_args.args[0].url, 'http://explicit.test/hud')
        instance.__exit__.assert_called_once()
        select.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Native named handles require Windows')
    def test_legacy_window_rejection_releases_instance_before_showing_error_and_retry(self):
        with patch('sys.argv', ['overlay.window']), \
             patch('overlay.window.HudInstance', side_effect=lambda: HudInstance(root=self.root)), \
             patch('overlay.window.find_hud_window', return_value=42), \
             patch('overlay.window.current_hud_url') as select:
            with self.assertRaisesRegex(RuntimeError, '托盘'):
                main()
        # This is the state in which the outer GUI error handler shows its modal.
        with HudInstance(root=self.root) as retry:
            self.assertTrue(retry.is_owner)
        select.assert_not_called()
        with patch('sys.argv', ['overlay.window', '--url', 'http://fixture.test/hud']), \
             patch('overlay.window.HudInstance', side_effect=lambda: HudInstance(root=self.root)), \
             patch('overlay.window.find_hud_window', return_value=None), \
             patch('overlay.window.open_window', return_value=0) as start:
            self.assertEqual(main(), 0)
        start.assert_called_once()

    def test_automatic_start_uses_shared_window_identity_and_only_owns_its_new_process(self):
        from orchestrator.run import start_hud_window
        with patch('orchestrator.run.sys.platform', 'win32'), \
             patch('overlay.hud_startup.find_hud_window', return_value=None) as find, \
             patch('orchestrator.run.importlib.util.find_spec', return_value=object()), \
             patch('orchestrator.run.subprocess.Popen') as spawn:
            process = start_hud_window(12345, self.root)
        find.assert_called_once()
        self.assertIs(process, spawn.return_value)
        self.assertIn('http://127.0.0.1:12345/hud', spawn.call_args.args[0])
        spawn.call_args.kwargs['stdout'].close()
        with patch('orchestrator.run.sys.platform', 'win32'), \
             patch('overlay.hud_startup.find_hud_window', return_value=42), \
             patch('orchestrator.run.subprocess.Popen') as spawn:
            self.assertIsNone(start_hud_window(12345, self.root))
        spawn.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows callback ABI')
    def test_window_enumeration_skips_the_first_same_title_dialog_then_finds_real_form(self):
        user = MagicMock()
        title = 'Isolated fixture HUD'

        def enumerate_windows(callback, data):
            for handle in (17, 42):
                if not callback(handle, data):
                    return False
            return True

        def caption(handle, buffer, size):
            buffer.value = title
            return len(title)

        def class_name(handle, buffer, size):
            buffer.value = '#32770' if handle == 17 else 'WindowsForms10.Window.8.fixture'
            return len(buffer.value)

        user.EnumWindows.side_effect = enumerate_windows
        user.GetWindowTextW.side_effect = caption
        user.GetClassNameW.side_effect = class_name
        with patch('overlay.hud_startup.ctypes.WinDLL', return_value=user):
            self.assertEqual(find_hud_window(title), 42)

    def test_explicit_unavailable_service_fails_with_a_bounded_wait(self):
        with patch.dict('sys.modules', {'webview': MagicMock()}), \
             patch('overlay.window.set_dpi_awareness'), \
             patch('urllib.request.urlopen', side_effect=OSError('not listening')), \
             patch('overlay.window.time.monotonic', side_effect=[0, 16]):
            with self.assertRaisesRegex(RuntimeError, '15 秒'):
                from types import SimpleNamespace
                open_window(SimpleNamespace(url='http://hud.test/hud'))

    @unittest.skipUnless(sys.platform == 'win32', 'Native named handles require Windows')
    def test_real_native_instance_retains_startup_wake_and_releases_for_retry(self):
        with HudInstance(root=self.root) as owner:
            self.assertTrue(owner.is_owner)
            with HudInstance(root=self.root) as second:
                self.assertFalse(second.is_owner)
                second.request_restore()
            self.assertTrue(owner.wake_requested())
            self.assertFalse(owner.wake_requested())
        owner.close()  # idempotent
        with HudInstance(root=self.root) as replacement:
            self.assertTrue(replacement.is_owner)

    @unittest.skipUnless(sys.platform == 'win32', 'Native named handles require Windows')
    def test_concurrent_openers_have_one_owner(self):
        barrier = threading.Barrier(6)
        instances = []

        def create():
            barrier.wait(timeout=5)
            instances.append(HudInstance(root=self.root))

        threads = [threading.Thread(target=create) for _ in range(6)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(len(instances), 6)
            self.assertEqual(sum(instance.is_owner for instance in instances), 1)
        finally:
            for instance in instances:
                instance.close()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows GUI executable check')
    def test_built_exe_uses_its_own_directory_and_preserves_quoted_arguments(self):
        exe = Path(__file__).resolve().parents[1] / 'CelesteAIHud.exe'
        if not exe.is_file():
            self.skipTest('Build the HUD launcher first')
        # A real temporary venv and a tiny receiver test the compiled launcher;
        # no HUD page, live controller, user config or game is opened.
        import venv
        target = self.root / 'fixture checkout with spaces'
        target.mkdir()
        venv.EnvBuilder(with_pip=False).create(target / '.venv')
        overlay = target / 'overlay'
        overlay.mkdir()
        (overlay / '__init__.py').touch()
        (overlay / 'window.py').write_text(
            'import json,os,sys\nfrom pathlib import Path\n'
            'Path("received.tmp").write_text(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:],"exe":sys.executable,"pid":os.getpid(),"parent":os.getppid()}))\n'
            'Path("received.tmp").replace("received.json")\n',
            encoding='utf-8')
        executable = target / exe.name
        shutil.copy2(exe, executable)
        argv = ['--url', 'http://hud.test/hud?text=two words&value=1', '--edit']
        process = subprocess.run([str(executable), *argv], cwd=self.root, timeout=5,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(process.returncode, 0)
        marker = target / 'received.json'
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(marker.exists())
        received = json.loads(marker.read_text())
        # The thin EXE exits before Python; wait for both Python and its venv
        # redirector to release their executable files before temp cleanup.
        import ctypes
        import ctypes.wintypes as wt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        for pid in (received['pid'], received['parent']):
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                finally:
                    kernel.CloseHandle(handle)
        self.assertEqual(Path(received['cwd']), target)
        self.assertEqual(received['args'], argv)
        self.assertEqual(Path(received['exe']).parent, target / '.venv/Scripts')


if __name__ == '__main__':
    unittest.main()
