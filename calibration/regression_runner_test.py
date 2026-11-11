"""Parallel regression entry: discover-equivalent imports, failure reporting and timing cache."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tools import regression_runner as runner


def write(folder, name, body):
    (folder / name).write_text(textwrap.dedent(body), encoding='utf-8')


class RegressionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tests = self.root / 'suite'
        self.tests.mkdir()
        write(self.tests, 'helper_module.py', 'VALUE = 7\n')
        write(self.tests, 'alpha_test.py', f'''
            import os, unittest
            import helper_module                      # sibling import, as under discover -s
            from tools import regression_runner       # repository root stays importable

            class AlphaTests(unittest.TestCase):
                def test_imports_and_cwd(self):
                    self.assertEqual(helper_module.VALUE, 7)
                    self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath({str(runner.ROOT)!r}))
            ''')
        write(self.tests, 'gamma_test.py', '''
            import unittest

            class GammaTests(unittest.TestCase):
                def test_ok(self):
                    pass

                @unittest.skip('fixture')
                def test_skipped(self):
                    pass
            ''')

    def run_main(self, *extra, start=None):
        result = subprocess.run(
            [sys.executable, str(runner.ROOT / 'tools' / 'run_tests.py'),
             '--start-directory', str(start or self.tests), '--output', str(self.root / 'out'),
             '--timings-cache', str(self.root / 'times.json'), *extra],
            cwd=runner.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding='utf-8', timeout=30,
        )
        return result.returncode, result.stdout

    def test_parallel_run_passes_counts_and_caches_module_times(self):
        code, out = self.run_main('--workers', '2')
        self.assertEqual(code, 0, out)
        self.assertIn('OK run=3 failures=0 errors=0 skipped=1', out)
        self.assertEqual(set(json.loads((self.root / 'times.json').read_text(encoding='utf-8'))),
                         {'alpha_test', 'gamma_test'})
        self.assertEqual(sorted(p.name for p in (self.root / 'out').glob('worker-*.log')),
                         ['worker-0.log', 'worker-1.log'])

    def test_failure_and_import_error_fail_the_run_and_show_the_report(self):
        write(self.tests, 'delta_test.py', '''
            import unittest

            class DeltaTests(unittest.TestCase):
                def test_broken(self):
                    self.assertEqual(1, 2, 'visible failure detail')
            ''')
        write(self.tests, 'omega_test.py', 'import module_that_does_not_exist\n')
        code, out = self.run_main('--workers', '3')
        self.assertEqual(code, 1)
        self.assertIn('visible failure detail', out)
        self.assertIn('module_that_does_not_exist', out)
        self.assertIn('FAILED', out)

    def test_parallel_and_serial_discover_nested_package_failures(self):
        package = self.tests / 'pkg'
        package.mkdir()
        write(package, '__init__.py', '')
        write(package, 'nested_test.py', '''
            import unittest

            class NestedTests(unittest.TestCase):
                def test_nested(self):
                    self.fail('nested package failure')
            ''')
        parallel_code, parallel = self.run_main('--workers', '2')
        serial_code, serial = self.run_main('--serial')
        self.assertEqual(parallel_code, 1, parallel)
        self.assertEqual(serial_code, 1, serial)
        self.assertIn('FAILED run=4 failures=1 errors=0 skipped=1', parallel)
        self.assertIn('Ran 4 tests', serial)
        for report in (parallel, serial):
            self.assertIn('pkg.nested_test', report)
            self.assertIn('nested package failure', report)

    def test_named_tests_and_serial_run_in_process(self):
        code, _ = self.run_main('suite.gamma_test.GammaTests.test_ok')
        self.assertEqual(code, 0)
        code, _ = self.run_main('--serial')
        self.assertEqual(code, 0)
        self.assertFalse((self.root / 'out').exists())

    def test_worker_imports_every_module_before_running_like_discover(self):
        """A module first imported while another test patches a global must not keep the patched value."""
        lone = self.root / 'lone'
        lone.mkdir()
        write(lone, 'holder_module.py', "ROOT = 'real'\n")
        write(lone, 'derived_module.py', "import holder_module\nVALUE = holder_module.ROOT\n")
        write(lone, 'a_patch_test.py', '''
            import unittest
            from unittest.mock import patch
            import holder_module

            class PatchTests(unittest.TestCase):
                def test_lazy_import_under_patch(self):
                    with patch.object(holder_module, 'ROOT', 'temporary'):
                        import derived_module
            ''')
        write(lone, 'b_check_test.py', '''
            import unittest
            import derived_module

            class CheckTests(unittest.TestCase):
                def test_value_bound_at_import(self):
                    self.assertEqual(derived_module.VALUE, 'real')
            ''')
        code, out = self.run_main('--workers', '1', start=lone)
        self.assertEqual(code, 0, out)

    def test_partition_balances_by_history_and_guesses_unknown_modules(self):
        groups = runner.partition(['a', 'b', 'c', 'd'], {'a': 10.0, 'b': 6.0, 'c': 4.0}, 2)
        self.assertEqual(sorted(sorted(g['modules']) for g in groups), [['a', 'c'], ['b', 'd']])
        self.assertEqual(len(runner.partition(['a'], {}, 4)), 1)


if __name__ == '__main__':
    unittest.main()
