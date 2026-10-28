"""原生菜单宏和维护编排；仅游戏/进程/编码器边界使用替身。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.game_exit import exit_game_by_menu
from tools.bridge_client import BridgeClient
from orchestrator.maintenance import save_and_exit
from calibration import recording_session_test as recording_fixture


class MenuGame:
    def __init__(self, level=False, disconnect=False):
        self.level, self.disconnect, self.exited = level, disconnect, False
        self.calls, self.saved, self.paused = [], False, False
        self.selected = 0
        self.hello = {'bridge': '1.5.0', 'process_id': 123, 'record_audio_active': False,
                      'clock': {'lockstep': False, 'plugin': {'state': 'idle'}}}

    def observation(self):
        obs = {'scene': 'Level' if self.level else 'Overworld', 'save': {'slot': 10}, 'saving': False}
        if self.level:
            obs['level'] = {'canPause': True, 'paused': self.paused}
            if self.paused:
                obs['pauseMenu'] = {'focused': True, 'selection': 0, 'items': [
                    {'index': 0, 'id': 'menu_pause_savequit', 'selectable': True, 'disabled': False}]}
        else:
            # Irregular layout plus an unrelated mod button: use actual edges.
            obs['menu'] = {'current': 'OuiMainMenu', 'next': None, 'focused': True, 'selection': self.selected,
                           'buttons': [{'index': 0, 'id': 'menu_begin', 'ready': True, 'R': 2},
                                       {'index': 1, 'id': 'mod_option', 'ready': True},
                                       {'index': 2, 'id': 'menu_exit', 'ready': True, 'L': 0}]}
        return obs

    def call(self, cmd, **args):
        if self.exited:
            raise AssertionError('Game RPC used after confirmed exit: ' + cmd)
        self.calls.append((cmd, args))
        if cmd == 'hello':
            return copy.deepcopy(self.hello)
        if cmd == 'step':
            for segment in args['inputs']:
                keys = segment.split(',')[1:]
                if self.level:
                    if 'P' in keys:
                        self.paused = True
                    if 'C' in keys:
                        self.saved, self.level = True, False
                elif 'R' in keys:
                    self.selected = 2
                elif 'C' in keys:
                    if self.selected != 2:
                        raise AssertionError('Confirmed wrong main-menu action')
                    self.exited = True
                    if self.disconnect:
                        raise ConnectionError('normal shutdown')
            return {'obs': self.observation(), 'result': {'consumed': 1}}
        return {'obs': self.observation()}


class Process:
    pid = 123
    def __init__(self, game, code=0):
        self.game, self.code, self.closed = game, code, False

    def wait(self, timeout):
        if not self.game.exited:
            raise RuntimeError('still running')
        return self.code

    def close(self):
        self.closed = True


class GameExitTests(unittest.TestCase):
    def test_actual_graph_navigation_and_separate_confirm_with_optional_eof(self):
        for eof in (False, True):
            game = MenuGame(disconnect=eof)
            result = exit_game_by_menu(game, Process(game))
            self.assertTrue(result['exited'])
            steps = [args['inputs'] for cmd, args in game.calls if cmd == 'step']
            self.assertEqual(steps, [['1,R', '6'], ['1,C']])
            self.assertFalse(game.calls[-1][1]['allow_reconnect'])

    def test_final_confirmation_send_failure_cannot_reconnect_or_resend(self):
        client = BridgeClient.__new__(BridgeClient)
        client.next_id, client.cancel_check, client.step_options = 1, None, None
        client.file = Mock()
        client.file.flush.side_effect = OSError('possibly partially delivered quit confirmation')
        client.reconnect = Mock()
        with self.assertRaises(OSError):
            client.call('step', inputs=['1,C'], allow_reconnect=False)
        client.file.write.assert_called_once()
        client.reconnect.assert_not_called()

    def test_eof_or_abnormal_exit_never_proves_normal_shutdown(self):
        for code in (3, None):
            game = MenuGame(disconnect=True)
            process = Process(game, code)
            if code is None:
                process.wait = lambda timeout: (_ for _ in ()).throw(RuntimeError('still running'))
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                exit_game_by_menu(game, process)
            self.assertEqual(sum(args.get('inputs') == ['1,C'] for cmd, args in game.calls), 1)

    def test_busy_recording_wrong_process_and_unknown_menu_refuse_before_confirm(self):
        for kind in ('audio', 'finishing', 'process', 'menu', 'unreachable'):
            game = MenuGame()
            if kind == 'audio': game.hello['record_audio_active'] = True
            if kind == 'finishing': game.hello['clock']['plugin']['state'] = 'finishing'
            if kind == 'process': game.hello['process_id'] = 456
            if kind in {'menu', 'unreachable'}:
                original = game.observation
                def obs():
                    value = original()
                    if kind == 'menu': value['menu']['current'] = 'ModMenu'
                    else: value['menu']['buttons'][0].pop('R')
                    return value
                game.observation = obs
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                exit_game_by_menu(game, Process(game))
            self.assertFalse(game.exited)

    def test_maintenance_saves_then_exits_before_real_recorder_postprocessing(self):
        fixture = recording_fixture.RecordingSessionTests()
        fixture.setUp()
        try:
            recorder = fixture.start(); fixture.advance(3); recorder.retain()
            (fixture.directory/'gameplay.mp4').write_bytes(b'encoder movie fixture')
            (fixture.first/'summary.json').write_text('{"game_released":false}')
            game = MenuGame(level=True)
            game.hello.update(copy.deepcopy(fixture.hello))
            original = game.call
            def call(cmd, **args):
                if cmd == 'record':
                    self.assertTrue(game.saved, 'Stopped recording before save/quit')
                    fixture.call(cmd, **args)
                    game.hello.update(copy.deepcopy(fixture.hello))
                    return game.hello
                return original(cmd, **args)
            game.call = call
            process = Process(game)
            def pure(*args, **kwargs):
                self.assertTrue(game.exited, 'Video postprocessing blocked game shutdown')
                self.assertTrue(json.loads((fixture.first/'summary.json').read_text())['game_released'])
                return {'state': 'skipped'}
            with patch('orchestrator.maintenance.GameProcess', return_value=process), \
                 patch('orchestrator.recording.make_pure', side_effect=pure):
                result = save_and_exit(game, save_slot=10, run_dir=fixture.first)
            self.assertEqual(result['phase'], 'finished')
            self.assertTrue(process.closed)
            self.assertEqual(result['recording']['state'], 'finished')
        finally:
            fixture.tearDown()

    def test_maintenance_rechecks_slot_after_activation_before_saving(self):
        game = MenuGame(level=True)
        original = game.call
        def call(cmd, **args):
            reply = original(cmd, **args)
            if cmd == 'activate':
                reply['obs']['save']['slot'] = 11
            return reply
        game.call = call
        with patch('orchestrator.maintenance.GameProcess', return_value=Process(game)), \
             self.assertRaisesRegex(RuntimeError, '槽位 11'):
            save_and_exit(game, save_slot=10, run_dir=None)
        self.assertFalse(game.saved or game.exited)
        self.assertFalse(any(cmd in {'step', 'record'} for cmd, _ in game.calls))

    def test_terminal_callback_once_before_offline_work_and_cached_retry(self):
        fixture = recording_fixture.RecordingSessionTests(); fixture.setUp()
        try:
            recorder = fixture.start(); fixture.advance(1)
            (fixture.directory/'gameplay.mp4').write_bytes(b'encoder movie fixture')
            callbacks = []
            with patch('orchestrator.recording.make_pure', return_value={'state': 'skipped'}):
                recorder.stop()
                recorder.stop(on_encoder_finished=lambda: callbacks.append('exit'))
                recorder.stop(on_encoder_finished=lambda: callbacks.append('duplicate'))
            self.assertEqual(callbacks, ['exit'])
        finally:
            fixture.tearDown()

    def test_encoder_finishing_timeout_does_not_quit_then_retry_notifies_once(self):
        fixture = recording_fixture.RecordingSessionTests(); fixture.setUp()
        try:
            recorder = fixture.start(); fixture.advance(1)
            (fixture.directory/'gameplay.mp4').write_bytes(b'encoder movie fixture')
            original = fixture.call
            def slow_stop(cmd, **args):
                reply = original(cmd, **args)
                if cmd == 'record':
                    fixture.hello['clock']['plugin']['state'] = 'finishing'
                return reply
            recorder.call = slow_stop
            callbacks = []
            pending = recorder.stop(timeout=0, on_encoder_finished=lambda: callbacks.append('too early'))
            self.assertTrue(pending['pending'])
            self.assertEqual(callbacks, [])
            fixture.hello['clock']['plugin']['state'] = 'finished'
            with patch('orchestrator.recording.make_pure', return_value={'state': 'skipped'}):
                result = recorder.stop(on_encoder_finished=lambda: callbacks.append('quit'))
            self.assertFalse(result['pending'])
            self.assertEqual(callbacks, ['quit'])
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
