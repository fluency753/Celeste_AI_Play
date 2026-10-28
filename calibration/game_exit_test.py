"""原生菜单宏和维护编排；仅游戏/进程/编码器边界使用替身。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.game_exit import exit_game_by_menu
from tools.bridge_client import BridgeClient
from orchestrator.maintenance import save_and_exit, _wait_for_save_ready
from calibration import recording_session_test as recording_fixture


class MenuGame:
    def __init__(self, level=False, disconnect=False):
        self.level, self.disconnect, self.exited = level, disconnect, False
        self.calls, self.saved, self.paused = [], False, False
        self.selected = 0
        self.hello = {'bridge': '1.5.1', 'process_id': 123, 'record_audio_active': False,
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


class EntryGame(MenuGame):
    def __init__(self, states):
        super().__init__(level=True)
        self.states = copy.deepcopy(states)
        self.activated = False

    def observation(self):
        if self.states:
            return copy.deepcopy({'save': {'slot': 10}, **self.states[0]})
        return super().observation()

    def call(self, cmd, **args):
        if cmd == 'activate':
            self.activated = True
        if self.states and (cmd == 'step' or (cmd == 'observe' and self.activated
                                              and self.states[0]['scene'] == 'LevelLoader')):
            self.calls.append((cmd, args))
            if cmd == 'step':
                if args['inputs'] != ['1'] or set(args.get('stop_on', [])) != {'story_input', 'menu_prompt'}:
                    raise AssertionError('Entry wait must use one neutral frame and stop at story input')
            self.states.pop(0)
            return {'obs': self.observation(), 'result': {'consumed': int(cmd == 'step')}}
        return super().call(cmd, **args)


class GameExitTests(unittest.TestCase):
    def test_entry_loader_wipe_and_room_transition_continue_to_save_and_exit(self):
        game = EntryGame([
            {'scene': 'LevelEnter', 'frame': 0}, {'scene': 'LevelLoader', 'frame': 1},
            {'scene': 'Level', 'frame': 2, 'level': {'canPause': False, 'screenWipe': True}},
            {'scene': 'Level', 'frame': 3, 'level': {'canPause': False, 'transitioning': True}}])
        progress = []
        with patch('orchestrator.maintenance.GameProcess', return_value=Process(game)), \
             patch('orchestrator.maintenance.time.sleep'):
            result = save_and_exit(game, save_slot=10, run_dir=None, progress=progress.append)
        self.assertEqual(result['phase'], 'finished')
        self.assertTrue(game.saved and game.exited)
        self.assertEqual(sum(row['phase'] == 'waiting_transition' for row in progress), 4)
        self.assertEqual(next(row['transition_frames'] for row in progress if row['phase'] == 'transition_ready'), 4)
        self.assertIsNone(result['scene'])
        self.assertEqual(sum(args.get('inputs') == ['1'] for _, args in game.calls), 3)

    def test_wait_refuses_story_input_and_slot_change_before_any_save_or_record(self):
        for state in ({'scene': 'LevelEnter', 'storyInput': 'postcard'},
                      {'scene': 'LevelEnter', 'story': [{'kind': 'postcard', 'waitingForInput': True}]},
                      {'scene': 'LevelEnter', 'save': {'slot': 11}},
                      {'scene': 'Level', 'level': {'canPause': False, 'inCutscene': True}}):
            for immediate in (False, True):
                with self.subTest(state=state, immediate=immediate):
                    game = EntryGame(([{'scene': 'LevelEnter'}] if not immediate else []) + [state])
                    progress = []
                    with patch('orchestrator.maintenance.GameProcess', return_value=Process(game)), \
                         self.assertRaises(RuntimeError):
                        save_and_exit(game, save_slot=10, run_dir=None, progress=progress.append)
                    self.assertFalse(game.saved or game.exited)
                    self.assertFalse(any(cmd == 'record' for cmd, _ in game.calls))
                    self.assertTrue(all(args['inputs'] == ['1'] for cmd, args in game.calls if cmd == 'step'))
                    self.assertEqual(progress[-1].get('transition_frames', 0), int(not immediate))

    def test_maintenance_reports_loader_prompt_animation_without_polling(self):
        client = Mock()
        obs = {'scene': 'OverworldLoader', 'ui': {'type': 'Postcard', 'inputReady': False}}
        with self.assertRaisesRegex(RuntimeError, '原生解锁提示.*不能仅靠等待'):
            _wait_for_save_ready(client, obs, lambda _: None, lambda **_: None)
        client.call.assert_not_called()

    def test_wait_frame_budget_includes_loading_and_wall_timeout(self):
        game = EntryGame([{'scene': 'LevelEnter', 'frame': i} for i in range(4)])
        with self.assertRaisesRegex(RuntimeError, '超时'):
            _wait_for_save_ready(game, game.observation(), lambda _: None, lambda **_: None, max_frames=2)
        self.assertEqual(sum(cmd == 'step' for cmd, _ in game.calls), 2)
        game = EntryGame([{'scene': 'LevelLoader', 'frame': 0},
                          {'scene': 'LevelLoader', 'frame': 10}])
        game.activated = True
        with patch('orchestrator.maintenance.time.sleep'), self.assertRaisesRegex(RuntimeError, '帧预算'):
            _wait_for_save_ready(game, game.observation(), lambda _: None, lambda **_: None, max_frames=2)
        self.assertFalse(any(cmd == 'step' for cmd, _ in game.calls))
        client = Mock()
        client.call.return_value = {'obs': {'scene': 'Level', 'level': {'canPause': True}},
                                    'result': {'consumed': 1, 'loadingFrames': 5}}
        with self.assertRaisesRegex(RuntimeError, '帧预算'):
            _wait_for_save_ready(client, {'scene': 'LevelEnter'}, lambda _: None, lambda **_: None, max_frames=2)
        client.call.assert_called_once()
        game = EntryGame([{'scene': 'LevelEnter'}])
        with patch('orchestrator.maintenance.time.monotonic', side_effect=[0, 2]), \
             self.assertRaisesRegex(RuntimeError, '超时'):
            _wait_for_save_ready(game, game.observation(), lambda _: None, lambda **_: None, timeout=1)
        self.assertEqual(game.calls, [])
        client = Mock()
        client.call.return_value = {'obs': {'scene': 'Level', 'level': {'canPause': True}},
                                    'result': {'consumed': 1}}
        with patch('orchestrator.maintenance.time.monotonic', side_effect=[0, 0, 2]), \
             self.assertRaisesRegex(RuntimeError, '超时'):
            _wait_for_save_ready(client, {'scene': 'LevelEnter'}, lambda _: None, lambda **_: None, timeout=1)

    def test_already_paused_save_menu_is_ready_even_when_can_pause_is_false(self):
        game = MenuGame(level=True)
        game.paused = True
        original = game.observation
        def observation():
            value = original()
            if game.level:
                value['level']['canPause'] = False
            return value
        game.observation = observation
        with patch('orchestrator.maintenance.GameProcess', return_value=Process(game)):
            result = save_and_exit(game, save_slot=10, run_dir=None)
        self.assertEqual(result['phase'], 'finished')
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
