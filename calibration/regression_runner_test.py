"""Formal CLI, real workers and unittest fixtures; no application services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tools import regression_runner
from tools.regression_runner import partition


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT/'tools/run_tests.py'


class RegressionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.tests = self.root/'tests'
        self.tests.mkdir()
        self.counter = 0

    def source(self, name, body):
        path = self.tests/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')

    def command(self, *options):
        self.counter += 1
        output = self.root/f'run-{self.counter}'
        return [sys.executable, '-X', 'utf8', str(ENTRY), '--start-directory', str(self.tests),
                '--output', str(output), '--timings-cache', str(self.root/'timings.json'), *options], output

    def run_cli(self, *options, expected=0):
        command, output = self.command(*options)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout+'\n'+result.stderr)
        return json.loads((output/'summary.json').read_text(encoding='utf-8')), output, result

    def basic(self):
        self.source('alpha_test.py', '''import unittest
class Alpha(unittest.TestCase):
    def test_pass(self): self.assertEqual(2+2,4)
    @unittest.skip('fixture skip')
    def test_skip(self): pass
if __name__=='__main__': raise RuntimeError('manual entry must never execute')
''')
        self.source('beta_test.py', '''import unittest
class Beta(unittest.TestCase):
    def test_pass(self): self.assertTrue(True)
''')

    def test_parallel_and_serial_collect_same_cases_and_do_not_run_file_main(self):
        self.basic()
        parallel, first, _ = self.run_cli('--workers', '2', '--heavy-workers', '1')
        serial, second, _ = self.run_cli('--serial')
        self.assertTrue(parallel['success'])
        self.assertEqual((parallel['tests_run'], parallel['skipped']), (3, 1))
        self.assertEqual((serial['tests_run'], serial['skipped']), (3, 1))
        p = json.loads((first/'plan.json').read_text())
        s = json.loads((second/'plan.json').read_text())
        self.assertEqual(p['collected_ids'], s['collected_ids'])
        self.assertEqual(len(p['groups']), 2)
        self.assertEqual(len(s['groups']), 1)
        ids = [json.loads((first/f'worker-{n}.json').read_text())['started_ids'] for n in range(2)]
        self.assertEqual(sorted(sum(ids, [])), sorted(p['collected_ids']))

    def test_worker_preserves_native_subprocess_default_encoding(self):
        # JSON is ASCII here, so the control interpreter can report its default
        # encoding without the caller's own -X utf8 influencing the assertion.
        baseline = subprocess.run([sys.executable, '-c',
            'import sys,locale,json;print(json.dumps([sys.flags.utf8_mode,locale.getpreferredencoding(False)]))'],
            capture_output=True, text=True, check=True)
        mode, encoding = json.loads(baseline.stdout)
        sample = 'é'
        try:
            payload = sample.encode(encoding)
        except UnicodeEncodeError:
            sample, payload = 'A', b'A'
        program = f'import sys;sys.stdout.buffer.write({payload!r})'
        self.source('encoding_test.py', f'''import subprocess,sys,unittest
class Cases(unittest.TestCase):
    def test_encoding(self):
        self.assertEqual(sys.flags.utf8_mode,{mode!r})
        result=subprocess.run([sys.executable,'-c',{program!r}],capture_output=True,text=True)
        self.assertEqual(result.stdout,{sample!r})
''')
        result, _, _ = self.run_cli()
        self.assertTrue(result['success'])

    def test_nested_package_discovery_and_class_skip_are_complete(self):
        self.source('pkg/__init__.py', '')
        self.source('pkg/nested_test.py', '''import unittest
class Skipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise unittest.SkipTest('whole class')
    def test_a(self): raise AssertionError('must not run')
    def test_b(self): raise AssertionError('must not run')
''')
        result, _, _ = self.run_cli()
        self.assertTrue(result['collection_match'])
        self.assertEqual(result['collected_tests'], 2)
        self.assertEqual(result['tests_run'], 0)
        self.assertEqual(result['skipped'], 1)  # unittest counts the fixture, not each blocked method.
        self.assertEqual(len(result['blocked_ids']), 2)
        self.assertEqual(result['skips'][0]['reason'], 'whole class')

    def test_package_load_tests_keeps_discovery_root_in_both_modes(self):
        self.source('pkg/__init__.py', '''import os
def load_tests(loader, tests, pattern):
    return loader.discover(os.path.dirname(__file__), pattern=pattern)
''')
        self.source('pkg/nested_test.py', '''import unittest
class Cases(unittest.TestCase):
    def test_ok(self): pass
''')
        for options in ((), ('--serial',)):
            result, output, _ = self.run_cli(*options)
            self.assertEqual((result['collected_tests'], result['tests_run']), (1, 1))
            plan = json.loads((output/'plan.json').read_text())
            self.assertEqual(plan['collected_ids'], ['pkg.nested_test.Cases.test_ok'])

    def test_import_level_skip_remains_a_skip_in_both_modes(self):
        self.source('platform_test.py', "import unittest\nraise unittest.SkipTest('platform unavailable')\n")
        for options in ((), ('--serial',)):
            result, _, _ = self.run_cli(*options)
            self.assertEqual((result['tests_run'], result['skipped']), (1, 1))
            self.assertEqual(result['skips'][0]['reason'], 'platform unavailable')

    def test_fixture_error_failure_and_unexpected_success_are_visible(self):
        self.source('broken_test.py', '''import unittest
class Broken(unittest.TestCase):
    @classmethod
    def setUpClass(cls): raise RuntimeError('fixture failed')
    def test_a(self): pass
class Cases(unittest.TestCase):
    def test_fail(self): self.fail('failure detail')
    @unittest.expectedFailure
    def test_unexpected(self): pass
''')
        result, _, process = self.run_cli(expected=1)
        self.assertFalse(result['success'])
        self.assertEqual(len(result['blocked_ids']), 1)
        self.assertIn('failure detail', process.stderr)
        self.assertIn('fixture failed', process.stderr)
        self.assertIn('UNEXPECTED SUCCESS', process.stderr)

    def test_worker_exit_without_report_is_failure(self):
        self.source('crash_test.py', '''import os,unittest
class Crash(unittest.TestCase):
    def test_exit(self): os._exit(7)
''')
        result, _, process = self.run_cli(expected=1)
        self.assertFalse(result['collection_match'])
        self.assertEqual(result['workers'][0]['exit_code'], 7)
        self.assertIn('未产生有效结果', process.stderr)

    def test_incomplete_success_report_cannot_pass(self):
        self.source('incomplete_test.py', '''import json,os,sys,unittest
from pathlib import Path
class Cases(unittest.TestCase):
    def test_exit(self):
        manifest=json.loads(Path(sys.argv[-1]).read_text())
        ids=[ident for entry in manifest['entries'] for ident in entry['ids']]
        Path(manifest['report']).write_text(json.dumps({'success':True,'complete':False,
            'collection_match':True,'collected_ids':ids}))
        os._exit(0)
''')
        result, _, process = self.run_cli(expected=1)
        self.assertTrue(result['collection_match'])
        self.assertFalse(result['success'])
        self.assertIn('未完成', process.stderr)

    def test_windows_force_cleanup_is_idempotent_after_job_was_closed(self):
        owner = regression_runner.OwnedWorker.__new__(regression_runner.OwnedWorker)
        owner.job = None
        owner.proc = Mock()
        owner.proc.poll.side_effect = [None, 0]
        with patch('tools.regression_process.os.name', 'nt'):
            owner.force_stop()
            owner.force_stop()
        owner.proc.terminate.assert_called_once()
        self.assertEqual(owner.proc.wait.call_count, 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX process group ownership')
    def test_exited_worker_cannot_leave_same_group_helper_running(self):
        pidfile = self.root/'orphan.pid'
        self.source('orphan_test.py', f'''import os,subprocess,sys,unittest
from pathlib import Path
class Cases(unittest.TestCase):
    def test_exit(self):
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        Path({str(pidfile)!r}).write_text(str(child.pid))
        os._exit(7)
''')
        self.run_cli(expected=1)
        path = Path('/proc')/pidfile.read_text()/'stat'
        deadline = time.monotonic()+3
        while path.exists() and path.read_text().split(')')[-1].split()[0]!='Z' and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(not path.exists() or path.read_text().split(')')[-1].split()[0]=='Z')

    def test_partial_worker_start_failure_cleans_owned_worker_and_reports_cause(self):
        self.basic()
        command, output = self.command('--workers', '2')
        original, owned = regression_runner.OwnedWorker, []

        def start(*args, **kwargs):
            if owned:
                raise OSError('fixture launch failure')
            worker = original(*args, **kwargs)
            owned.append(worker)
            return worker

        with patch.dict(sys.modules), patch.object(sys, 'path', list(sys.path)), \
                patch.object(regression_runner, 'OwnedWorker', side_effect=start):
            code = regression_runner.main(command[4:])
        self.assertEqual(code, 1)
        self.assertIsNotNone(owned[0].proc.poll())
        self.assertIsNone(owned[0].log)
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual(summary['phase'], 'worker_startup')
        self.assertIn('fixture launch failure', summary['errors'][0]['message'])

    def test_collection_error_and_empty_suite_do_not_pass(self):
        self.source('invalid_test.py', 'this is invalid Python !')
        result, _, _ = self.run_cli(expected=1)
        self.assertEqual(result['phase'], 'collection')
        self.run_cli('--pattern', 'absent_*.py', expected=1)

    def test_corrupt_cache_falls_back_and_partial_run_does_not_overwrite_it(self):
        self.basic()
        cache = self.root/'timings.json'
        cache.write_text('broken-json')
        result, _, process = self.run_cli('alpha_test.Alpha.test_pass')
        self.assertTrue(result['success'])
        self.assertEqual(cache.read_text(), 'broken-json')
        self.assertIn('忽略无效耗时', process.stdout)

    def test_uncollected_worker_side_effect_is_detected(self):
        # The parent sees two cases; a worker receives a different suite. It must
        # fail before running either suite rather than silently dropping a case.
        self.source('different_test.py', '''import sys,unittest
class A(unittest.TestCase):
    def test_a(self): pass
class B(unittest.TestCase):
    def test_b(self): pass
def load_tests(loader, tests, pattern):
    if '--worker' in sys.argv: return loader.loadTestsFromTestCase(A)
    return tests
''')
        result, _, process = self.run_cli(expected=1)
        self.assertFalse(result['success'])
        self.assertIn('收集', process.stderr)

    def test_cancel_finishes_fixture_and_does_not_start_next_case(self):
        entered, cleaned, forbidden = (self.root/name for name in ('entered', 'cleaned', 'forbidden'))
        self.source('cancel_test.py', f'''import time,unittest
from pathlib import Path
class Cases(unittest.TestCase):
    def tearDown(self): Path({str(cleaned)!r}).write_text('clean')
    def test_a(self):
        Path({str(entered)!r}).write_text('entered')
        time.sleep(.8)
    def test_b(self): Path({str(forbidden)!r}).write_text('must not run')
''')
        command, output = self.command('--cancel-grace-seconds', '5')
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+10
            while not entered.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(entered.exists())
            (output/'cancel').write_text('cancel')
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, (stdout, stderr))
            self.assertTrue(cleaned.exists())
            self.assertFalse(forbidden.exists())
            self.assertTrue(json.loads((output/'summary.json').read_text())['interrupted'])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object ownership')
    def test_force_cancel_reaps_detached_grandchild(self):
        import ctypes
        from ctypes import wintypes
        pidfile = self.root/'child.pid'
        self.source('force_test.py', f'''import subprocess,sys,time,unittest
from pathlib import Path
class Cases(unittest.TestCase):
    def test_hang(self):
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], creationflags=subprocess.DETACHED_PROCESS)
        Path({str(pidfile)!r}).write_text(str(child.pid))
        time.sleep(60)
''')
        command, output = self.command('--cancel-grace-seconds', '.1')
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        child = None
        try:
            deadline = time.monotonic()+10
            while not pidfile.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(pidfile.exists())
            child = api.OpenProcess(0x100000, False, int(pidfile.read_text()))
            self.assertTrue(child)
            (output/'cancel').write_text('cancel')
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, (stdout, stderr))
            self.assertEqual(api.WaitForSingleObject(child, 2000), 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
            if child:
                api.CloseHandle(child)

    def test_balancing_caps_heavy_modules_and_fills_all_workers(self):
        entries = [{'name':f'm{i}', 'module':f'm{i}', 'heavy':i<4, 'ids':[f'm{i}.Case.test']}
                   for i in range(12)]
        weights = {f'm{i}':10 for i in range(12)}
        groups = partition(entries, weights, 4, 2)
        self.assertEqual([g['predicted_s'] for g in groups], [30, 30, 30, 30])
        self.assertTrue(all(not entry['heavy'] for group in groups[2:] for entry in group['entries']))
        self.assertEqual(sorted(entry['name'] for group in groups for entry in group['entries']), sorted(weights))


if __name__ == '__main__':
    unittest.main()
