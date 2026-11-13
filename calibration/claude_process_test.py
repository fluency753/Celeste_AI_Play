"""Claude subprocess ownership with real local children; no model, Docker or game."""
import asyncio
import builtins
import json
import os
from contextlib import ExitStack
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator import agent, config, kb_access, personal_prompts, prompting
from orchestrator.agent import ClaudeRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.session_store import rename_directory


CHILD = '''import json,os,sys,time
from pathlib import Path
Path(sys.argv[2]).write_text(str(os.getpid()),encoding='ascii')
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
 value=json.loads(line)
 if value.get('type')!='user': continue
 emit({'type':'assistant','message':{'id':'message','content':[
  {'type':'tool_use','id':'program','name':'Bash','input':{'command':'fixture'}}]}})
 if sys.argv[1]=='hang': time.sleep(60)
 emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'program'}]}})
 emit({'type':'result','subtype':'success','result':'Fixture','usage':{'output_tokens':1}})
'''


class CloseFailure:
    def __init__(self, file, error):
        self.file, self.error = file, error

    def __getattr__(self, name):
        return getattr(self.file, name)

    def close(self):
        self.file.close()
        raise self.error


class ClaudeProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, 'REPO_ROOT', self.root))
        self.stack.enter_context(patch.object(kb_access, 'KB_ROOT', self.root/'kb'))
        self.stack.enter_context(patch.object(personal_prompts, 'STORE', self.root/'personal.json'))
        self.stack.enter_context(patch.object(prompting, 'OVERRIDES', self.root/'prompts'))
        for key, value in {'PROCESS_GRACE_S': .5, 'PROCESS_TERMINATE_S': .1,
                           'PROCESS_KILL_S': .3, 'DOCKER_CLEANUP_S': 1.0,
                           'READER_CLOSE_S': .3, 'NETWORK_PROBE_S': .05}.items():
            self.stack.enter_context(patch.object(agent, key, value))
        self.cfg = RunConfig(agent='process-fixture', save='fixture', model='fixture', sim=False,
                             mode='explore', knowledge_profile='explore', runtime='container')
        self.metrics = Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'high', 60)
        self.session = SimpleNamespace(done=False, done_reason='')
        self.runner = ClaudeRunner(self.cfg, self.root, self.session, self.metrics, 'fixture-token',
                                   resume_note='Fixture process request', naming_only=True)
        self.work = self.root/'work'
        self.work.mkdir()
        self.script = self.root/'child.py'
        self.script.write_text(CHILD, encoding='utf-8')
        # Windows venv python.exe is a launcher that owns another Python process.
        # These fixtures model a directly owned executable, as the real Claude client is.
        self.python = sys._base_executable if os.name == 'nt' else sys.executable
        self.main_pid = self.root/'main-child.pid'
        self.pid_files = []
        self.mode = 'normal'
        self.children, self.readers, self.logs = [], [], []
        self.spawn_error = self.docker_error = self.body_error = self.close_error = None
        self.docker_mode = 'normal'
        self.docker_exit = 0
        self.docker_commands = []
        self.cleanup_started = asyncio.Event()
        self.spawned = asyncio.Event()
        self.disk_failure = False
        original_command, original_reader = self.runner._command, self.runner._read_stdout
        self.real_spawn = asyncio.create_subprocess_exec
        original_open = builtins.open

        def command():
            original_command()  # Keep guide and prompt publication; replace only the executable.
            self.main_pid = self.root/f'main-child-{len(self.children)}.pid'
            return [self.python, '-X', 'utf8', '-u', str(self.script), self.mode, str(self.main_pid)]

        async def reader():
            self.readers.append(asyncio.current_task())
            return await original_reader()

        async def spawn(*argv, **options):
            if argv[0] == 'docker-fixture':
                self.docker_commands.append(argv)
                self.disk_failure = True
                if self.docker_error is not None:
                    raise self.docker_error
                if self.docker_mode == 'remote-pkill':
                    shell = shutil.which('sh')
                    git = shutil.which('git')
                    if shell is None and git:
                        candidate = Path(git).resolve().parent.parent/'usr/bin/sh.exe'
                        shell = str(candidate) if candidate.is_file() else None
                    if shell is None:
                        self.skipTest('A local POSIX shell is required for the fixed remote wrapper check')
                    # Exercise the exact script passed at the Docker boundary.
                    # Shadow pkill so no local process receives the test signal.
                    script = f'pkill() {{ return {self.docker_exit}; }}; ' + argv[-1]
                    proc = await self.real_spawn(shell, '-c', script, **options)
                else:
                    marker = self.root/f'cleanup-child-{len(self.children)}.pid'
                    program = ('import os,sys;from pathlib import Path;'
                               'Path(sys.argv[1]).write_text(str(os.getpid()),encoding="ascii");'
                               + ('import time;time.sleep(60)' if self.docker_mode == 'hang'
                                  else f'raise SystemExit({self.docker_exit})'))
                    proc = await self.real_spawn(self.python, '-c', program, str(marker), **options)
                    self.pid_files.append((proc, marker))
            else:
                if self.spawn_error is not None:
                    raise self.spawn_error
                proc = await self.real_spawn(*argv, **options)
                self.pid_files.append((proc, self.main_pid))
                close = proc.stdin.close

                def close_input():
                    self.cleanup_started.set()
                    return close()

                proc.stdin.close = close_input
                if self.body_error is not None:
                    async def drain():
                        raise self.body_error
                    proc.stdin.drain = drain
                self.spawned.set()
            self.children.append(proc)
            return proc

        def open_file(path, *args, **options):
            handle = original_open(path, *args, **options)
            if Path(path) == self.root/'claude_stderr.log':
                self.logs.append(handle)
                if self.close_error is not None:
                    return CloseFailure(handle, self.close_error)
            return handle

        self.stack.enter_context(patch.object(self.runner, '_command', command))
        self.stack.enter_context(patch.object(self.runner, '_read_stdout', reader))
        self.stack.enter_context(patch.object(agent, 'find_docker_exe', return_value='docker-fixture'))
        self.stack.enter_context(patch.object(agent.asyncio, 'create_subprocess_exec', spawn))
        self.stack.enter_context(patch.object(agent, 'open', open_file, create=True))

    async def asyncTearDown(self):
        for proc in self.children:
            if proc.returncode is None:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 2)
        for task in self.readers:
            if not task.done():
                task.cancel()
        if self.readers:
            await asyncio.gather(*self.readers, return_exceptions=True)
        self.runner.close_events()
        self.stack.close()
        self.folder.cleanup()

    def assert_reaped(self):
        self.assertTrue(all(proc.returncode is not None for proc in self.children))
        self.assertTrue(all(task.done() for task in self.readers))
        self.assertTrue(all(handle.closed for handle in self.logs))
        self.assertFalse(self.metrics.tools_running)
        for proc, marker in self.pid_files:
            # A child killed before its first instruction has no marker; one killed between creating
            # the file and writing into it leaves it empty (seen on a slow machine).
            written = marker.read_text(encoding='ascii') if marker.exists() else ''
            if written:
                self.assertEqual(int(written), proc.pid)
        released = self.work.with_name('work-released')
        # Process identity/exit checks above are mandatory; this additionally
        # checks CWD release without turning a transient Windows file lock into a test failure.
        rename_directory(self.work, released)
        rename_directory(released, self.work)

    async def run_process(self):
        return await asyncio.wait_for(self.runner._run_process(self.work, 'Fixture'), 5)

    async def test_real_stdio_child_normal_completion_reaps_reader_and_log(self):
        self.assertTrue(await self.run_process())
        self.assertEqual(self.runner.name_result, 'Fixture')
        self.assertEqual(self.runner.proc.returncode, 0)
        self.assertEqual(len(self.readers), 1)
        self.assertEqual(len(self.logs), 1)
        self.assert_reaped()

    async def test_spawn_failure_closes_real_stderr_and_preserves_original_error(self):
        expected = self.spawn_error = OSError('external spawn failed')
        self.runner.proc = SimpleNamespace(returncode=0)  # A previous, already reaped client.
        with self.assertRaises(OSError) as caught:
            await self.run_process()
        self.assertIs(caught.exception, expected)
        self.assertIsNone(self.runner.proc)
        self.assertEqual(self.children, [])
        self.assertEqual(self.readers, [])
        self.assertEqual(len(self.logs), 1)
        self.assert_reaped()

    async def test_initial_metrics_failure_still_closes_log_without_spawning(self):
        expected = ValueError('initial metrics serialization failure')
        original = json.dumps

        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'written_wall' in value:
                raise expected
            return original(value, *args, **kwargs)

        with patch('orchestrator.metrics.json.dumps', dumps), self.assertRaises(ValueError) as caught:
            await self.run_process()
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.children, [])
        self.assertEqual(len(self.logs), 1)
        self.assert_reaped()

    async def test_body_cancel_reaps_real_child_and_preserves_task_cancellation(self):
        self.mode = 'hang'
        task = asyncio.create_task(self.runner._run_process(self.work, 'Fixture'))
        async with asyncio.timeout(3):
            while not self.metrics.tools_running:
                await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        self.assertTrue(self.main_pid.is_file())
        self.assert_reaped()

    async def test_cancel_during_shutdown_overrides_body_error_and_keeps_its_cause(self):
        self.mode = 'hang'
        expected = self.body_error = ValueError('body pipe failure')
        task = asyncio.create_task(self.runner._run_process(self.work, 'Fixture'))
        await asyncio.wait_for(self.cleanup_started.wait(), 3)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertTrue(task.cancelled())
        self.assertIs(caught.exception.__cause__, expected)
        self.assert_reaped()

    async def test_docker_spawn_failure_cannot_skip_reader_or_stderr_cleanup(self):
        self.runner.container = 'fixture'
        expected = self.docker_error = OSError('docker cleanup spawn failed')
        with self.assertRaises(OSError) as caught:
            await self.run_process()
        self.assertIs(caught.exception, expected)
        self.assert_reaped()

    async def test_docker_daemon_exit_one_is_not_treated_as_remote_no_match(self):
        self.runner.container = 'fixture'
        self.docker_exit = 1
        with self.assertRaisesRegex(RuntimeError, '清理未确认.*Docker 返回 1'):
            await self.run_process()
        self.assert_reaped()

    async def test_remote_wrapper_only_normalizes_pkill_no_match(self):
        self.runner.container = 'fixture'
        self.docker_mode = 'remote-pkill'
        self.docker_exit = 1
        self.assertTrue(await self.run_process())
        command = self.docker_commands[-1]
        self.assertEqual(command[:5], ('docker-fixture', 'exec', 'fixture', 'sh', '-c'))
        self.assertIn('pkill -KILL -u agent;', command[-1])
        self.assert_reaped()
        # Actual pkill errors (including executable-not-found=127) remain errors.
        for code in (2, 127):
            with self.subTest(remote_status=code):
                self.docker_exit = code
                with self.assertRaisesRegex(RuntimeError, f'清理未确认.*Docker 返回 {code}'):
                    await self.run_process()
                self.assert_reaped()

    async def test_body_error_wins_over_multiple_independent_cleanup_errors(self):
        self.runner.container = 'fixture'
        expected = self.body_error = ValueError('original body failure')
        self.docker_error = OSError('docker cleanup unavailable')
        self.close_error = OSError('stderr close failure')
        original = json.dumps

        def dumps(value, *args, **kwargs):
            if self.disk_failure and isinstance(value, dict) and 'written_wall' in value:
                raise ValueError('metrics cleanup serialization failure')
            return original(value, *args, **kwargs)

        with patch('orchestrator.metrics.json.dumps', dumps), self.assertRaises(ValueError) as caught:
            await self.run_process()
        self.assertIs(caught.exception, expected)
        notes = '\n'.join(caught.exception.__notes__)
        for failure in ('docker cleanup unavailable', 'metrics cleanup serialization failure', 'stderr close failure'):
            self.assertIn(failure, notes)
        self.assert_reaped()

    async def test_hanging_docker_cleanup_client_is_killed_and_reaped(self):
        self.runner.container = 'fixture'
        self.docker_mode = 'hang'
        with patch.object(agent, 'DOCKER_CLEANUP_S', .05), self.assertRaises(TimeoutError):
            await self.run_process()
        self.assertEqual(len(self.children), 2)
        self.assert_reaped()

    async def test_outer_run_heartbeat_failure_still_closes_event_log(self):
        expected = OSError('stop heartbeat unavailable')
        original = self.runner.control.heartbeat

        def heartbeat(**fields):
            if fields.get('stopped'):
                raise expected
            return original(**fields)

        with patch.object(self.runner.control, 'heartbeat', heartbeat), self.assertRaises(OSError) as caught:
            await asyncio.wait_for(self.runner.run(), 5)
        self.assertIs(caught.exception, expected)
        self.assertTrue(self.runner._events.closed)
        self.assertTrue(self.runner.control_task.done())
        self.assertTrue(self.runner.heartbeat_task.done())
        self.assert_reaped()

    async def test_outer_run_keeps_spawn_error_when_stop_heartbeat_also_fails(self):
        expected = self.spawn_error = OSError('original spawn failure')
        original = self.runner.control.heartbeat

        def heartbeat(**fields):
            if fields.get('stopped'):
                raise OSError('secondary heartbeat failure')
            return original(**fields)

        with patch.object(self.runner.control, 'heartbeat', heartbeat), self.assertRaises(OSError) as caught:
            await asyncio.wait_for(self.runner.run(), 5)
        self.assertIs(caught.exception, expected)
        self.assertIn('secondary heartbeat failure', '\n'.join(expected.__notes__))
        self.assertTrue(self.runner._events.closed)
        self.assert_reaped()

    async def test_outer_run_cancel_during_control_cleanup_replaces_prior_body_error(self):
        expected = OSError('spawn failed before outer cleanup')
        entered = asyncio.Event()

        async def control_channel():
            try:
                await asyncio.Event().wait()
            finally:
                entered.set()
                await asyncio.Event().wait()

        async def failed_spawn(*args, **kwargs):
            await asyncio.sleep(0)  # Let the real run() start its owned control task.
            raise expected

        with patch.object(self.runner, '_controls', control_channel), \
             patch.object(agent.asyncio, 'create_subprocess_exec', failed_spawn):
            task = asyncio.create_task(self.runner.run())
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertTrue(task.cancelled())
        self.assertIs(caught.exception.__cause__, expected)
        self.assertTrue(self.runner.control_task.done())
        self.assertTrue(self.runner._events.closed)
        self.assert_reaped()

    async def test_probe_timeout_reaps_local_docker_client_before_host_fallback(self):
        self.runner.container = 'fixture'
        self.docker_mode = 'hang'
        with patch('urllib.request.urlopen', return_value=object()) as fallback:
            self.assertTrue(await self.runner._probe_network())
        fallback.assert_called_once()
        self.assert_reaped()

    async def test_probe_cancel_reaps_local_docker_client_without_host_fallback(self):
        self.runner.container = 'fixture'
        self.docker_mode = 'hang'
        with patch('urllib.request.urlopen', return_value=object()) as fallback:
            task = asyncio.create_task(self.runner._probe_network())
            async with asyncio.timeout(3):
                while not self.children:
                    await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task.cancelled())
        fallback.assert_not_called()
        self.assert_reaped()


class ProcessWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminate_and_kill_are_followed_by_exit_confirmation(self):
        for raced in (False, True):
            trace = []

            class Child:
                returncode = None
                stdin = None

                async def wait(self):
                    trace.append('wait')
                    if self.returncode is not None:
                        return self.returncode
                    await asyncio.Event().wait()

                def terminate(self):
                    trace.append('terminate')
                    if raced:
                        raise ProcessLookupError('exit raced with signal')

                def kill(self):
                    trace.append('kill')
                    self.returncode = -9

            child = Child()
            owner = SimpleNamespace(proc=child)
            with self.subTest(raced=raced), patch.object(agent, 'PROCESS_GRACE_S', .01), \
                 patch.object(agent, 'PROCESS_TERMINATE_S', .01):
                await ClaudeRunner._shutdown_process(owner)
            self.assertEqual(trace, ['wait', 'terminate', 'wait', 'kill', 'wait'])
            self.assertEqual(child.returncode, -9)

    async def test_child_without_confirmed_exit_is_reported_explicitly(self):
        class Child:
            returncode = None
            stdin = None

            async def wait(self):
                await asyncio.Event().wait()

            def terminate(self):
                pass

            def kill(self):
                pass

        owner = SimpleNamespace(proc=Child())
        with patch.object(agent, 'PROCESS_GRACE_S', .01), patch.object(agent, 'PROCESS_TERMINATE_S', .01), \
             patch.object(agent, 'PROCESS_KILL_S', .01), self.assertRaisesRegex(RuntimeError, '未确认退出'):
            await ClaudeRunner._shutdown_process(owner)
        self.assertIsNone(owner.proc.returncode)


if __name__ == '__main__':
    unittest.main()
