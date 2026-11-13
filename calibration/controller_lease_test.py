"""CLI controllers hold the same controller mutex as background hosts; no real Windows objects."""
import ast
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import background
from orchestrator.game_process import CONTROLLER_MUTEX, ERROR_ALREADY_EXISTS, hold_cli_controller_lease


class FakeKernel:
    """Named-mutex boundary: existing names report ERROR_ALREADY_EXISTS like CreateMutexW."""
    def __init__(self, existing=0):
        self.existing = existing      # number of upcoming calls that find the name already present
        self.created, self.closed = [], []
        self.error = 0

    def last_error(self):
        return self.error

    def CreateMutexW(self, security, owner, name):
        handle = 1000 + len(self.created)
        self.created.append(name)
        self.error = ERROR_ALREADY_EXISTS if self.existing else 0
        self.existing = max(0, self.existing - 1)
        return handle

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


class ControllerLeaseTests(unittest.TestCase):
    def test_background_host_child_does_not_reacquire_the_hosts_lease(self):
        kernel = FakeKernel(existing=99)
        self.assertIsNone(hold_cli_controller_lease({'CELESTE_CONTROLLER_HOST': r'C:\repo\local\background\x'},
                                                    kernel=kernel, last_error=kernel.last_error))
        self.assertEqual(kernel.created, [])

    def test_cli_controller_holds_the_shared_mutex_until_exit(self):
        kernel = FakeKernel()
        handle = hold_cli_controller_lease({}, kernel=kernel, last_error=kernel.last_error)
        self.assertEqual(kernel.created, [CONTROLLER_MUTEX])
        self.assertEqual(kernel.closed, [])          # retained for the process lifetime
        self.assertIsNotNone(handle)

    def test_second_controller_is_refused_and_releases_its_probe_handle(self):
        kernel = FakeKernel(existing=99)
        with self.assertRaisesRegex(RuntimeError, '已有主控'):
            hold_cli_controller_lease({}, kernel=kernel, last_error=kernel.last_error, attempts=3, delay=0)
        self.assertEqual(len(kernel.created), 3)
        self.assertEqual(len(kernel.closed), 3)

    def test_transient_existence_check_by_another_tool_is_tolerated(self):
        kernel = FakeKernel(existing=1)
        handle = hold_cli_controller_lease({}, kernel=kernel, last_error=kernel.last_error, attempts=3, delay=0)
        self.assertEqual(len(kernel.created), 2)
        self.assertEqual(kernel.closed, [1000])
        self.assertEqual(handle, 1001)

    def test_cli_entry_point_claims_the_lease_before_main(self):
        from orchestrator import run
        with patch('orchestrator.game_process.hold_cli_controller_lease', side_effect=RuntimeError('已有主控在运行')), \
             self.assertRaisesRegex(SystemExit, '已有主控'):
            run.claim_controller_lease()
        tree = ast.parse(Path(run.__file__).read_text(encoding='utf-8'))
        block = next(node for node in tree.body if isinstance(node, ast.If) and 'main' in ast.unparse(node.test)
                     and '__name__' in ast.unparse(node.test))
        calls = [ast.unparse(node.func) for node in ast.walk(block) if isinstance(node, ast.Call)]
        self.assertLess(calls.index('claim_controller_lease'), calls.index('main'))


class LaunchRejectionTests(unittest.TestCase):
    """Only a definitive Windows refusal is reported as 'no process was created'."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'local/background').mkdir(parents=True)
        patcher = patch.object(background, 'ROOT', self.root)
        patcher.start(); self.addCleanup(patcher.stop)

    def launch(self, **run):
        fake_os = SimpleNamespace(name='nt', environ=os.environ)
        directory = self.root / 'local/background/attempt'
        with patch.object(background, 'os', fake_os), \
             patch.object(background.subprocess, 'CREATE_NO_WINDOW', 0, create=True), \
             patch.object(background.subprocess, 'run', **run):
            try:
                return background.launch(['--agent', 'a'], directory=directory)
            finally:
                self.assertTrue(directory.is_dir())   # restart must handle an existing directory

    def test_transient_status_read_conflict_waits_for_the_host_instead_of_failing(self):
        """Windows can refuse a read while the host replaces status.json; the launcher keeps waiting."""
        from orchestrator import session_store
        directory = self.root / 'local/background/attempt'
        def started(*args, **kwargs):
            (directory / 'status.json').write_text('{"state": "running", "host_pid": 7}', encoding='utf-8')
            return SimpleNamespace(stdout='{"ReturnValue":0,"ProcessId":7}')
        real, calls = session_store.load_json, []
        def flaky(path, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, 'The process cannot access the file because it is being used by another process')
            return real(path, **kwargs)
        with patch.object(session_store, 'load_json', side_effect=flaky):
            folder, data = self.launch(side_effect=started)
        self.assertEqual((folder, data['state']), (directory, 'running'))
        self.assertEqual(len(calls), 2)

    def test_wmi_failure_code_is_a_definitive_rejection(self):
        with self.assertRaises(background.LaunchRejected):
            self.launch(return_value=SimpleNamespace(stdout='{"ReturnValue":9,"ProcessId":null}'))

    def test_powershell_error_is_a_definitive_rejection(self):
        failure = subprocess.CalledProcessError(1, 'powershell.exe', output='', stderr='Invoke-CimMethod failed')
        with self.assertRaisesRegex(background.LaunchRejected, 'Invoke-CimMethod'):
            self.launch(side_effect=failure)

    def test_timeout_is_not_reported_as_rejected(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.launch(side_effect=subprocess.TimeoutExpired('powershell.exe', 30))
        self.assertNotIsInstance(caught.exception, background.LaunchRejected)

    def test_slow_machine_is_waited_for_instead_of_reported_as_a_failed_launch(self):
        """A cold or busy machine can take tens of seconds to start PowerShell and then the host; both
        waits are 'stuck' detectors, far above the old 30 s and 10 s. The clock here is the test's own."""
        directory = self.root / 'local/background/attempt'
        clock = SimpleNamespace(now=1000.0)
        def sleep(seconds):
            clock.now += seconds
            if clock.now >= 1045 and not (directory / 'status.json').exists():     # host reports in after 45 s
                (directory / 'status.json').write_text('{"state": "running", "host_pid": 7}', encoding='utf-8')
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep, strftime=time.strftime, time=time.time)
        run = MagicMock(return_value=SimpleNamespace(stdout='{"ReturnValue":0,"ProcessId":7}'))
        with patch.object(background, 'time', fake_time):
            folder, data = self.launch(new=run)
        self.assertEqual((folder, data['state']), (directory, 'running'))
        self.assertEqual(run.call_args.kwargs['timeout'], background.POWERSHELL_WAIT_S)
        self.assertGreaterEqual(background.POWERSHELL_WAIT_S, 90)

    def test_host_that_never_reports_is_still_given_up_on(self):
        clock = SimpleNamespace(now=0.0)
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, strftime=time.strftime, time=time.time,
                                    sleep=lambda seconds: setattr(clock, 'now', clock.now + seconds))
        with patch.object(background, 'time', fake_time), self.assertRaisesRegex(RuntimeError, '宿主未确认就绪'):
            self.launch(return_value=SimpleNamespace(stdout='{"ReturnValue":0,"ProcessId":7}'))
        self.assertGreaterEqual(clock.now, background.HOST_READY_WAIT_S)


if __name__ == '__main__':
    unittest.main()
