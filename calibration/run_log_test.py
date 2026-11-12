"""运行目录里的主控记录：带时间戳的输出副本与异常退出证据。"""
import io
from contextlib import redirect_stderr
from pathlib import Path
import re
import tempfile
import unittest

from orchestrator import run_log

STAMPED = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ')


class Broken(io.StringIO):
    def write(self, text):
        raise OSError('disk full')


class RunLogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def test_output_before_and_after_attach_is_stamped_per_line_and_still_printed(self):
        log, original = run_log.RunLog(), io.StringIO()
        self.addCleanup(log.close)
        stream = run_log._Mirror(original, log)
        stream.write('[run] 预检\n')
        stream.write('部分')                      # A line written in pieces gets one stamp.
        stream.write('一行\n第二行\n')
        path = log.attach(self.directory)
        print('[run] 日志目录', file=stream, flush=True)
        self.assertEqual(original.getvalue(), '[run] 预检\n部分一行\n第二行\n[run] 日志目录\n')
        lines = path.read_text(encoding='utf-8').splitlines()
        self.assertIn('==== 主控进程', lines[0])
        self.assertEqual([STAMPED.sub('', line) for line in lines[1:]],
                         ['[run] 预检', '部分一行', '第二行', '[run] 日志目录'])
        self.assertTrue(all(STAMPED.match(line) for line in lines))

    def test_unwritable_log_never_breaks_printing(self):
        log, original = run_log.RunLog(), io.StringIO()
        log.attach(self.directory)
        log.close()
        log.file = Broken()
        stream = run_log._Mirror(original, log)
        stream.write('还能打印\n')
        stream.write('之后也能\n')
        self.assertEqual(original.getvalue(), '还能打印\n之后也能\n')
        self.assertIsInstance(log.broken, OSError)

    def test_missing_original_stream_still_logs(self):
        log = run_log.RunLog()
        self.addCleanup(log.close)
        log.attach(self.directory)
        stream = run_log._Mirror(None, log)
        self.assertEqual(stream.write('无窗口\n'), 4)
        self.assertFalse(stream.isatty())
        self.assertIn('无窗口', (self.directory / run_log.LOG_NAME).read_text(encoding='utf-8'))

    def test_handled_exception_reaches_log_when_original_stderr_fails(self):
        log = run_log.RunLog()
        self.addCleanup(log.close)
        path = log.attach(self.directory)
        with redirect_stderr(run_log._Mirror(Broken(), log)):
            run_log.log_exception('[tools] phase=fixture', self.crashed())
        text = path.read_text(encoding='utf-8')
        self.assertIn('OSError: pipe closed', text)
        self.assertIn('ConnectionError: Codex app-server 已断开', text)

    def test_attach_without_install_does_nothing(self):
        self.assertIsNone(run_log._ACTIVE)    # Tests and imports never replace sys.stdout.
        self.assertIsNone(run_log.attach(self.directory))
        self.assertFalse((self.directory / run_log.LOG_NAME).exists())

    def test_uncaught_exception_traceback_reaches_the_run_log_in_a_real_process(self):
        import subprocess
        import sys
        code = ('import sys; from pathlib import Path; from orchestrator import run_log; run_log.install(); '
                'print("[run] 预检"); run_log.attach(Path(sys.argv[1])); print("[run] 开始"); '
                'raise ConnectionError("Codex app-server 已断开")')
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(self.directory)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[run] 开始', result.stdout)                # The original streams are unchanged.
        self.assertIn('ConnectionError: Codex app-server 已断开', result.stderr)
        text = (self.directory / run_log.LOG_NAME).read_text(encoding='utf-8')
        for part in ('[run] 预检', '[run] 开始', 'Traceback', 'ConnectionError: Codex app-server 已断开'):
            self.assertIn(part, text)
        self.assertTrue(all(STAMPED.match(line) for line in text.splitlines()))

    def crashed(self):
        try:
            try:
                raise OSError('pipe closed')
            except OSError as inner:
                raise ConnectionError('Codex app-server 已断开') from inner
        except ConnectionError as outer:
            outer.add_note('录制收尾清理失败：超时')
            return outer

    def test_crash_record_keeps_chain_notes_and_traceback(self):
        error = self.crashed()
        path = run_log.record_crash(self.directory, error)
        text = path.read_text(encoding='utf-8')
        for part in ('因异常退出', 'OSError: pipe closed', 'ConnectionError: Codex app-server 已断开',
                     '录制收尾清理失败：超时', 'Traceback'):
            self.assertIn(part, text)
        described = run_log.describe(error)
        self.assertEqual((described['type'], described['message']), ('ConnectionError', 'Codex app-server 已断开'))
        self.assertEqual(described['chain'], [{'type': 'OSError', 'message': 'pipe closed', 'relation': 'cause'}])
        self.assertEqual(described['notes'], ['录制收尾清理失败：超时'])
        self.assertTrue(run_log.crash_reason(error).startswith('主控异常：ConnectionError: '))
        self.assertLessEqual(len(run_log.crash_reason(RuntimeError('长' * 1000))), run_log.REASON_CHARS)

    def test_interrupts_and_deliberate_exits_are_not_crashes(self):
        import asyncio
        for error in (KeyboardInterrupt(), SystemExit('端口占用'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(run_log.is_crash(error))
                self.assertIsNone(run_log.record_crash(self.directory, error))
        self.assertFalse((self.directory / run_log.ERROR_NAME).exists())
        self.assertFalse(run_log.is_crash(None))
        self.assertIsNone(run_log.record_crash(None, RuntimeError('x')))


if __name__ == '__main__':
    unittest.main()
