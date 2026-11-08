"""空闲更新复用真实重载状态机，仅替换 OS 准入与外部游戏 RPC。"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from calibration.bridge_reload_test import BoundaryResident, FakeCore
from orchestrator import idle_bridge_reload as idle
from orchestrator.game_process import idle_bridge_listener


class IdleResident(BoundaryResident):
    def __init__(self, *args):
        super().__init__(*args)
        self.obs = {'scene': 'Level', 'frame': 18, 'save': {'slot': 10, 'name': 'test'},
                    'level': {'sid': 'Celeste/3-CelestialResort', 'side': 'A', 'room': '01-b', 'chapterTimeTicks': 12345}}
        self.active = False
        self.lose = None
        self.recording = False
        self.interrupt = False

    def bridge_call(self, command, **args):
        if command in {'activate', 'deactivate'}:
            self.calls.append((command, args))
            self.active = command == 'activate'
            reply = {'obs': copy.deepcopy(self.obs)} if self.active else {}
        else:
            reply = super().bridge_call(command, **args)
            if command == 'hello':
                reply.update(active=self.active, reload_hold=self.holding(), process_id=700,
                             record_audio_active=self.recording,
                             clock={'plugin': {'state': 'recording' if self.recording else 'idle',
                                               'recording': self.recording}})
            if 'record_audio_active' in reply:
                reply['record_audio_active'] = self.recording
        if self.lose == command:
            self.lose = None
            if self.interrupt:
                raise KeyboardInterrupt()
            raise OSError('reply lost after application')
        return reply


class IdleReloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'maintenance'
        self.source = self.root / 'build/CelesteAIBridge.dll'
        self.source.parent.mkdir()
        self.source.write_bytes(b'new bridge')
        (self.source.parent / 'CelesteAIRuntime.dll').write_bytes(b'same runtime')
        self.runtime = self.root / 'game/Mods/CelesteAIRuntime/bin/CelesteAIRuntime.dll'
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_bytes(b'same runtime')
        self.target = self.root / 'game/Mods/CelesteAIBridge/bin/CelesteAIBridge.dll'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'old bridge')
        self.resident = IdleResident(self.target, self.runtime)
        self.identity = {'pid': 700, 'created': 123456, 'executable': str(self.root / 'game/Celeste.exe')}
        self.clients = []
        self.leased = False

        @contextmanager
        def lease():
            self.assertFalse(self.leased)
            self.leased = True
            try:
                yield
            finally:
                self.leased = False

        def connect(**kwargs):
            self.assertTrue(self.leased)
            client = SimpleNamespace(sock=None, timeout=10, closed=False, reconnect=lambda: None)
            def call(command, **args):
                self.assertIs(args.pop('allow_reconnect'), False)
                return self.resident.bridge_call(command, **args)
            client.call = call
            client.close = lambda: setattr(client, 'closed', True)
            self.clients.append(client)
            return client

        self.process = SimpleNamespace(identity=lambda: dict(self.identity), close=lambda: None)
        self.admission = self.enterContext(patch.object(idle, 'idle_bridge_listener', return_value=700))
        self.enterContext(patch.object(idle, 'GameProcess', return_value=self.process))
        self.enterContext(patch.object(idle, 'controller_lease', lease))
        self.connect = self.enterContext(patch.object(idle, 'BridgeClient', side_effect=connect))

    def run_reload(self, request=None, **kwargs):
        return idle.reload_idle(47774, request or {'dll': str(self.source), 'expected_version': '1.3.1'},
                                directory=self.directory, game_dir=self.root / 'game',
                                save_slot=kwargs.pop('save_slot', 10), **kwargs)

    def metadata(self):
        return json.loads((self.directory / 'idle_maintenance.json').read_text(encoding='utf-8'))

    def commands(self):
        return [command for command, args in self.resident.calls]

    def test_success_replaces_bridge_only_and_returns_the_original_idle_game(self):
        result = self.run_reload()
        self.assertEqual(result['phase'], 'finished')
        self.assertTrue(result['idle_control_returned'])
        self.assertEqual(self.target.read_bytes(), b'new bridge')
        self.assertEqual(self.runtime.read_bytes(), b'same runtime')
        self.assertFalse(self.resident.active)
        self.assertEqual(self.metadata()['phase'], 'returned')
        self.assertEqual(self.commands().count('reload_commit'), 1)
        self.assertEqual(self.commands().count('deactivate'), 1)
        self.assertNotIn('step', self.commands())
        self.assertNotIn('record', self.commands())
        self.assertTrue(all(client.closed for client in self.clients))
        self.assertFalse(self.leased)

    def test_occupied_port_refuses_before_connect(self):
        self.admission.side_effect = RuntimeError('already connected')
        with self.assertRaisesRegex(RuntimeError, 'already connected'):
            self.run_reload()
        self.connect.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'old bridge')

    def test_active_recording_wrong_target_and_changed_runtime_never_activate(self):
        for refusal in ('active', 'recording', 'target', 'runtime'):
            with self.subTest(refusal=refusal):
                self.resident.active = refusal == 'active'
                self.resident.recording = refusal == 'recording'
                self.resident.runtime = str(self.runtime if refusal != 'target' else self.root / 'other/Mods/Core/bin/Core.dll')
                self.runtime.write_bytes(b'different' if refusal == 'runtime' else b'same runtime')
                with self.assertRaises((RuntimeError, OSError)):
                    self.run_reload()
                self.assertNotIn('activate', self.commands())
                self.assertNotIn('deactivate', self.commands())
                self.assertEqual(self.target.read_bytes(), b'old bridge')

    def test_lost_activate_reply_is_reconciled_and_control_returned_without_reload(self):
        self.resident.lose = 'activate'
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertFalse(self.resident.active)
        self.assertEqual(self.metadata()['phase'], 'returned')
        self.assertNotIn('reload_commit', self.commands())

    def test_failed_intent_write_never_activates_or_deactivates(self):
        with patch.object(idle, 'write_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_reload()
        self.assertNotIn('activate', self.commands())
        self.assertNotIn('deactivate', self.commands())
        self.assertTrue(self.clients[0].closed)

    def test_wrong_save_or_rejected_preflight_returns_control_without_replacing_files(self):
        for wrong_save in (True, False):
            with self.subTest(wrong_save=wrong_save):
                self.resident.bindings_ok = wrong_save
                with self.assertRaises(RuntimeError):
                    self.run_reload(save_slot=11 if wrong_save else 10)
                self.assertFalse(self.resident.active)
                self.assertEqual(self.metadata()['phase'], 'returned')
                self.assertEqual(self.target.read_bytes(), b'old bridge')
                self.assertNotIn('reload_commit', self.commands())

    def test_lost_prepare_reply_reconciles_lease_without_repeating_prepare(self):
        self.resident.lose = 'reload_prepare'
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertTrue(self.resident.active)
        self.assertNotIn('deactivate', self.commands())
        self.assertTrue(self.run_reload({'retry': True})['idle_control_returned'])
        self.assertEqual(self.commands().count('reload_prepare'), 1)
        self.assertEqual(self.commands().count('reload_commit'), 1)

    def test_prepared_unknown_reply_can_be_aborted_then_returns_control(self):
        self.resident.lose = 'reload_prepare'
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertEqual(self.run_reload({'abort': True})['phase'], 'aborted')
        self.assertFalse(self.resident.active)
        self.assertEqual(self.target.read_bytes(), b'old bridge')
        self.assertNotIn('reload_commit', self.commands())

    def test_keyboard_interrupt_without_hold_returns_control_and_reports_cancellation(self):
        self.resident.interrupt = True
        self.resident.lose = 'activate'
        with self.assertRaisesRegex(RuntimeError, '用户取消'):
            self.run_reload()
        self.assertFalse(self.resident.active)
        self.assertEqual(self.metadata()['phase'], 'returned')
        self.assertNotIn('reload_commit', self.commands())

    def test_keyboard_interrupt_with_unknown_prepare_keeps_hold_and_can_abort(self):
        self.resident.interrupt = True
        self.resident.lose = 'reload_prepare'
        with self.assertRaisesRegex(RuntimeError, '--direct --retry --bridge-port 47774'):
            self.run_reload()
        self.assertTrue(self.resident.active)
        self.assertNotIn('deactivate', self.commands())
        self.assertEqual(self.run_reload({'abort': True})['phase'], 'aborted')
        self.assertFalse(self.resident.active)

    def test_committed_failure_keeps_hold_then_retry_returns_control(self):
        self.resident.fail = True
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertTrue(self.resident.active)
        self.assertNotIn('deactivate', self.commands())
        self.resident.fail = False
        self.assertEqual(self.run_reload({'retry': True})['phase'], 'finished')
        self.assertFalse(self.resident.active)
        self.assertEqual(self.commands().count('activate'), 1)

    def test_lost_finish_ack_is_retried_without_a_second_commit(self):
        self.resident.lose = 'reload_finish'
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertNotIn('deactivate', self.commands())
        self.assertEqual(self.run_reload({'retry': True})['phase'], 'finished')
        self.assertEqual(self.commands().count('reload_commit'), 1)
        self.assertFalse(self.resident.active)

    def test_lost_finish_ack_recovers_even_when_the_released_menu_advances(self):
        self.resident.obs = {'scene': 'Overworld', 'frame': 18, 'save': None,
                             'menu': {'current': 'OuiMainMenu', 'selection': 2}}
        self.resident.lose = 'reload_finish'
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertEqual(self.resident.stage, 'finished')
        hello = self.resident.bridge_call('hello')
        self.assertFalse(hello['frozen'])
        self.resident.obs['menu']['selection'] = 3
        self.assertTrue(self.run_reload({'retry': True})['idle_control_returned'])
        self.assertEqual(self.commands().count('reload_commit'), 1)
        self.assertEqual(self.commands().count('reload_finish'), 1)
        self.assertFalse(self.resident.active)

    def test_lost_deactivate_reply_does_not_repeat_reload_or_deactivate(self):
        self.resident.lose = 'deactivate'
        with self.assertRaises(RuntimeError):
            self.run_reload()
        self.assertEqual(self.metadata()['phase'], 'returning')
        self.assertTrue(self.run_reload({'retry': True})['idle_control_returned'])
        self.assertEqual(self.commands().count('reload_commit'), 1)
        self.assertEqual(self.commands().count('deactivate'), 1)

    def test_retry_refuses_pid_reuse_before_connect(self):
        self.resident.fail = True
        with self.assertRaises(RuntimeError):
            self.run_reload()
        before = self.connect.call_count
        self.identity['created'] += 1
        with self.assertRaisesRegex(RuntimeError, '身份已变化'):
            self.run_reload({'retry': True})
        self.assertEqual(self.connect.call_count, before)


@unittest.skipUnless(os.name == 'nt', 'Windows TCP table admission')
class TcpAdmissionTests(unittest.TestCase):
    def test_an_existing_inactive_connection_is_rejected_before_a_second_connect(self):
        with socket.create_server(('127.0.0.1', 0)) as server:
            port = server.getsockname()[1]
            self.assertEqual(idle_bridge_listener(port), os.getpid())
            with socket.create_connection(('127.0.0.1', port)), server.accept()[0]:
                with self.assertRaisesRegex(RuntimeError, '已有连接'):
                    idle_bridge_listener(port)


class DirectRoutingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('orchestrator.game_process.CONTROLLER_MUTEX',
                                'Local\\CelesteIdleReloadTest-' + uuid.uuid4().hex))

    def test_completed_idle_history_does_not_hide_a_later_player_hold(self):
        from tools import reload_bridge as cli
        core = FakeCore()
        self.addCleanup(core.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'idle_maintenance.json').write_text(json.dumps({
                'mode': 'idle_maintenance', 'port': core.port, 'phase': 'returned'}), encoding='utf-8')
            with patch.object(idle, 'maintenance_directory', return_value=path), \
                 patch.object(cli, 'request', return_value={'control': {'connected': False}}), \
                 patch('sys.argv', ['reload_bridge.py', '--direct', '--abort', '--bridge-port', str(core.port)]):
                self.assertEqual(cli.main(), 0)
        self.assertEqual(core.stage, 'aborted')
        self.assertIn('reload_abort', core.commands)

    def test_legacy_abort_rejects_wrong_game_before_restoring_or_releasing(self):
        from tools import reload_bridge as cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = FakeCore(payload_path=str(root / 'actual/Mods/CelesteAIBridge/bin/CelesteAIBridge.dll'))
            self.addCleanup(core.close)
            with patch.object(idle, 'maintenance_directory', return_value=root / 'maintenance'), \
                 patch.object(cli, 'request', return_value={'control': {'connected': False}}), \
                 patch('sys.argv', ['reload_bridge.py', '--direct', '--abort', '--bridge-port', str(core.port),
                                    '--game-dir', str(root / 'wrong')]):
                self.assertEqual(cli.main(), 2)
            self.assertEqual(core.stage, 'prepared')
            self.assertNotIn('reload_abort', core.commands)
