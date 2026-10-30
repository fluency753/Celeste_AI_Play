"""Resident reload transitions keep the owner connection, files and recording.

Only the external resident RPC boundary is replaced; the worker, persistence,
file installation and SessionControls consumer remain production code.
"""
import asyncio
import copy
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest

from orchestrator.bridge_reload import reload_bridge, BridgeReloadError, hold_message, options
from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.metrics import Metrics
from orchestrator.session_controls import SessionControls
from tools.bridge_client import BridgeClient


class Resident:
    def __init__(self, target, runtime=None):
        self.target = str(target)
        self.runtime = str(runtime) if runtime else None
        self.calls = []
        self.stage = 'idle'
        self.lease = 'lease-123'
        self.version = '1.3.1'
        self.fail = False
        self.busy = 0
        self.done = False
        self.cfg = SimpleNamespace()
        self.client = SimpleNamespace(timeout=600, reconnect=lambda: None)
        self.resets = 0
        self.after_change = None
        self.replays = 3
        self.bindings_ok = True
        self.report_runtime = True
        self.lock = threading.RLock()
        self.obs = {'scene': 'Level', 'frame': 18, 'save': {'slot': 10, 'name': 'test'},
                    'level': {'sid': 'Celeste/3-CelestialResort', 'side': 'A', 'room': '01-b', 'chapterTimeTicks': 12345}}

    def reset_observation_delivery(self):
        self.resets += 1

    def holding(self):
        return self.stage in {'prepared', 'reloading', 'ready', 'failed'}

    def bridge_call(self, command, **args):
        self.calls.append((command, args))
        if command == 'hello':
            hello = {'bridge_reload': 1, 'bridge': self.version, 'frame': self.obs.get('frame', 18), 'frozen': True, 'active': True,
                     'reload_scene_modes': True, 'chapter_loaded_stop': True, 'reload_ready_stop': True,
                     'entry_guard': True, 'chapter_reset_guard': True, 'action_policy': 1, 'pico8_entry_guard': True,
                     'input_reinit_guard': True, 'input_bindings_ok': self.bindings_ok,
                     'input_binding_mismatches': [] if self.bindings_ok else ['Grab', 'mod:SpeedrunTool.LoadState'],
                     'record_audio_active': True, 'record_audio_frames': 15, 'record_audio_replay_frames': self.replays,
                     'clock': {'plugin': {'state': 'recording', 'recording': True, 'dir': 'same-recording'}}}
            if self.runtime and self.report_runtime:
                hello['runtime_path'] = self.runtime
            return hello
        if command == 'playback':
            # Real core: a held reload rejects every game operation, including replay frames.
            if self.holding():
                raise RuntimeError('playback failed: Bridge maintenance hold; no game input was executed')
            self.replays += 3
            return {'frames': 3}
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
        elif command == 'reload_abort':
            if args['lease'] != self.lease:
                raise RuntimeError('wrong lease')
            if self.stage != 'prepared':
                raise RuntimeError('reload_abort failed: Reload hold cannot be released safely')
            self.stage = 'aborted'
        elif command != 'reload_status':
            raise AssertionError('Unexpected mutation: ' + command)
        reply = {'stage': self.stage, 'lease': self.lease if self.stage != 'idle' else None,
                 'payload_path': self.target, 'payload_version': self.version, 'error': 'bad build' if self.fail else None,
                 'holding': self.holding(), 'abortable': self.stage == 'prepared', 'restart_required': False,
                 'frame': self.obs.get('frame', 18), 'record_audio_active': True, 'record_audio_frames': 15, 'record_audio_replay_frames': self.replays}
        if self.runtime:
            reply['runtime_path'] = self.runtime
        return reply


class BoundaryResident(Resident):
    """External game boundary: menus advance unless explicitly held by a payload/core."""

    def __init__(self, target, runtime=None):
        super().__init__(target, runtime)
        self.boundary = False
        self.busy_boundary = 0
        self.prompt = False
        self.origin_hold = True
        self.obs = {'scene': 'Overworld', 'frame': 18, 'save': None,
                    'menu': {'current': 'OuiMainMenu', 'selection': 2}}

    def descriptor(self):
        result = {'mode': ('wait' if self.obs['scene'] == 'OverworldLoader' and not self.prompt else
                         'replace' if self.obs['scene'] == 'Level' else 'preserve'),
                'scene': 'Celeste.' + self.obs['scene'], 'save': copy.deepcopy(self.obs.get('save')),
                'session': copy.deepcopy(self.obs.get('completion') or self.obs.get('level'))}
        if self.prompt:
            result['requires_origin_hold'] = True
        return result

    def bridge_call(self, command, **args):
        if not self.boundary and not self.holding() and self.obs['scene'] == 'Overworld':
            self.obs['frame'] += 1
        if command == 'reload_boundary':
            self.calls.append((command, args))
            if args['enabled'] and self.busy_boundary:
                self.busy_boundary -= 1
                return {'acquired': False}
            self.boundary = args['enabled']
            return {'acquired': self.boundary}
        result = super().bridge_call(command, **args)
        if command == 'reload_prepare' and self.stage == 'prepared':
            self.boundary = False  # Export transferred the hold to the resident.
        if command == 'hello':
            result.update(reload_boundary_control=True, reload_boundary=self.descriptor(),
                          reload_origin_scene_hold=self.origin_hold,
                          frozen=self.boundary or self.holding() or self.obs['scene'] != 'Overworld')
        elif command == 'observe':
            result['obs']['reload_boundary'] = self.descriptor()
        return result


