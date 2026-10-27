"""Resident reload transitions keep the owner connection, files and recording.

Only the external resident RPC boundary is replaced; the worker, persistence,
file installation and SessionControls consumer remain production code.
"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest

from orchestrator.bridge_reload import reload_bridge, BridgeReloadError
from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.metrics import Metrics
from orchestrator.session_controls import SessionControls


class Resident:
    def __init__(self, target):
        self.target = str(target)
        self.calls = []
        self.stage = 'idle'
        self.lease = 'lease-123'
        self.version = '1.3.0'
        self.fail = False
        self.busy = 0
        self.done = False
        self.cfg = SimpleNamespace()
        self.resets = 0
        self.after_change = None
        self.obs = {'scene': 'Level', 'frame': 18, 'save': {'slot': 10, 'name': 'test'},
                    'level': {'sid': 'Celeste/3-CelestialResort', 'side': 'A', 'room': '01-b', 'chapterTimeTicks': 12345}}

    def reset_observation_delivery(self):
        self.resets += 1

    def bridge_call(self, command, **args):
        self.calls.append((command, args))
        if command == 'hello':
            return {'bridge_reload': 1, 'bridge': self.version, 'frame': 18, 'frozen': True,
                    'entry_guard': True, 'chapter_reset_guard': True, 'action_policy': 1,
                    'record_audio_active': True, 'record_audio_frames': 15, 'record_audio_replay_frames': 3,
                    'clock': {'plugin': {'state': 'recording', 'recording': True, 'dir': 'same-recording'}}}
        if command == 'observe':
            obs = copy.deepcopy(self.obs)
            if self.after_change and self.stage in {'ready', 'finished'}:
                obs['level'][self.after_change[0]] = self.after_change[1]
            return {'obs': obs}
        if command == 'reload_prepare':
            if self.busy:
                self.busy -= 1
                return {'stage': 'busy'}
            self.stage = 'prepared'
        elif command == 'reload_commit':
            if args['lease'] != self.lease:
                raise RuntimeError('wrong lease')
            self.stage = 'failed' if self.fail else 'ready'
        elif command == 'reload_finish':
            if args['lease'] != self.lease:
                raise RuntimeError('wrong lease')
            self.stage = 'finished'
        elif command != 'reload_status':
            raise AssertionError('Unexpected mutation: ' + command)
        return {'stage': self.stage, 'lease': self.lease if self.stage != 'idle' else None,
                'payload_path': self.target, 'payload_version': self.version, 'error': 'bad build' if self.fail else None}


class ReloadWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / 'installed' / 'CelesteAIBridge.dll'
        self.target.parent.mkdir()
        self.target.write_bytes(b'previous payload')
        self.source = self.root / 'build' / 'CelesteAIBridge.dll'
        self.source.parent.mkdir()
        self.source.write_bytes(b'new payload')
        self.session = Resident(self.target)
        self.options = {'dll': str(self.source), 'expected_version': '1.3.0'}

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_backup_replace_commit_and_finish_preserve_recording(self):
        self.session.busy = 1
        result = reload_bridge(self.session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(self.target.read_bytes(), b'new payload')
        self.assertEqual((self.root/'bridge_reload_backups/lease-123/CelesteAIBridge.dll').read_bytes(), b'previous payload')
        self.assertEqual([c for c, _ in self.session.calls].count('reload_prepare'), 2)
        self.assertEqual([c for c, _ in self.session.calls].count('reload_finish'), 1)
        self.assertEqual(self.session.resets, 1)
        self.assertFalse(any(c in {'record', 'step', 'advance', 'deactivate'} for c, _ in self.session.calls))

    def test_unsupported_runtime_rejects_before_file_write(self):
        original = self.session.bridge_call
        self.session.bridge_call = lambda cmd, **args: {'bridge_reload': False} if cmd == 'hello' else original(cmd, **args)
        with self.assertRaisesRegex(BridgeReloadError, '首次安装'):
            reload_bridge(self.session, self.root, self.options)
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        self.assertFalse((self.root/'bridge_reload_backups').exists())

    def test_lost_prepare_ack_stays_held_and_retry_queries_without_reprepare(self):
        original = self.session.bridge_call
        lost = False

        def call(cmd, **args):
            nonlocal lost
            result = original(cmd, **args)
            if cmd == 'reload_prepare' and not lost:
                lost = True
                raise TimeoutError('prepare reply lost after hold')
            return result

        self.session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '结果尚未确认') as raised:
            reload_bridge(self.session, self.root, self.options)
        self.assertTrue(raised.exception.reload_held)
        state = json.loads((self.root/'bridge_reload.json').read_text())
        self.assertTrue(state['prepare_outcome_unknown'])
        self.assertEqual(state['resume_phase'], 'preparing')
        self.assertIsNone(state.get('lease'))
        self.assertEqual(self.session.stage, 'prepared')
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        with self.assertRaisesRegex(BridgeReloadError, '--retry'):
            reload_bridge(self.session, self.root, self.options)
        self.session.calls.clear()
        result = reload_bridge(self.session, self.root, {**self.options, 'retry': True})
        self.assertEqual(result['phase'], 'finished')
        self.assertFalse(any(c == 'reload_prepare' for c, _ in self.session.calls))

    def test_lost_commit_ack_waits_original_attempt_without_writing_or_recommitting(self):
        original = self.session.bridge_call
        polling = 0
        lost = False

        def call(cmd, **args):
            nonlocal polling, lost
            if cmd == 'reload_status' and self.session.stage == 'reloading':
                polling += 1
                if polling >= 3:
                    self.session.stage = 'ready'
            result = original(cmd, **args)
            if cmd == 'reload_commit' and not lost:
                lost = True
                self.session.stage = 'reloading'
                raise TimeoutError('commit reply lost while Everest reads the DLL')
            return result

        self.session.bridge_call = call
        with self.assertRaises(TimeoutError):
            reload_bridge(self.session, self.root, self.options)
        self.assertEqual(self.target.read_bytes(), b'new payload')
        self.source.write_bytes(b'different bytes must not replace an in-flight attempt')
        self.session.calls.clear()
        result = reload_bridge(self.session, self.root, {**self.options, 'retry': True})
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(self.target.read_bytes(), b'new payload')
        self.assertGreaterEqual(polling, 3)
        self.assertFalse(any(c in {'reload_prepare', 'reload_commit'} for c, _ in self.session.calls))

    def test_failure_keeps_lease_then_retry_uses_same_lease_without_reprepare(self):
        self.session.fail = True
        with self.assertRaisesRegex(BridgeReloadError, 'bad build'):
            reload_bridge(self.session, self.root, self.options)
        state = json.loads((self.root/'bridge_reload.json').read_text())
        self.assertEqual(state['lease'], 'lease-123')
        self.assertEqual(state['phase'], 'failed')
        self.assertFalse(any(c == 'reload_finish' for c, _ in self.session.calls))
        self.session.fail = False
        self.session.calls.clear()
        result = reload_bridge(self.session, self.root, {**self.options, 'retry': True})
        self.assertEqual(result['phase'], 'finished')
        self.assertFalse(any(c == 'reload_prepare' for c, _ in self.session.calls))
        self.assertEqual((self.root/'bridge_reload_backups/lease-123/CelesteAIBridge.dll').read_bytes(), b'previous payload')

    def test_wrong_map_is_not_finished_and_retry_never_replays_act(self):
        self.session.after_change = ('sid', 'Celeste/4-GoldenRidge')
        with self.assertRaisesRegex(BridgeReloadError, '核验失败'):
            reload_bridge(self.session, self.root, self.options)
        self.assertFalse(any(c == 'reload_finish' for c, _ in self.session.calls))
        self.assertEqual(self.session.stage, 'ready')
        self.session.after_change = None
        reload_bridge(self.session, self.root, {'retry': True})
        self.assertFalse(any(c in {'step', 'advance', 'record'} for c, _ in self.session.calls))

    def test_player_pose_change_is_allowed_for_normal_everest_room_reload(self):
        # Room identity/time remain required; normal Everest player reconstruction is expected.
        self.session.obs['player'] = {'state': 'Climb', 'pos': [1, 2]}
        result = reload_bridge(self.session, self.root, {})
        self.assertEqual(result['phase'], 'finished')

    def test_retry_finished_record_does_not_corrupt_it(self):
        reload_bridge(self.session, self.root, {})
        self.session.calls.clear()
        with self.assertRaisesRegex(BridgeReloadError, '已经完成'):
            reload_bridge(self.session, self.root, {'retry': True})
        self.assertEqual(json.loads((self.root/'bridge_reload.json').read_text())['phase'], 'finished')
        self.assertEqual(self.session.calls, [])

    def test_retry_keeps_expected_version_until_explicitly_overridden(self):
        with self.assertRaisesRegex(BridgeReloadError, '版本与要求不符'):
            reload_bridge(self.session, self.root, {'expected_version': '9.9.9'})
        state = json.loads((self.root/'bridge_reload.json').read_text())
        self.assertEqual(state['expected_version'], '9.9.9')
        self.session.calls.clear()
        with self.assertRaisesRegex(BridgeReloadError, '版本与要求不符'):
            reload_bridge(self.session, self.root, {'retry': True})
        self.assertFalse(any(c == 'reload_finish' for c, _ in self.session.calls))
        result = reload_bridge(self.session, self.root, {'retry': True, 'expected_version': '1.3.0'})
        self.assertEqual(result['expected_version'], '1.3.0')
        self.assertEqual(result['phase'], 'finished')

    def test_refresh_failure_retries_publication_without_recommit_or_file_replacement(self):
        count = []

        def refresh(obs):
            count.append(obs)
            if len(count) == 1:
                raise OSError('publication failed')

        self.session.after_bridge_reload = refresh
        with self.assertRaisesRegex(BridgeReloadError, '刷新失败'):
            reload_bridge(self.session, self.root, self.options)
        self.assertEqual(self.session.stage, 'finished')
        record = json.loads((self.root/'bridge_reload.json').read_text())
        self.assertTrue(record['resident_finished'])
        self.assertFalse(record['connection_failed'])
        self.session.calls.clear()
        self.source.write_bytes(b'not part of the completed resident reload')
        result = reload_bridge(self.session, self.root, {**self.options, 'retry': True})
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(len(count), 2)
        self.assertEqual(self.target.read_bytes(), b'new payload')
        self.assertFalse(any(c in {'reload_prepare', 'reload_commit', 'reload_finish'} for c, _ in self.session.calls))


class Controls(SessionControls):
    def __init__(self, root, session):
        self.run_dir = root
        self.cfg = RunConfig(agent='test', save='test', model='test')
        self.session = session
        self.chat_only = False
        self.control = RunControl(root)
        self.metrics = Metrics(root/'metrics.json', 'test', 'test', 'high', 60)
        self.paused = False
        self.pausing = False
        self.pause_at = None
        self.paused_seconds = 0
        self.action_lock = asyncio.Lock()
        self.pending_messages = []


class ReloadControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_waits_for_current_action_and_preserves_pause_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            session = Resident(target)
            owner = Controls(root, session)
            owner.paused = True
            ident = owner.control.enqueue('bridge_reload', '{}')
            row = owner.control.pending()[0]
            async with owner.action_lock:
                task = asyncio.create_task(owner._reload_bridge(row))
                await asyncio.sleep(.03)
                self.assertEqual(session.calls, [])
            await task
            self.assertTrue(owner.paused)
            self.assertEqual(owner.control.snapshot()['actions'][0]['id'], ident)
            self.assertEqual(owner.control.snapshot()['actions'][0]['status'], 'submitted')

    async def test_failed_reload_keeps_paused_and_failed_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            session = Resident(target)
            session.fail = True
            owner = Controls(root, session)
            owner.control.enqueue('bridge_reload', '{}')
            with self.assertRaises(BridgeReloadError):
                await owner._reload_bridge(owner.control.pending()[0])
            self.assertTrue(owner.paused)
            self.assertTrue(owner._bridge_reload_failed)
            self.assertTrue(owner.cfg._keep_frozen)
            self.assertFalse(owner.pausing)

    async def test_refresh_failure_blocks_resume_then_retry_only_refreshes_and_restores_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            session = Resident(target)
            count = []

            def refresh(obs):
                count.append(obs)
                if len(count) == 1:
                    raise OSError('strict workspace publication failed')

            session.after_bridge_reload = refresh
            owner = Controls(root, session)
            owner.control.enqueue('bridge_reload', '{}')
            with self.assertRaisesRegex(BridgeReloadError, '刷新失败'):
                await owner._reload_bridge(owner.control.pending()[0])
            self.assertEqual(session.stage, 'finished')
            self.assertTrue(owner.paused)
            self.assertTrue(owner._bridge_reload_failed)
            self.assertFalse(json.loads((root/'bridge_reload.json').read_text())['owner_was_paused'])
            session.calls.clear()
            owner.control.enqueue('bridge_reload', '{"retry":true}')
            await owner._reload_bridge(owner.control.pending()[0])
            self.assertEqual(len(count), 2)
            self.assertFalse(owner.paused)
            self.assertFalse(owner._bridge_reload_failed)
            self.assertIsNone(owner.pause_at)
            self.assertFalse(any(cmd in {'reload_prepare', 'reload_commit', 'reload_finish', 'step', 'deactivate'}
                                 for cmd, _ in session.calls))

    async def test_prepare_ack_loss_blocks_normal_resume_until_status_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            session = Resident(target)
            original = session.bridge_call

            def call(cmd, **args):
                response = original(cmd, **args)
                if cmd == 'reload_prepare':
                    raise TimeoutError('prepare ack lost')
                return response

            session.bridge_call = call
            owner = Controls(root, session)
            owner.control.enqueue('bridge_reload', '{}')
            with self.assertRaisesRegex(BridgeReloadError, '结果尚未确认'):
                await owner._reload_bridge(owner.control.pending()[0])
            self.assertEqual(session.stage, 'prepared')
            self.assertTrue(owner.paused)
            self.assertTrue(owner._bridge_reload_failed)
            self.assertTrue(owner._bridge_reload_status()['bridge_reload_failed'])
            self.assertFalse(any(cmd in {'reload_commit', 'reload_finish', 'step', 'deactivate'} for cmd, _ in session.calls))

    async def test_control_cancellation_holds_action_lock_until_worker_quiesces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            entered, release = threading.Event(), threading.Event()
            session = Resident(target)
            original = session.bridge_call

            def call(cmd, **args):
                if cmd == 'reload_commit':
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('fixture did not release commit')
                return original(cmd, **args)

            session.bridge_call = call
            owner = Controls(root, session)
            owner.control.enqueue('bridge_reload', '{}')
            task = asyncio.create_task(owner._reload_bridge(owner.control.pending()[0]))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(.03)
            self.assertTrue(owner.action_lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(owner.action_lock.locked())
            self.assertTrue(owner.paused)
            self.assertTrue(owner._bridge_reload_failed)
            self.assertFalse(any(cmd == 'reload_finish' for cmd, _ in session.calls))
            self.assertEqual(owner.control.snapshot()['actions'][0]['status'], 'error')


class ReloadCliTests(unittest.TestCase):
    def invoke(self, failed=False, lost_prepare=False, refresh_fail=False):
        from overlay.server import HudServer, make_handler
        from orchestrator.agent import ClaudeRunner
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            session = Resident(target)
            session.fail = failed
            if refresh_fail:
                def refresh(obs):
                    raise OSError('strict workspace publication failed')

                session.after_bridge_reload = refresh
            if lost_prepare:
                original = session.bridge_call

                def call(cmd, **args):
                    response = original(cmd, **args)
                    if cmd == 'reload_prepare':
                        raise TimeoutError('prepare acknowledgement was lost')
                    return response

                session.bridge_call = call
            session.workspace = None
            session.bridge_reload_supported = True
            cfg = RunConfig(agent='test', save='test', model='test', runtime='host')
            session.cfg = cfg
            (root/'run_state.json').write_text(json.dumps({'agent': 'test', 'save': 'test', 'engine': 'claude', 'model': 'test'}))
            metrics = Metrics(root/'metrics.json', 'test', 'test', 'high', 60)
            session.metrics = metrics
            ready, stop = threading.Event(), threading.Event()
            failures = []

            def consume():
                async def run():
                    owner = ClaudeRunner(cfg, root, session, metrics, '')
                    task = asyncio.create_task(owner._controls())
                    try:
                        owner.control.heartbeat(bridge_reload_control=True)
                        ready.set()
                        while not stop.is_set():
                            await asyncio.sleep(.01)
                        if task.done():
                            task.result()
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        owner.close_events()
                try:
                    asyncio.run(run())
                except Exception as exc:
                    failures.append(exc)
                    ready.set()

            thread = threading.Thread(target=consume, daemon=True)
            server = HudServer(('127.0.0.1', 0), make_handler(root))
            http = threading.Thread(target=server.serve_forever, daemon=True)
            http.start(); thread.start()
            self.assertTrue(ready.wait(3))
            try:
                result = subprocess.run([sys.executable, '-X', 'utf8', 'tools/reload_bridge.py',
                                         '--hud-port', str(server.server_port), '--timeout', '5'],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                        encoding='utf-8', timeout=12)
                state = json.loads((root/'bridge_reload.json').read_text())
                if lost_prepare or refresh_fail:
                    from tools.reload_bridge import request
                    base = f'http://127.0.0.1:{server.server_port}'
                    ident = request(base, {'run': str(root), 'kind': 'resume'})['id']
                    limit = time.monotonic() + 3
                    while time.monotonic() < limit:
                        action = next(row for row in request(base)['control']['actions'] if row['id'] == ident)
                        if action['status'] == 'error':
                            break
                        time.sleep(.02)
                    self.assertEqual(action['status'], 'error')
                    self.assertIn('重载尚未接回', action['detail'])
                    self.assertEqual(session.stage, 'prepared' if lost_prepare else 'finished')
                calls = list(session.calls)
            finally:
                stop.set(); server.shutdown(); server.server_close()
                thread.join(3); http.join(3)
            if failures:
                raise failures[0]
            return result, state, calls

    def test_real_cli_http_sqlite_control_and_worker_success(self):
        result, state, calls = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('原录像继续', result.stdout)
        self.assertEqual(state['phase'], 'finished')
        self.assertFalse(any(cmd in {'record', 'step', 'deactivate'} for cmd, _ in calls))

    def test_real_cli_receives_failure_without_stopping_recording(self):
        result, state, calls = self.invoke(True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('bad build', result.stderr)
        self.assertIn('试玩仍暂停', result.stderr)
        self.assertEqual(state['lease'], 'lease-123')
        self.assertFalse(any(cmd in {'reload_finish', 'record', 'step', 'deactivate'} for cmd, _ in calls))

    def test_real_cli_prepare_ack_loss_and_ui_resume_are_safe(self):
        result, state, calls = self.invoke(lost_prepare=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('结果尚未确认', result.stderr)
        self.assertIn('--retry', result.stderr)
        self.assertTrue(state['prepare_outcome_unknown'])
        self.assertFalse(any(cmd in {'reload_commit', 'reload_finish', 'step', 'deactivate'} for cmd, _ in calls))

    def test_real_cli_refresh_failure_after_resident_finish_still_blocks_ui_resume(self):
        result, state, calls = self.invoke(refresh_fail=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('刷新失败', result.stderr)
        self.assertTrue(state['resident_finished'])
        self.assertEqual([cmd for cmd, _ in calls].count('reload_finish'), 1)
        self.assertFalse(any(cmd in {'step', 'deactivate'} for cmd, _ in calls))