class PlaybackGapLock:
    """session.lock stand-in. When the lock becomes completely free, a queued replay segment runs
    before the next owner continues: the exact window the real playback worker (holding only
    session.lock in Playback._call) can use while the agent's search tools run."""

    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0
        self.pending = None

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self.lock.release()
        if self.depth == 0 and self.pending:
            job, self.pending = self.pending, None
            job()
        return False


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
        # The core the new bridge was built against, and the identical core the game runs.
        (self.root / 'build' / 'CelesteAIRuntime.dll').write_bytes(b'resident core 1.0.2')
        self.installed_runtime = self.root / 'Mods' / 'CelesteAIRuntime' / 'bin' / 'CelesteAIRuntime.dll'
        self.installed_runtime.parent.mkdir(parents=True)
        self.installed_runtime.write_bytes(b'resident core 1.0.2')
        self.session = Resident(self.target, self.installed_runtime)
        self.options = {'dll': str(self.source), 'expected_version': '1.3.1'}

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

    def test_free_roam_cannot_reload_to_bridge_without_map_set_guard(self):
        self.session.cfg.play_scope = 'visited'
        with self.assertRaisesRegex(BridgeReloadError, '地图集合门禁'):
            reload_bridge(self.session, self.root, self.options)
        self.assertTrue(self.session.holding())
        self.assertNotIn('reload_finish', [cmd for cmd, _ in self.session.calls])
        original = self.session.bridge_call
        def call(command, **args):
            result = original(command, **args)
            if command == 'hello':
                result['map_entry_set'] = True
            return result
        self.session.bridge_call = call
        self.assertEqual(reload_bridge(self.session, self.root, {'retry': True})['phase'], 'finished')
        self.assertTrue(self.session.map_entry_set_supported)
        self.assertEqual([cmd for cmd, _ in self.session.calls].count('reload_commit'), 1)

    def test_detour_cannot_reload_to_bridge_without_map_set_guard(self):
        # 计划外地图的门禁依赖地图集合：接回的桥接没有它就保持暂停，不退回只认计划章节以外的旧门禁
        self.session.cfg.allow_detour = True
        with self.assertRaisesRegex(BridgeReloadError, '计划外地图'):
            reload_bridge(self.session, self.root, self.options)
        self.assertTrue(self.session.holding())
        self.assertNotIn('reload_finish', [cmd for cmd, _ in self.session.calls])

    def test_loading_waits_at_first_loaded_boundary_before_prepare_and_file_replace(self):
        original = self.session.bridge_call
        loaded = copy.deepcopy(self.session.obs)
        self.session.obs = {'scene': 'LevelLoader', 'frame': 17, 'save': loaded['save']}
        def call(command, **args):
            if command == 'step':
                self.session.calls.append((command, args))
                self.assertEqual(self.target.read_bytes(), b'previous payload')
                self.assertEqual(args['inputs'], ['1'])
                self.assertEqual(args['stop_on'], ['reload_ready'])
                self.session.obs = loaded
                return {'obs': loaded, 'result': {'stop': 'reload_ready', 'consumed': 0, 'loadingFrames': 1}}
            value = original(command, **args)
            if command == 'hello': value['frozen'] = self.session.obs['scene'] == 'Level'
            return value
        self.session.bridge_call = call
        progress = []
        result = reload_bridge(self.session, self.root, self.options, progress=progress.append)
        commands = [command for command, _ in self.session.calls]
        self.assertLess(commands.index('step'), commands.index('reload_prepare'))
        self.assertEqual(result['loading_wait']['consumed'], 0)
        self.assertEqual(result['loading_wait']['loading_frames'], 1)
        self.assertEqual(result['before']['scene'], 'Level')
        self.assertIn('加载', progress[0])

    def test_loading_completion_between_hello_and_observe_refreshes_frozen_boundary(self):
        original = self.session.bridge_call
        first = True
        def call(command, **args):
            nonlocal first
            if command == 'step':
                self.session.calls.append((command, args))
                return {'obs': self.session.obs, 'result': {'stop': 'reload_ready', 'consumed': 0, 'loadingFrames': 0}}
            reply = original(command, **args)
            if command == 'hello' and first:
                first = False
                reply.update(scene='LevelLoader', frozen=False)
            return reply
        self.session.bridge_call = call
        result = reload_bridge(self.session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(result['loading_wait']['source_scene'], 'LevelLoader')
        self.assertEqual(result['loading_wait']['consumed'], 0)

    def test_loading_timeout_cancels_only_wait_and_retry_preserves_original_request(self):
        original = self.session.bridge_call
        loaded = copy.deepcopy(self.session.obs)
        self.session.obs = {'scene': 'LevelLoader', 'frame': 17, 'save': loaded['save']}
        timeout = True
        def call(command, **args):
            if command in {'step', 'cancel'}:
                self.session.calls.append((command, args))
                if command == 'cancel': return {'ok': True}
                if timeout: raise TimeoutError('loading did not complete')
                self.session.obs = loaded
                return {'obs': loaded, 'result': {'stop': 'reload_ready', 'consumed': 0, 'loadingFrames': 1}}
            value = original(command, **args)
            if command == 'hello': value['frozen'] = self.session.obs['scene'] == 'Level'
            return value
        self.session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '已取消等待'):
            reload_bridge(self.session, self.root, {**self.options, 'timeout_s': 1}, owner_was_paused=False)
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        self.assertNotIn('reload_prepare', [command for command, _ in self.session.calls])
        self.assertEqual([command for command, _ in self.session.calls].count('cancel'), 1)
        timeout = False
        result = reload_bridge(self.session, self.root, {'retry': True}, owner_was_paused=True)
        self.assertEqual(result['phase'], 'finished')
        self.assertIs(result['owner_was_paused'], False)
        self.assertEqual(result['initial_timeout_s'], 1)
        self.assertEqual(self.target.read_bytes(), b'new payload')
        # An explicit new limit replaces the old one; later sparse retries
        # inherit it rather than receiving producer-inserted defaults.
        self.session.obs = {'scene': 'LevelLoader', 'frame': 17, 'save': loaded['save']}
        timeout = True
        with self.assertRaises(BridgeReloadError):
            reload_bridge(self.session, self.root, {**self.options, 'timeout_s': 1})
        with self.assertRaises(BridgeReloadError):
            reload_bridge(self.session, self.root, {'retry': True, 'timeout_s': 2})
        timeout = False
        self.assertEqual(reload_bridge(self.session, self.root, {'retry': True})['initial_timeout_s'], 2)

    def test_control_queue_preserves_explicit_fields_before_worker_defaults(self):
        control = RunControl(self.root)
        control.enqueue('bridge_reload', '{"retry":true}')
        control.enqueue('bridge_reload', '{"retry":true,"timeout_s":2}')
        values = [json.loads(row['text']) for row in control.pending()]
        self.assertEqual(values, [{'retry': True}, {'retry': True, 'timeout_s': 2}])
        self.assertEqual(options({}, fill_defaults=False), {})
        self.assertEqual(options({})['timeout_s'], 120)

    def test_real_socket_loading_timeout_reconnects_only_owner_cancels_and_retries(self):
        resident = self.session
        loaded = copy.deepcopy(resident.obs)
        resident.obs = {'scene': 'LevelLoader', 'frame': 17, 'save': loaded['save']}
        state = {'timeout': True, 'pending': False, 'cancels': 0, 'steps': 0}
        class Handler(socketserver.StreamRequestHandler):
            def handle(handler):
                for line in handler.rfile:
                    request = json.loads(line)
                    command = request.pop('cmd'); ident = request.pop('id')
                    if command == 'step':
                        state['steps'] += 1
                        if state['timeout']:
                            state['pending'] = True
                            continue  # The real buffered socket reader must time out.
                        resident.obs = loaded
                        reply = {'obs': loaded, 'result': {'consumed': 0, 'loadingFrames': 1, 'stop': 'reload_ready'}}
                    elif command == 'cancel':
                        state.update(pending=False, cancels=state['cancels'] + 1)
                        reply = {}
                    else:
                        reply = Resident.bridge_call(resident, command, **request)
                        if command == 'hello': reply['frozen'] = resident.obs['scene'] == 'Level'
                    handler.wfile.write((json.dumps({'ok': True, 'id': ident, **reply}) + '\n').encode())
                    handler.wfile.flush()
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        with Server(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            client = BridgeClient(port=server.server_address[1], timeout=30)
            original_cancel = lambda: True  # A completed task must not cancel owner maintenance.
            client.cancel_check = original_cancel
            resident.client, resident.bridge_call = client, client.call
            try:
                with self.assertRaisesRegex(BridgeReloadError, '已取消等待'):
                    reload_bridge(resident, self.root, {**self.options, 'timeout_s': 1})
                self.assertFalse(state['pending'])
                self.assertEqual(state['cancels'], 1)
                self.assertEqual(client.reconnects, 1)
                self.assertIs(client.cancel_check, original_cancel)
                self.assertEqual(self.target.read_bytes(), b'previous payload')
                state['timeout'] = False
                result = reload_bridge(resident, self.root, {'retry': True})
                self.assertEqual(result['phase'], 'finished')
                self.assertEqual(state['steps'], 2)
                self.assertIs(resident.client, client)
            finally:
                client.close()
                server.shutdown(); thread.join(timeout=2)

    def test_loading_never_confirms_a_postcard_or_accepts_another_save(self):
        original = self.session.bridge_call
        loaded = copy.deepcopy(self.session.obs)
        self.session.obs = {'scene': 'LevelLoader', 'frame': 17, 'save': loaded['save']}
        def call(command, **args):
            if command == 'step':
                self.session.calls.append((command, args))
                self.session.obs = {'scene': 'LevelEnter', 'frame': 18, 'save': loaded['save'], 'storyInput': 'postcard'}
                return {'obs': self.session.obs, 'result': {'stop': 'reload_ready', 'consumed': 0, 'loadingFrames': 1}}
            return original(command, **args)
        self.session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, 'LevelEnter'):
            reload_bridge(self.session, self.root, self.options)
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        self.assertEqual([command for command, _ in self.session.calls].count('step'), 1)

    def test_results_use_preserve_mode_and_verify_ui_without_any_input(self):
        level = self.session.obs.pop('level')
        self.session.obs.update(scene='AreaComplete', completion=level, ui={'type': 'AreaComplete', 'confirmReady': False})
        result = reload_bridge(self.session, self.root, self.options)
        commit = next(args for command, args in self.session.calls if command == 'reload_commit')
        self.assertIs(commit['expect_scene_change'], False)
        self.assertEqual(result['scene_mode'], 'preserve')
        self.assertEqual(result['before']['completion']['ui']['confirmReady'], False)
        self.assertFalse(any(command in {'step', 'advance', 'record'} for command, _ in self.session.calls))

    def test_menu_freezes_before_baseline_without_save_or_session(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        session.busy_boundary = 1
        result = reload_bridge(session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(result['scene_mode'], 'preserve')
        self.assertIsNone(result['before']['boundary']['save'])
        self.assertEqual(result['before']['frame'], session.obs['frame'])
        self.assertEqual(result['before']['presentation']['menu']['selection'], 2)
        self.assertFalse(session.boundary)
        self.assertFalse(next(args for command, args in session.calls if command == 'reload_commit')['expect_scene_change'])
        self.assertFalse(any(command in {'step', 'advance', 'record'} for command, _ in session.calls))

    def prompt_session(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        session.prompt = True
        session.obs = {'scene': 'OverworldLoader', 'frame': 18, 'save': None,
                       'storyInput': 'postcard', 'storyDecision': 'postcard',
                       'story': [{'kind': 'postcard', 'text': 'unlocked'}],
                       'ui': {'type': 'Postcard', 'inputReady': True},
                       'storyPlaybackState': {'state': 'input', 'source': 'postcard'}}
        return session

    def test_loader_prompt_preserves_page_without_any_confirmation_or_wait(self):
        session = self.prompt_session()
        result = reload_bridge(session, self.root, self.options)
        self.assertEqual(result['scene_mode'], 'preserve')
        self.assertEqual(result['before']['presentation']['prompt']['story'], session.obs['story'])
        self.assertFalse(next(args for cmd, args in session.calls if cmd == 'reload_commit')['expect_scene_change'])
        self.assertFalse(any(cmd in {'step', 'advance', 'record'} for cmd, _ in session.calls))

    def test_old_runtime_refuses_prompt_before_temporary_or_resident_hold(self):
        session = self.prompt_session()
        session.origin_hold = False
        with self.assertRaisesRegex(BridgeReloadError, '1.0.5.*原场景冻结'):
            reload_bridge(session, self.root, self.options)
        self.assertFalse(any(cmd in {'reload_boundary', 'reload_prepare', 'step'} for cmd, _ in session.calls))
        self.assertEqual(self.target.read_bytes(), b'previous payload')

    def test_runtime_upgrade_mismatch_refuses_before_waiting_in_legacy_loader(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        session.obs['scene'] = 'OverworldLoader'
        self.installed_runtime.write_bytes(b'older resident')
        with self.assertRaisesRegex(BridgeReloadError, '核心变化必须维护重启'):
            reload_bridge(session, self.root, self.options)
        self.assertFalse(any(cmd in {'step', 'reload_boundary', 'reload_prepare'} for cmd, _ in session.calls))

    def test_loading_stops_at_prompt_then_reloads_without_consuming_input(self):
        session = self.prompt_session()
        session.prompt = False
        original = session.bridge_call
        def call(command, **args):
            if command == 'step':
                session.calls.append((command, args))
                self.assertEqual(args['stop_on'], ['reload_ready'])
                session.prompt = True
                return {'result': {'consumed': 0, 'stop': 'reload_ready', 'loadingFrames': 5}}
            return original(command, **args)
        session.bridge_call = call
        result = reload_bridge(session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(result['loading_wait']['consumed'], 0)
        self.assertEqual(result['before']['presentation']['storyInput'], 'postcard')

    def test_loader_prompt_changed_page_holds_and_retry_only_verifies(self):
        session = self.prompt_session()
        original = session.bridge_call
        change = True
        def call(command, **args):
            value = original(command, **args)
            if command == 'observe' and session.stage == 'ready' and change:
                value['obs']['story'][0]['text'] = 'different page'
            return value
        session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, 'presentation'):
            reload_bridge(session, self.root, self.options)
        self.assertTrue(session.holding())
        change = False
        self.assertEqual(reload_bridge(session, self.root, {'retry': True})['phase'], 'finished')
        self.assertEqual(sum(cmd == 'reload_commit' for cmd, _ in session.calls), 1)

    def test_prompt_failed_commit_and_missing_capability_retry_stay_held(self):
        session = self.prompt_session()
        session.fail = True
        with self.assertRaisesRegex(BridgeReloadError, '桥接重载失败'):
            reload_bridge(session, self.root, self.options)
        session.fail = False
        session.origin_hold = False
        with self.assertRaisesRegex(BridgeReloadError, '未报告原场景冻结'):
            reload_bridge(session, self.root, {'retry': True})
        self.assertTrue(session.holding())
        session.origin_hold = True
        self.assertEqual(reload_bridge(session, self.root, {'retry': True})['phase'], 'finished')
        self.assertEqual(sum(cmd == 'reload_commit' for cmd, _ in session.calls), 2)

    def test_legacy_payload_upgrades_to_boundary_capable_payload(self):
        original = self.session.bridge_call
        def call(command, **args):
            value = original(command, **args)
            if self.session.stage == 'ready':
                if command == 'hello':
                    value['reload_boundary_control'] = True
                elif command == 'observe':
                    value['obs']['reload_boundary'] = {'mode': 'replace', 'scene': 'Celeste.Level',
                                                       'save': value['obs']['save'], 'session': value['obs']['level']}
            return value
        self.session.bridge_call = call
        result = reload_bridge(self.session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertNotIn('boundary', result['before'])

    def test_loader_to_no_save_menu_uses_observed_identity_not_run_config(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        session.cfg.save_slot, session.cfg.save = 10, 'configured-game-save'
        session.obs['scene'] = 'OverworldLoader'
        original = session.bridge_call
        def call(command, **args):
            if command == 'step':
                session.calls.append((command, args))
                session.obs['scene'] = 'Overworld'
                return {'result': {'consumed': 0, 'stop': 'reload_ready', 'loadingFrames': 1}}
            return original(command, **args)
        session.bridge_call = call
        result = reload_bridge(session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertIsNone(result['before']['boundary']['save'])
        self.assertEqual(sum(cmd == 'step' for cmd, _ in session.calls), 1)

    def test_nonlevel_scenes_preserve_without_implicit_input(self):
        for scene in ('LevelEnter', 'IntroVignette', 'Overworld', 'ThirdPartyScene'):
            with self.subTest(scene=scene):
                session = BoundaryResident(self.target, self.installed_runtime)
                session.obs.update(scene=scene, storyInput='postcard')
                (self.root / scene).mkdir()
                result = reload_bridge(session, self.root / scene, self.options)
                self.assertEqual(result['scene_mode'], 'preserve')
                self.assertEqual(result['before']['presentation']['storyInput'], 'postcard')
                self.assertFalse(any(command == 'step' for command, _ in session.calls))

    def test_preflight_failure_releases_payload_boundary_then_retry(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        original = session.bridge_call
        fail = True
        def call(command, **args):
            value = original(command, **args)
            if command == 'observe' and session.boundary and fail:
                value['obs']['reload_boundary']['mode'] = 'wait'
            return value
        session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '边界描述'):
            reload_bridge(session, self.root, self.options)
        self.assertFalse(session.boundary)
        self.assertFalse(session.holding())
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        fail = False
        self.assertEqual(reload_bridge(session, self.root, {'retry': True})['phase'], 'finished')

    def test_cancel_after_acquire_still_releases_payload_boundary(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        original = session.bridge_call
        cancel = threading.Event()
        def call(command, **args):
            value = original(command, **args)
            if command == 'reload_boundary' and args['enabled']:
                cancel.set()
            return value
        session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '取消'):
            reload_bridge(session, self.root, self.options, cancel=cancel)
        self.assertFalse(session.boundary)
        self.assertNotIn('reload_prepare', [cmd for cmd, _ in session.calls])

    def test_preserved_menu_change_rejected_until_verified_retry(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        original = session.bridge_call
        change = True
        def call(command, **args):
            value = original(command, **args)
            if command == 'observe' and session.stage == 'ready' and change:
                value['obs']['menu']['selection'] += 1
            return value
        session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, 'presentation'):
            reload_bridge(session, self.root, self.options)
        self.assertTrue(session.holding())
        change = False
        self.assertEqual(reload_bridge(session, self.root, {'retry': True})['phase'], 'finished')
        self.assertEqual(sum(cmd == 'reload_commit' for cmd, _ in session.calls), 1)

    def test_cancel_just_before_prepare_resolves_idle_and_releases_boundary(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        original = session.bridge_call
        cancel = threading.Event()
        def call(command, **args):
            value = original(command, **args)
            if command == 'observe' and session.boundary:
                cancel.set()
            return value
        session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '结果尚未确认'):
            reload_bridge(session, self.root, self.options, cancel=cancel)
        self.assertTrue(session.boundary)
        self.assertNotIn('reload_prepare', [cmd for cmd, _ in session.calls])
        session.bridge_call = original
        with self.assertRaisesRegex(BridgeReloadError, '临时维护冻结已释放'):
            reload_bridge(session, self.root, {'retry': True})
        self.assertFalse(session.boundary)
        self.assertEqual(reload_bridge(session, self.root, {'retry': True})['phase'], 'finished')

    def test_release_connection_failure_keeps_maintenance_gate_and_can_retry(self):
        session = BoundaryResident(self.target, self.installed_runtime)
        original = session.bridge_call
        def call(command, **args):
            value = original(command, **args) if command != 'reload_boundary' or args['enabled'] else None
            if command == 'reload_boundary' and not args['enabled']:
                raise OSError('release connection lost')
            if command == 'observe' and session.boundary:
                value['obs']['reload_boundary']['mode'] = 'wait'
            return value
        session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '冻结尚未确认释放') as raised:
            reload_bridge(session, self.root, self.options)
        self.assertTrue(raised.exception.reload_held)
        state = json.loads((self.root / 'bridge_reload.json').read_text('utf-8'))
        self.assertTrue(state['boundary_release_unconfirmed'])
        self.assertTrue(state['connection_failed'])
        session.client.sock = SimpleNamespace(gettimeout=lambda: 3, settimeout=lambda value: None)
        def disconnected():
            raise OSError('reconnect still unavailable')
        session.client.reconnect = disconnected
        with self.assertRaisesRegex(BridgeReloadError, 'reconnect still unavailable') as repeated:
            reload_bridge(session, self.root, {'retry': True})
        self.assertTrue(repeated.exception.reload_held)
        self.assertTrue(json.loads((self.root / 'bridge_reload.json').read_text('utf-8'))['boundary_release_unconfirmed'])
        session.client.sock = None
        session.bridge_call = original
        self.assertEqual(reload_bridge(session, self.root, {'retry': True})['phase'], 'finished')

    def test_exit_loader_to_results_waits_without_advancing_or_confirming_results(self):
        original = self.session.bridge_call
        completion = self.session.obs.pop('level')
        results = {**self.session.obs, 'scene': 'AreaComplete', 'completion': completion,
                   'ui': {'type': 'AreaComplete', 'confirmReady': False}}
        self.session.obs = {'scene': 'LevelExit', 'frame': 17, 'save': results['save']}
        def call(command, **args):
            if command == 'step':
                self.session.calls.append((command, args))
                self.assertEqual(args['stop_on'], ['reload_ready'])
                self.session.obs = results
                return {'obs': results, 'result': {'consumed': 0, 'loadingFrames': 1, 'stop': 'reload_ready'}}
            value = original(command, **args)
            if command == 'hello':
                value['frozen'] = self.session.obs['scene'] == 'AreaComplete'
            return value
        self.session.bridge_call = call
        result = reload_bridge(self.session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(result['scene_mode'], 'preserve')
        self.assertEqual(result['loading_wait']['consumed'], 0)
        self.assertFalse(self.session.obs['ui']['confirmReady'])

    def test_results_need_new_core_and_changed_ui_stays_held(self):
        level = self.session.obs.pop('level')
        self.session.obs.update(scene='AreaComplete', completion=level, ui={'type': 'AreaComplete', 'confirmReady': False})
        original = self.session.bridge_call
        def no_cap(command, **args):
            value = original(command, **args)
            if command == 'hello': value.pop('reload_scene_modes')
            return value
        self.session.bridge_call = no_cap
        with self.assertRaisesRegex(BridgeReloadError, '核心 1.0.4'):
            reload_bridge(self.session, self.root, self.options)
        self.assertNotIn('reload_prepare', [command for command, _ in self.session.calls])
        def change_ui(command, **args):
            value = original(command, **args)
            if command == 'observe' and self.session.stage == 'ready':
                value['obs']['ui']['confirmReady'] = True
            return value
        self.session.bridge_call = change_ui
        with self.assertRaisesRegex(BridgeReloadError, '结算身份或 UI'):
            reload_bridge(self.session, self.root, self.options)
        self.assertTrue(self.session.holding())

    def test_transitioning_level_keeps_native_replacement_mode(self):
        self.session.obs['level']['transitioning'] = True
        result = reload_bridge(self.session, self.root, self.options)
        commit = next(args for command, args in self.session.calls if command == 'reload_commit')
        self.assertTrue(commit['expect_scene_change'])
        self.assertEqual(result['scene_mode'], 'replace')

    def test_missing_pico_guard_after_reload_keeps_hold_until_verified_retry(self):
        original = self.session.bridge_call
        value = False
        def call(command, **args):
            reply = original(command, **args)
            if command == 'hello':
                reply.pop('pico8_entry_guard', None)
                if value is not None:
                    reply['pico8_entry_guard'] = value
            return reply
        self.session.bridge_call = call
        for missing in (False, None):
            value = missing
            with self.subTest(value=value), self.assertRaisesRegex(BridgeReloadError, 'PICO-8'):
                reload_bridge(self.session, self.root, self.options if missing is False else {'retry': True})
            self.assertTrue(self.session.holding())
            self.assertNotIn('reload_finish', [command for command, _ in self.session.calls])
        self.session.bridge_call = original
        result = reload_bridge(self.session, self.root, {'retry': True})
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual([command for command, _ in self.session.calls].count('reload_commit'), 1)

    def test_playback_between_snapshot_and_prepare_cannot_strand_the_hold(self):
        self.session.lock = PlaybackGapLock()
        original = self.session.bridge_call
        rejected = []

        def playback():
            with self.session.lock:
                try:
                    original('playback', frames=3)
                except RuntimeError as exc:
                    rejected.append(str(exc))

        def call(cmd, **args):
            result = original(cmd, **args)
            if cmd == 'observe' and self.session.stage == 'idle':
                self.session.lock.pending = playback   # plays as soon as the owner lock is free
            return result

        self.session.bridge_call = call
        result = reload_bridge(self.session, self.root, self.options)
        self.assertEqual(result['phase'], 'finished')
        # The segment could only run after the hold existed, and the held core rejected it.
        self.assertEqual(self.session.replays, 3)
        self.assertEqual(len(rejected), 1)
        self.assertIn('maintenance hold', rejected[0])
        state = json.loads((self.root/'bridge_reload.json').read_text())
        self.assertTrue(state['before_from_hold'])

    def test_retry_uses_held_counters_instead_of_a_stale_snapshot(self):
        # A ledger whose pre-prepare snapshot missed a replay segment (written before this fix)
        # must not leave a permanently unfinishable hold: the held runtime's counters are authoritative.
        with self.assertRaisesRegex(BridgeReloadError, '版本与要求不符'):
            reload_bridge(self.session, self.root, {'expected_version': '9.9.9'})
        self.assertEqual(self.session.stage, 'ready')
        state = json.loads((self.root/'bridge_reload.json').read_text())
        state['before']['recording']['replays'] = 0
        state.pop('before_from_hold', None)
        (self.root/'bridge_reload.json').write_text(json.dumps(state))
        result = reload_bridge(self.session, self.root, {'retry': True, 'expected_version': '1.3.1'})
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(result['before']['recording']['replays'], 3)

    def test_transient_game_states_allow_room_rebuild_without_touching_recording(self):
        cases = [({'level': {'inCutscene': True}}, '剧情进行中'), ({'level': {'skippingCutscene': True}}, '正在跳过剧情'),
                 ({'level': {'completed': True}}, '本章已通关'), ({'level': {'paused': True}}, '暂停菜单'),
                 ({'level': {'frozen': True}}, '游戏冻结'), ({'level': {'transitioning': True}}, '换房间'),
                 ({'player': None}, '不在场'), ({'player': {'dead': True}}, '已死亡'),
                 ({'player': {'inControl': False}}, '不受玩家控制')]
        base = copy.deepcopy(self.session.obs)
        for change, message in cases:
            with self.subTest(message=message):
                self.session.calls.clear()
                obs = copy.deepcopy(base)
                if 'level' in change:
                    obs['level'].update(change['level'])
                else:
                    obs['player'] = change['player']
                self.session.obs = obs
                result = reload_bridge(self.session, self.root, self.options)
                self.assertEqual(result['phase'], 'finished')
                self.assertIn('临时剧情', result['room_rebuild'])
                self.assertTrue(any(c == 'reload_prepare' for c, _ in self.session.calls))
                self.assertFalse(any(c in {'record', 'step', 'activate', 'deactivate'} for c, _ in self.session.calls))
                self.assertEqual(result['before']['recording']['dir'], 'same-recording')

    def test_runtime_refusal_is_reported_without_a_hold(self):
        original = self.session.bridge_call
        self.session.bridge_call = lambda cmd, **args: (
            {'stage': 'refused', 'error': 'Reload refused: a cutscene is running or being skipped'}
            if cmd == 'reload_prepare' else original(cmd, **args))
        with self.assertRaisesRegex(BridgeReloadError, '拒绝重载.*cutscene') as raised:
            reload_bridge(self.session, self.root, self.options)
        self.assertFalse(raised.exception.reload_held)
        state = json.loads((self.root/'bridge_reload.json').read_text())
        self.assertFalse(state['prepare_outcome_unknown'])
        self.assertIsNone(state.get('lease'))
        # A refused attempt does not block the next ordinary reload.
        self.session.bridge_call = original
        self.assertEqual(reload_bridge(self.session, self.root, self.options)['phase'], 'finished')

    def test_runtime_without_binding_reapply_is_refused_before_prepare(self):
        original = self.session.bridge_call

        def old_core(cmd, **args):
            reply = original(cmd, **args)
            if cmd == 'hello':
                reply.pop('input_reinit_guard')
                reply.pop('input_bindings_ok')
            return reply

        self.session.bridge_call = old_core
        with self.assertRaisesRegex(BridgeReloadError, '核心 1.0.2'):
            reload_bridge(self.session, self.root, self.options)
        self.assertFalse(any(c == 'reload_prepare' for c, _ in self.session.calls))

    def test_wrong_bindings_after_reload_keep_hold_without_finish(self):
        original = self.session.bridge_call

        def call(cmd, **args):
            if cmd == 'hello' and self.session.stage == 'ready':
                self.session.bindings_ok = False
            return original(cmd, **args)

        self.session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, 'Grab') as raised:
            reload_bridge(self.session, self.root, self.options)
        self.assertTrue(raised.exception.reload_held)
        self.assertEqual(self.session.stage, 'ready')
        self.assertFalse(any(c == 'reload_finish' for c, _ in self.session.calls))
        self.session.bindings_ok = True
        self.session.bridge_call = original
        self.assertEqual(reload_bridge(self.session, self.root, {'retry': True})['phase'], 'finished')

    def test_different_resident_core_is_refused_before_prepare(self):
        self.installed_runtime.write_bytes(b'resident core 1.0.1')
        with self.assertRaisesRegex(BridgeReloadError, '核心变化必须维护重启'):
            reload_bridge(self.session, self.root, self.options)
        self.assertFalse(any(c == 'reload_prepare' for c, _ in self.session.calls))
        self.assertEqual(self.target.read_bytes(), b'previous payload')

    def test_core_difference_found_after_prepare_aborts_the_untouched_hold(self):
        # A bridge without hello.runtime_path: the installed core is only known from the held status.
        self.session.report_runtime = False
        self.installed_runtime.write_bytes(b'resident core 1.0.1')
        with self.assertRaisesRegex(BridgeReloadError, '已撤销') as raised:
            reload_bridge(self.session, self.root, self.options)
        self.assertFalse(raised.exception.reload_held)
        self.assertEqual(self.session.stage, 'aborted')
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        self.assertFalse(any(c == 'reload_commit' for c, _ in self.session.calls))
        self.assertEqual(json.loads((self.root/'bridge_reload.json').read_text())['phase'], 'aborted')

    def test_missing_runtime_reference_is_refused(self):
        (self.root / 'build' / 'CelesteAIRuntime.dll').unlink()
        with self.assertRaisesRegex(BridgeReloadError, '--runtime-dll'):
            reload_bridge(self.session, self.root, self.options)
        self.assertFalse(any(c == 'reload_prepare' for c, _ in self.session.calls))

    def test_abort_restores_replaced_payload_and_releases_uncommitted_hold(self):
        original = self.session.bridge_call

        def call(cmd, **args):
            if cmd == 'reload_commit':
                raise TimeoutError('commit never reached the runtime')
            return original(cmd, **args)

        self.session.bridge_call = call
        with self.assertRaises(TimeoutError):
            reload_bridge(self.session, self.root, self.options)
        self.assertEqual(self.target.read_bytes(), b'new payload')
        self.assertEqual(self.session.stage, 'prepared')
        self.session.bridge_call = original
        result = reload_bridge(self.session, self.root, {'abort': True})
        self.assertEqual(result['phase'], 'aborted')
        self.assertEqual(self.session.stage, 'aborted')
        self.assertEqual(self.target.read_bytes(), b'previous payload')
        self.assertTrue((self.root/'bridge_reload_backups/lease-123/CelesteAIBridge.dll').exists())
        with self.assertRaisesRegex(BridgeReloadError, '已经撤销'):
            reload_bridge(self.session, self.root, {'retry': True})
        self.assertFalse(any(c in {'step', 'record', 'reload_finish'} for c, _ in self.session.calls))
        # An aborted attempt does not block a fresh reload.
        self.session.stage = 'idle'
        self.assertEqual(reload_bridge(self.session, self.root, self.options)['phase'], 'finished')

    def test_abort_after_lost_prepare_ack_uses_runtime_lease(self):
        original = self.session.bridge_call

        def call(cmd, **args):
            result = original(cmd, **args)
            if cmd == 'reload_prepare':
                raise TimeoutError('prepare reply lost after hold')
            return result

        self.session.bridge_call = call
        with self.assertRaisesRegex(BridgeReloadError, '结果尚未确认'):
            reload_bridge(self.session, self.root, self.options)
        self.session.bridge_call = original
        self.assertEqual(reload_bridge(self.session, self.root, {'abort': True})['phase'], 'aborted')
        self.assertEqual(self.session.stage, 'aborted')

    def test_committed_reload_cannot_be_aborted_and_says_how_to_recover(self):
        self.session.fail = True
        with self.assertRaisesRegex(BridgeReloadError, 'bad build'):
            reload_bridge(self.session, self.root, self.options)
        self.assertEqual(self.session.stage, 'failed')
        with self.assertRaisesRegex(BridgeReloadError, '--retry.*重启游戏') as raised:
            reload_bridge(self.session, self.root, {'abort': True})
        self.assertTrue(raised.exception.reload_held)
        self.assertEqual(self.session.stage, 'failed')
        self.assertFalse(any(c == 'reload_abort' for c, _ in self.session.calls))
        original = self.session.bridge_call
        self.session.bridge_call = lambda cmd, **args: (
            {**original(cmd, **args), 'restart_required': True} if cmd == 'reload_status' else original(cmd, **args))
        with self.assertRaisesRegex(BridgeReloadError, '只能重启游戏'):
            reload_bridge(self.session, self.root, {'abort': True})

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
        result = reload_bridge(self.session, self.root, {'retry': True, 'expected_version': '1.3.1'})
        self.assertEqual(result['expected_version'], '1.3.1')
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


class HoldReportingTests(unittest.TestCase):
    def test_options_keep_abort_separate_from_retry_and_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            dll = Path(directory) / 'CelesteAIBridge.dll'
            dll.write_bytes(b'x')
            for data in ({'abort': True, 'retry': True}, {'abort': True, 'dll': str(dll)},
                         {'abort': True, 'expected_version': '1.3.1'}, {'abort': 'yes'}):
                with self.subTest(data=data), self.assertRaises(ValueError):
                    options(data)
            with self.assertRaisesRegex(ValueError, 'CelesteAIRuntime'):
                options({'runtime_dll': str(dll)})
            self.assertTrue(options({'abort': True})['abort'])

    def test_restarted_controller_refuses_a_held_game_with_recovery_advice(self):
        from orchestrator.bridge_requirements import validate_bridge
        self.assertIsNone(hold_message({'bridge': '1.3.1', 'reload_hold': False}))
        abortable = {'bridge': None, 'reload_hold': True, 'reload': {'stage': 'prepared', 'abortable': True}}
        self.assertIn('--abort --direct', hold_message(abortable))
        committed = {'reload_hold': True, 'reload': {'stage': 'failed', 'error': 'bad build', 'abortable': False}}
        self.assertIn('--retry', hold_message(committed))
        self.assertIn('bad build', hold_message(committed))
        restart = {'reload_hold': True, 'reload': {'stage': 'failed', 'restart_required': True}}
        self.assertIn('只能保存好其他工作后重启游戏', hold_message(restart))
        with self.assertRaisesRegex(RuntimeError, '仍被桥接重载交接冻结'):
            validate_bridge(abortable)

    def test_session_connect_never_activates_into_a_held_game(self):
        from orchestrator import game_session

        class HeldClient:
            def __init__(self, *args, **kwargs):
                self.calls = []
                HeldClient.instance = self

            def call(self, cmd, **args):
                self.calls.append(cmd)
                return {'bridge': None, 'reload_hold': True, 'reload': {'stage': 'prepared', 'abortable': True}}

        session = game_session.GameSession.__new__(game_session.GameSession)
        session.cfg = SimpleNamespace(bridge_port=1)
        original = game_session.BridgeClient
        game_session.BridgeClient = HeldClient
        try:
            with self.assertRaisesRegex(RuntimeError, '不接管'):
                session.connect()
        finally:
            game_session.BridgeClient = original
        self.assertEqual(HeldClient.instance.calls, ['hello'])


class FakeCore:
    """Line-protocol stand-in for the resident core, for the --direct CLI path."""

    def __init__(self, stage='prepared', abortable=True):
        self.stage, self.abortable = stage, abortable
        self.commands = []
        self.server = socket.create_server(('127.0.0.1', 0))
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn, conn.makefile('rwb') as stream:
            for line in stream:
                request = json.loads(line)
                self.commands.append(request['cmd'])
                if request['cmd'] == 'reload_abort' and self.abortable and request.get('lease') == 'lease-9':
                    self.stage = 'aborted'
                reply = {'id': request['id'], 'ok': True, 'stage': self.stage, 'lease': 'lease-9',
                         'holding': self.stage != 'aborted', 'abortable': self.abortable and self.stage == 'prepared',
                         'restart_required': False, 'error': ''}
                stream.write((json.dumps(reply) + '\n').encode())
                stream.flush()

    def close(self):
        self.server.close()


class DirectAbortCliTests(unittest.TestCase):
    def run_cli(self, *args):
        unused = socket.create_server(('127.0.0.1', 0))
        hud_port = unused.getsockname()[1]
        unused.close()   # nothing listens: the original controller has exited
        return subprocess.run([sys.executable, '-X', 'utf8', 'tools/reload_bridge.py', '--hud-port', str(hud_port), *args],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, encoding='utf-8', timeout=20)

    def test_direct_abort_restores_files_from_run_ledger_and_releases_hold(self):
        core = FakeCore()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                installed = root / 'Mods' / 'CelesteAIBridge' / 'bin' / 'CelesteAIBridge.dll'
                installed.parent.mkdir(parents=True)
                installed.write_bytes(b'replaced payload')
                backup = root / 'bridge_reload_backups' / 'lease-9' / 'CelesteAIBridge.dll'
                backup.parent.mkdir(parents=True)
                backup.write_bytes(b'running payload')
                (root / 'bridge_reload.json').write_text(json.dumps({
                    'lease': 'lease-9', 'phase': 'failed', 'resume_phase': 'replaced', 'before': {'frame': 1},
                    'files': [{'path': str(installed), 'backup': str(backup)}]}), encoding='utf-8')
                result = self.run_cli('--abort', '--direct', '--bridge-port', str(core.port), '--run-dir', str(root))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('已撤销', result.stdout)
                self.assertEqual(installed.read_bytes(), b'running payload')
                self.assertEqual(json.loads((root / 'bridge_reload.json').read_text())['phase'], 'aborted')
        finally:
            core.close()
        self.assertEqual(core.stage, 'aborted')
        self.assertNotIn('reload_commit', core.commands)

    def test_direct_abort_refuses_committed_hold(self):
        core = FakeCore(stage='failed', abortable=False)
        try:
            result = self.run_cli('--abort', '--direct', '--bridge-port', str(core.port))
        finally:
            core.close()
        self.assertEqual(result.returncode, 2)
        self.assertIn('不能撤销', result.stderr)
        self.assertNotIn('reload_abort', core.commands)

    def test_direct_requires_abort(self):
        result = self.run_cli('--direct')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--direct 只用于 --abort', result.stderr)


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
    async def test_completed_task_with_owned_connection_can_reload_but_never_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'; target.write_bytes(b'installed')
            session = Resident(target)
            session.done = True
            session.bridge_reload_supported = True
            owner = Controls(root, session)
            owner.chat_only = True
            self.assertTrue(owner._bridge_reload_status()['bridge_reload_control'])
            owner.control.enqueue('bridge_reload', '{}')
            await owner._reload_bridge(owner.control.pending()[0])
            self.assertTrue(session.done)
            self.assertTrue(owner.paused)
            self.assertEqual(owner.pending_messages, [])
            self.assertEqual(owner.control.snapshot()['actions'][0]['status'], 'submitted')
            session.client.sock = SimpleNamespace(fileno=lambda: -1)
            self.assertFalse(owner._bridge_reload_status()['bridge_reload_control'])
            session.client = None
            self.assertFalse(owner._bridge_reload_status()['bridge_reload_control'])
            with self.assertRaisesRegex(ValueError, '没有游戏连接'):
                await owner._reload_bridge({'id': 123, 'text': '{}'})

    async def test_results_notification_describes_preserved_scene_not_room_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'; target.write_bytes(b'installed')
            session = Resident(target)
            session.obs.update(scene='AreaComplete', completion=session.obs.pop('level'),
                               ui={'type': 'AreaComplete', 'confirmReady': True})
            owner = Controls(root, session)
            owner.control.enqueue('bridge_reload', '{}')
            await owner._reload_bridge(owner.control.pending()[0])
            self.assertIn('游戏场景保持原对象', owner.pending_messages[-1]['text'])
            self.assertNotIn('当前房间经', owner.pending_messages[-1]['text'])

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

    async def test_abort_control_releases_uncommitted_hold_and_restores_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'CelesteAIBridge.dll'
            target.write_bytes(b'installed')
            session = Resident(target)
            original = session.bridge_call

            def call(cmd, **args):
                if cmd == 'reload_commit':
                    raise TimeoutError('commit never reached the runtime')
                return original(cmd, **args)

            session.bridge_call = call
            owner = Controls(root, session)
            owner.control.enqueue('bridge_reload', '{}')
            with self.assertRaises(TimeoutError):
                await owner._reload_bridge(owner.control.pending()[0])
            self.assertTrue(owner._bridge_reload_failed)
            self.assertEqual(session.stage, 'prepared')
            session.bridge_call = original
            ident = owner.control.enqueue('bridge_reload', '{"abort": true}')
            row = next(r for r in owner.control.pending() if r['id'] == ident)
            await owner._reload_bridge(row)
            self.assertEqual(session.stage, 'aborted')
            self.assertFalse(owner._bridge_reload_failed)
            self.assertFalse(owner.paused)
            self.assertEqual(owner.pending_messages, [])
            action = next(a for a in owner.control.snapshot()['actions'] if a['id'] == ident)
            self.assertEqual(action['status'], 'submitted')
            self.assertIn('已撤销', action['detail'])

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
