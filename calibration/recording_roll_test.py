"""Retain the production recorder/recovery ledger; replace only bridge and media codecs."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, Mock

from orchestrator.recording import (Recorder, RecordOptions, RecordingHalt, RecordingError, RecordingOwnershipError, analyze_frames, next_segment_dir,
                                    salvage_movie)
from orchestrator.purecut import read_ticks
from orchestrator.purecut import _wav_header


class FakeBridge:
    def __init__(self):
        self.commands = []
        self.fail_start = False
        self.hello = {'record_audio_nrt': True, 'record_audio_tracks': 2, 'record_audio_active': False,
                      'record_audio_frames': 0, 'clock': {'connected': True, 'lockstep': False,
                      'plugin': {'state': 'idle', 'frames': 0, 'audio_nrt': True, 'audio_tracks': 2}}}

    @property
    def plugin(self):
        return self.hello['clock']['plugin']

    def call(self, command, **args):
        self.commands.append((command, args))
        if command == 'record':
            if args['action'] == 'start':
                if self.fail_start:
                    raise RuntimeError('OBS plugin not connected to the clock port')
                directory = Path(args['dir'])
                self.plugin.update(state='recording', dir=args['dir'], frames=0, recording=True, error='')
                self.hello['clock']['lockstep'] = True
                self.hello['record_audio_active'] = True
                self.hello.pop('record_audio_error', None)
                (directory / 'video.mkv').write_bytes(b'')
                (directory / 'frames.csv').write_text('index,game_frame,chapter_ticks,obs_ts_ns\n', encoding='utf-8')
                for name in ('audio.wav', 'audio_play.wav'):
                    (directory / name).write_bytes(_wav_header(0))
            else:
                if self.plugin.get('state') != 'failed':
                    self.plugin['state'] = 'finished'
                self.plugin['recording'] = False
                self.hello['clock']['lockstep'] = False
                self.hello['record_audio_active'] = False
        return copy.deepcopy(self.hello)

    def advance(self, count):
        directory = Path(self.plugin['dir'])
        with (directory / 'video.mkv').open('ab') as video, (directory / 'frames.csv').open('a', encoding='utf-8') as csv:
            for i in range(self.plugin['frames'], self.plugin['frames'] + count):
                video.write(b'f')
                csv.write(f'{i},{100 + i},{i * 166667},{i * 16666667}\n')
        self.plugin['frames'] += count
        for name in ('audio.wav', 'audio_play.wav'):
            path = directory/name
            old = path.read_bytes()[44:]
            data = old + b'\0' * (count * 6400)
            path.write_bytes(_wav_header(len(data)) + data)

    def fail(self, error='independent audio frame missing or failed; refusing to substitute live music'):
        self.plugin.update(state='failed', recording=False, error=error)


class RollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / 'run'
        self.run.mkdir()
        self.bridge = FakeBridge()
        self.window = patch('orchestrator.recording.gamewindow.ensure_visible')
        self.window.start()
        self.salvaged = []

        def fake_salvage(directory, ffmpeg, log=print):
            out = Path(directory) / 'gameplay.mp4'
            out.write_bytes(b'movie')
            self.salvaged.append(Path(directory).name)
            return out
        self.salvage = patch('orchestrator.recording.salvage_movie', side_effect=fake_salvage)
        self.salvage.start()
        self.pure = patch('orchestrator.recording.make_pure', return_value={'state': 'skipped'})
        self.pure.start()
        profile = {'codec_name': 'fixture', 'width': 8, 'height': 8, 'r_frame_rate': '60/1'}
        self.probe = patch('orchestrator.recording_recovery._probe',
                           side_effect=lambda ffmpeg, path: (len(path.read_bytes()), profile))
        def media(ffmpeg, args, log):
            if args[:2] == ['-f', 'concat']:
                listing = Path(args[args.index('-i') + 1]).read_text(encoding='utf-8')
                data = b''.join(Path(line[6:-1]).read_bytes() for line in listing.splitlines() if line.startswith('file '))
            else:
                data = Path(args[args.index('-i') + 1]).read_bytes()
                data = data[:int(args[args.index('-frames:v') + 1])]
            Path(args[-1]).write_bytes(data)
        self.media = patch('orchestrator.recording_recovery._run', side_effect=media)
        self.probe.start(); self.media.start()

    def tearDown(self):
        for p in (self.window, self.salvage, self.pure, self.probe, self.media):
            p.stop()
        self.temp.cleanup()

    def recorder(self):
        recorder = Recorder(self.bridge.call, RecordOptions(self.run / 'recording', 'ffmpeg', '', audio_source='game_nrt'),
                            log=lambda _: None)
        recorder.bind_run(self.run)
        (self.run / 'recording').mkdir()
        recorder.start()
        return recorder

    def test_healthy_recording_is_left_alone(self):
        recorder = self.recorder()
        self.bridge.advance(3)
        self.assertIsNone(recorder.segment_failed())
        self.assertEqual([a['action'] for c, a in self.bridge.commands if c == 'record'], ['start'])

    def test_failed_capture_is_recovered_in_same_session_and_cumulative_files(self):
        recorder = self.recorder()
        self.bridge.advance(5)
        ident = recorder.session_id
        self.bridge.fail()
        reason = recorder.segment_failed()
        self.assertIn('independent audio', reason)
        old = recorder.roll(reason)
        self.assertEqual(old['state'], 'failed')
        self.assertTrue(old.get('salvaged'))
        self.assertEqual(self.salvaged, ['recording'])
        self.assertEqual(recorder.opts.dir.name, 'recording')
        self.assertEqual(recorder.session_id, ident)
        self.assertNotEqual(Path(self.bridge.plugin['dir']), recorder.opts.dir)
        self.assertTrue(recorder.started)
        self.assertEqual([a['action'] for c, a in self.bridge.commands if c == 'record'], ['start', 'stop', 'start'])
        self.assertEqual(Path(Recorder.saved_session(self.run)['options']['dir']).name, 'recording')
        self.assertEqual(Recorder.saved_session(self.run)['state'], 'recording')
        self.bridge.advance(4)
        result = recorder.stop()
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(result['frames'], 9)
        self.assertEqual(result['previous_segments'][0]['frames'], 5)
        self.assertEqual(Path(result['previous_segments'][0]['dir']).name, 'recording')

    def test_bridge_audio_error_counts_as_failure(self):
        recorder = self.recorder()
        self.bridge.hello['record_audio_error'] = 'Recording FMOD (full) update: ERR_FILE_BAD'
        self.assertIn('独立录像音频出错', recorder.segment_failed())

    def test_disconnected_obs_counts_as_failure_and_halts_when_restart_fails(self):
        recorder = self.recorder()
        self.bridge.hello['clock']['connected'] = False
        self.assertIn('断开', recorder.segment_failed())

    def test_restart_failure_raises_halt(self):
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        self.bridge.fail_start = True
        with self.assertRaises(RecordingHalt):
            recorder.roll('x')

    def test_hud_continue_retries_failed_roll_and_keeps_blocking_until_recording(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        self.bridge.fail_start = True
        guard = recording_guard(recorder, self.run)
        with patch('orchestrator.control.RunControl') as control:
            for _ in range(2):
                # The same callback runs again after the HUD resumes the controller.
                with self.assertRaises(RecordingHalt):
                    guard()
            self.assertEqual(control.return_value.enqueue.call_count, 2)
        self.assertIsNotNone(recorder.segment_failed())
        self.assertEqual(len(recorder.segments), 1)
        self.bridge.fail_start = False
        guard()
        self.assertTrue(recorder.started)
        self.assertIsNone(recorder.segment_failed())
        self.bridge.advance(3)
        self.assertEqual(recorder.stop()['frames'], 5)

    def test_failed_roll_ack_loss_is_adopted_on_same_guard_retry(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        calls = self.bridge.call
        def lost(command, **args):
            result = calls(command, **args)
            if command == 'record' and args.get('action') == 'start':
                raise OSError('ack lost after start')
            return result
        recorder.call = lost
        guard = recording_guard(recorder, self.run)
        with patch('orchestrator.control.RunControl'):
            with self.assertRaises(RecordingHalt):
                guard()
        self.bridge.advance(3)
        recorder.call = calls
        starts = [a for c, a in self.bridge.commands if c == 'record' and a['action'] == 'start']
        guard()
        self.assertTrue(recorder.started)
        self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record' and a['action'] == 'start']), len(starts))
        self.assertEqual(recorder.stop()['frames'], 5)

    def test_failed_roll_ack_loss_stop_closes_actual_capture_not_cached_old_result(self):
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        calls = self.bridge.call
        def lost(command, **args):
            result = calls(command, **args)
            if command == 'record' and args.get('action') == 'start':
                raise OSError('ack lost after start')
            return result
        recorder.call = lost
        with self.assertRaises(RecordingHalt):
            recorder.roll('x')
        self.bridge.advance(3)
        recorder.call = calls
        result = recorder.stop()
        self.assertEqual(result['frames'], 5)
        self.assertEqual(self.bridge.plugin['state'], 'finished')
        self.assertFalse(self.bridge.hello['record_audio_active'])
        self.assertFalse(recorder.started)
        self.assertEqual(recorder.stop(), result)

    def test_failed_roll_unknown_start_cannot_be_reported_as_stopped(self):
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        calls = self.bridge.call
        def unknown(command, **args):
            if command == 'record' and args.get('action') == 'start':
                # The asynchronous queue has named the destination, but has not
                # confirmed whether the encoder will start. stop must not turn
                # this idle observation into a terminal recording result.
                self.bridge.plugin.update(dir=args['dir'], state='idle', recording=False)
                raise OSError('request queued; acknowledgement lost')
            return calls(command, **args)
        recorder.call = unknown
        with self.assertRaises(RecordingHalt):
            recorder.roll('x')
        recorder.call = calls
        before = len([a for c, a in self.bridge.commands if c == 'record'])
        with self.assertRaisesRegex(RecordingError, '启动结果尚未确认'):
            recorder.stop()
        self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record']), before)
        self.assertEqual(Recorder.saved_session(self.run)['state'], 'recovering')
        self.assertIsNotNone(recorder.segment_failed())

    def test_definitively_refused_roll_can_stop_and_archive_existing_prefix(self):
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        self.bridge.fail_start = True
        with self.assertRaises(RecordingHalt):
            recorder.roll('x')
        self.assertEqual(Recorder.saved_session(self.run)['recovery_start']['phase'], 'prepared')
        before = len([a for c, a in self.bridge.commands if c == 'record'])
        callback = Mock()
        result = recorder.stop(on_capture_stopped=callback)
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(result['frames'], 2)
        self.assertFalse(result['pending'])
        self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record']), before)
        callback.assert_called_once_with()
        saved = Recorder.saved_session(self.run)
        self.assertEqual(saved['state'], 'finished')
        self.assertNotIn('recovery_start', saved)
        self.assertEqual((recorder.opts.dir/'video.mkv').read_bytes(), b'ff')
        self.assertEqual(recorder.stop(), result)

    def test_refused_roll_stop_does_not_touch_foreign_active_capture(self):
        recorder = self.recorder()
        self.bridge.advance(2)
        self.bridge.fail()
        self.bridge.fail_start = True
        with self.assertRaises(RecordingHalt):
            recorder.roll('x')
        self.bridge.plugin.update(dir=str(self.run/'foreign'), state='recording', recording=True)
        self.bridge.hello['clock']['lockstep'] = True
        before = len([a for c, a in self.bridge.commands if c == 'record'])
        with self.assertRaises(RecordingOwnershipError):
            recorder.stop()
        self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record']), before)
        self.assertIsNotNone(recorder.segment_failed())
        self.assertEqual(Recorder.saved_session(self.run)['state'], 'recovering')

    def test_foreign_directory_is_not_touched(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        self.bridge.plugin.update(state='failed', dir=str(self.run / 'other'))
        before = len([args for command, args in self.bridge.commands if command == 'record'])
        with self.assertRaises(RecordingHalt):
            recording_guard(recorder, self.run)()
        self.assertEqual(len([args for command, args in self.bridge.commands if command == 'record']), before)

    def test_guard_rolls_and_publishes(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        guard = recording_guard(recorder, self.run)
        guard()                                   # 正常：什么也不做
        self.bridge.advance(2)
        self.bridge.fail()
        guard()
        self.assertEqual(recorder.opts.dir.name, 'recording')
        events = (self.run / 'public_events.jsonl').read_text(encoding='utf-8')
        self.assertIn('恢复同一录制会话', events)

    def test_reconnected_idle_obs_blocks_without_stopping_unknown_capture(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        self.bridge.plugin.update(state='idle', recording=False, dir='')
        self.bridge.hello['clock']['lockstep'] = False
        before = len([args for command, args in self.bridge.commands if command == 'record'])
        with self.assertRaisesRegex(RecordingHalt, '--resume'):
            recording_guard(recorder, self.run)()
        self.assertEqual(len([args for command, args in self.bridge.commands if command == 'record']), before)

    def test_active_capture_without_directory_is_not_owned(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        self.bridge.plugin.update(state='recording', recording=True, dir='')
        before = len([args for command, args in self.bridge.commands if command == 'record'])
        with self.assertRaises(RecordingHalt):
            recording_guard(recorder, self.run)()
        self.assertEqual(len([args for command, args in self.bridge.commands if command == 'record']), before)

    def test_guard_pauses_and_raises_when_roll_fails(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        guard = recording_guard(recorder, self.run)
        self.bridge.fail()
        self.bridge.fail_start = True
        with patch('orchestrator.control.RunControl') as control:
            with self.assertRaises(RecordingHalt):
                guard()
            control.return_value.enqueue.assert_called_with('pause')
        self.assertIn('试玩已暂停', (self.run / 'public_events.jsonl').read_text(encoding='utf-8'))

    def test_multiple_stopped_encoder_recoveries_keep_identity_and_global_indices(self):
        recorder = self.recorder()
        ident = recorder.session_id
        self.bridge.advance(5)
        recorder.stop()
        second_run = self.run.parent/'second'
        second_run.mkdir()
        saved = Recorder.saved_session(self.run)
        self.assertEqual(Recorder.resume_mode(saved, self.bridge.hello), 'recover')
        recovered = Recorder.recover(self.bridge.call, saved, second_run, log=lambda _: None)
        self.bridge.advance(4)
        held = recovered.retain()
        self.assertEqual(held['frames'], 9)
        adopted = Recorder.adopt(self.bridge.call, Recorder.saved_session(second_run), second_run, log=lambda _: None)
        self.assertEqual(adopted.stop()['frames'], 9)
        third = Recorder.recover(self.bridge.call, Recorder.saved_session(second_run), second_run, log=lambda _: None)
        self.bridge.advance(2)
        final = third.stop()
        self.assertEqual(final['frames'], 11)
        self.assertEqual(third.session_id, ident)
        self.assertEqual(Recorder.saved_session(self.run)['id'], ident)
        self.assertEqual(Recorder.saved_session(second_run)['id'], ident)
        self.assertEqual(final['check']['repeats'], 0)
        self.assertEqual(len(final['check']['frame_resets']), 2)
        self.assertEqual((third.opts.dir/'video.mkv').read_bytes(), b'f' * 11)
        self.assertEqual(len((third.opts.dir/'audio.wav').read_bytes()), 44 + 11 * 6400)
        self.assertFalse((second_run/'recording').exists())

    def test_recovery_start_ack_loss_is_adopted_without_a_duplicate_start(self):
        recorder = self.recorder()
        self.bridge.advance(3)
        recorder.stop()
        saved = Recorder.saved_session(self.run)
        calls = self.bridge.call
        def lost(command, **args):
            result = calls(command, **args)
            if command == 'record' and args.get('action') == 'start':
                raise OSError('ack lost after start')
            return result
        with self.assertRaisesRegex(RecordingError, '结果尚未确认'):
            Recorder.recover(lost, saved, self.run, log=lambda _: None)
        pending = Recorder.saved_session(self.run)
        self.assertEqual(pending['state'], 'recovering')
        self.assertEqual(pending['recovery_start']['phase'], 'requested')
        before = len([a for c, a in self.bridge.commands if c == 'record' and a['action'] == 'start'])
        adopted = Recorder.recover(calls, pending, self.run, log=lambda _: None)
        self.assertTrue(adopted.started)
        self.assertEqual(Recorder.saved_session(self.run)['state'], 'recording')
        self.assertNotIn('recovery_start', Recorder.saved_session(self.run))
        self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record' and a['action'] == 'start']), before)

    def test_unknown_recovery_start_does_not_resubmit_or_overwrite_the_attempt(self):
        recorder = self.recorder()
        self.bridge.advance(3)
        recorder.stop()
        calls = self.bridge.call
        def missing(command, **args):
            if command == 'record' and args.get('action') == 'start':
                raise OSError('request outcome unknown')
            return calls(command, **args)
        with self.assertRaises(RecordingError):
            Recorder.recover(missing, Recorder.saved_session(self.run), self.run, log=lambda _: None)
        before = list(self.bridge.commands)
        with self.assertRaisesRegex(RecordingError, '未重复 start'):
            Recorder.recover(calls, Recorder.saved_session(self.run), self.run, log=lambda _: None, timeout=0)
        self.assertFalse(any(c == 'record' for c, _ in self.bridge.commands[len(before):]))

    def test_confirmed_bridge_start_refusal_can_retry_same_prepared_capture(self):
        recorder = self.recorder()
        self.bridge.advance(3)
        recorder.stop()
        self.bridge.fail_start = True
        with self.assertRaisesRegex(RecordingError, '未能启动'):
            Recorder.recover(self.bridge.call, Recorder.saved_session(self.run), self.run, log=lambda _: None)
        saved = Recorder.saved_session(self.run)
        capture = Recorder.capture_directory(saved)
        self.assertEqual(saved['recovery_start']['phase'], 'prepared')
        self.bridge.fail_start = False
        recovered = Recorder.recover(self.bridge.call, saved, self.run, log=lambda _: None)
        self.assertEqual(Recorder.capture_directory(Recorder.saved_session(self.run)), capture)
        self.assertTrue(recovered.started)

    def test_main_resume_restores_stopped_encoder_without_new_recording_flag(self):
        from orchestrator import run
        recorder = self.recorder()
        self.bridge.advance(3)
        recorder.stop()
        second = self.run.parent/'second'
        second.mkdir()
        cfg = SimpleNamespace(agent='fixture', create_save=False, bridge_port=48974, mcp_port=48980,
                              hud_mod=False, continuous_music=False, skip_enter=True)
        args = SimpleNamespace(previous={'dir': self.run}, new_recording=False, record=True,
                               no_agent=True, prepared=None, keep_obs_open=True)
        summary = {key: 0 for key in ('decisions', 'deaths', 'elapsed_s', 'thinking_s', 'tool_s', 'play_game_s', 'auto_game_s')}
        summary.update(completed=True, stop_reason='fixture')
        received = []
        async def run_session(*parameters):
            active = parameters[-1]
            received.append(active)
            self.assertIsInstance(active, Recorder)
            self.assertEqual(active.opts.dir.resolve(), recorder.opts.dir.resolve())
            self.assertEqual(active.session_id, recorder.session_id)
            self.assertTrue(active.started)
            return summary
        client = SimpleNamespace(call=self.bridge.call, close=Mock())
        with patch.object(run, 'parse_args', return_value=(cfg, args)), \
             patch.object(run, 'port_free', return_value=True), \
             patch('orchestrator.bridge_requirements.preflight_bridge', return_value=self.bridge.hello), \
             patch.object(run, 'make_run_dir', return_value=second), \
             patch.object(run, 'start_mod_hud', return_value=None), \
             patch.object(run, 'ensure_obs', return_value=False), \
             patch.object(run, 'check_resume_position', return_value=True), \
             patch.object(run, 'run_async', side_effect=run_session), \
             patch('tools.bridge_client.BridgeClient', return_value=client):
            self.assertEqual(run.main(), 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(Recorder.saved_session(second)['id'], recorder.session_id)
        self.assertFalse((second/'recording').exists())

    def test_entry_macro_does_not_restart_an_already_recovered_capture(self):
        from orchestrator import run
        cfg = SimpleNamespace(bridge_port=48974, continuous_music=False, save='test', chapter=3,
                              create_save=False, save_slot=10, cutscene_mode='agent', plan=None)
        recorder = self.recorder()
        client = SimpleNamespace(call=self.bridge.call, close=Mock())
        obs = {'scene': 'Level', 'save': {'slot': 10}, 'level': {'sid': 'Celeste/3-CelestialResort', 'room': 'a'}}
        before = [a for c, a in self.bridge.commands if c == 'record']
        with patch('tools.bridge_client.BridgeClient', return_value=client), \
             patch('tools.enter_save.to_file_select') as backwards, \
             patch('tools.enter_save.enter_save', return_value=obs) as macro:
            run.enter_save(cfg, recorder)
        backwards.assert_not_called()
        self.assertEqual([a for c, a in self.bridge.commands if c == 'record'], before)
        self.assertEqual(macro.call_args.kwargs['menu_speed'], 1)

    def test_foreign_active_capture_blocks_recovery_before_files_or_commands_change(self):
        recorder = self.recorder()
        self.bridge.advance(3)
        recorder.stop()
        saved = Recorder.saved_session(self.run)
        prefix = (recorder.opts.dir/'video.mkv').read_bytes()
        self.bridge.plugin.update(state='recording', recording=True, dir=str(self.run/'foreign'))
        self.bridge.hello['clock']['lockstep'] = True
        before = list(self.bridge.commands)
        with self.assertRaises(RecordingOwnershipError):
            Recorder.recover(self.bridge.call, saved, self.run, log=lambda _: None)
        self.assertFalse((recorder.opts.dir/'.recovery').exists())
        self.assertEqual((recorder.opts.dir/'video.mkv').read_bytes(), prefix)
        self.assertFalse(any(c == 'record' for c, _ in self.bridge.commands[len(before):]))

    def test_cumulative_publish_failure_is_retryable_without_restarting_capture(self):
        from orchestrator.recording_recovery import RecoveryError
        recorder = self.recorder()
        self.bridge.advance(3)
        recorder.stop()
        recovered = Recorder.recover(self.bridge.call, Recorder.saved_session(self.run), self.run, log=lambda _: None)
        self.bridge.advance(2)
        # Fail only the external media process, preserving production stop,
        # normalization, ledger and publication behavior.
        with patch('orchestrator.recording_recovery._run', side_effect=RecoveryError('codec unavailable')):
            with self.assertRaisesRegex(RecordingError, '发布未完成'):
                recovered.stop()
        self.assertIsNone(recovered.result)
        self.assertEqual(Recorder.saved_session(self.run)['state'], 'finishing')
        stops = len([a for c, a in self.bridge.commands if c == 'record' and a['action'] == 'stop'])
        self.assertEqual(recovered.stop()['frames'], 5)
        self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record' and a['action'] == 'stop']), stops)

    def test_session_turns_halt_into_act_error(self):
        from orchestrator.game_session import ActError, GameSession
        holder = GameSession.__new__(GameSession)
        holder.before_advance = lambda: (_ for _ in ()).throw(RecordingHalt('另起一段录像失败'))
        with self.assertRaises(ActError):
            GameSession._before_advance(holder)
        holder.before_advance = lambda: (_ for _ in ()).throw(RuntimeError('别的检查出错'))
        GameSession._before_advance(holder)        # 其他错误照旧忽略

    def test_corrupt_or_missing_ledger_blocks_real_session_before_any_step(self):
        from orchestrator.game_session import ActError, GameSession
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        self.bridge.advance(2)
        recorder.stop()
        recorder = Recorder.recover(self.bridge.call, Recorder.saved_session(self.run), self.run, log=lambda _: None)
        ledger = recorder.opts.dir/'.recovery/ledger.json'
        original = ledger.read_bytes()
        session = GameSession.__new__(GameSession)
        session.before_advance = recording_guard(recorder, self.run)
        session.playback = None
        session.metrics = Mock()
        session.client = Mock()
        session.cfg = SimpleNamespace(formal_thought=False)
        session.run_dir, session.decision_index = self.run, 0
        plan = SimpleNamespace(expect=None, input_format='celeste_ai')
        before = len([a for c, a in self.bridge.commands if c == 'record'])
        for damage in ('corrupt', 'missing'):
            with self.subTest(damage=damage):
                ledger.write_bytes(original)
                if damage == 'corrupt':
                    ledger.write_text('{broken', encoding='utf-8')
                else:
                    ledger.unlink()
                with patch('orchestrator.control.RunControl') as control:
                    with self.assertRaisesRegex(ActError, '这次按键没有执行'):
                        session._execute(plan, ['R'], 'check', [], None, 0)
                    control.return_value.enqueue.assert_called_once_with('pause')
                self.assertEqual(session.client.mock_calls, [])
                self.assertEqual(len([a for c, a in self.bridge.commands if c == 'record']), before)

    def test_guard_hello_failure_halts_but_ordinary_window_error_keeps_callback_semantics(self):
        from orchestrator.game_session import ActError, GameSession
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        session = GameSession.__new__(GameSession)
        session.before_advance = recording_guard(recorder, self.run)
        recorder.call = Mock(side_effect=OSError('bridge disconnected'))
        with patch('orchestrator.control.RunControl') as control:
            with self.assertRaises(ActError):
                session._before_advance()
            control.return_value.enqueue.assert_called_once_with('pause')
        with patch.object(recorder, 'check_window', side_effect=RuntimeError('window unavailable')):
            session._before_advance()


class SegmentAndCsvTests(unittest.TestCase):
    def test_recovery_boundary_before_replay_rows_still_marks_next_game_frame_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'frames.csv'
            path.write_text('index,game_frame,chapter_ticks,obs_ts_ns\n'
                            '0,100,100,1\n1,101,101,2\n2,0,-1,3\n3,0,-2,4\n4,0,0,5\n5,1,1,6\n', encoding='utf-8')
            result = analyze_frames(path, [{'index': 2}])
            self.assertEqual(result['frame_resets'], [{'index': 4, 'from': 101, 'to': 0}])
            self.assertEqual(result['missing_frames'], 0)
            self.assertEqual(result['repeats'], 0)
            self.assertEqual(result['replay_frames'], 2)

    def test_next_segment_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'recording'
            self.assertEqual(next_segment_dir(base).name, 'recording_2')
            (Path(folder) / 'recording_2').mkdir()
            (Path(folder) / 'recording_2' / 'session.json').write_text('{}', encoding='utf-8')
            self.assertEqual(next_segment_dir(base).name, 'recording_3')
            self.assertEqual(next_segment_dir(Path(folder) / 'recording_3').name, 'recording_4')

    def test_half_written_last_row_does_not_break_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'frames.csv'
            path.write_text('index,game_frame,chapter_ticks,obs_ts_ns\n0,10,0,1\n1,11,166667,2\n2,12\n', encoding='utf-8')
            check = analyze_frames(path)
            self.assertEqual(check['rows'], 2)
            self.assertEqual(check['missing_frames'], 0)
            self.assertEqual(check['malformed_rows'], 1)
            self.assertEqual(read_ticks(path), [0, 166667])

    def test_salvage_needs_video_and_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(salvage_movie(Path(folder), 'ffmpeg', log=lambda _: None))

    def test_salvage_runs_ffmpeg_mux_and_keeps_partial_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'video.mkv').write_bytes(b'v')
            (directory / 'audio.wav').write_bytes(b'a')
            seen = []

            def run(cmd, **kw):
                seen.append(cmd)
                Path(cmd[-1]).write_bytes(b'mp4')
                return type('R', (), {'returncode': 0})()
            with patch('orchestrator.recording.subprocess.run', side_effect=run):
                out = salvage_movie(directory, 'ffmpeg', log=lambda _: None)
            self.assertEqual(out.name, 'gameplay.mp4')
            self.assertTrue(out.is_file())
            self.assertIn('-c:v', seen[0])
            self.assertFalse((directory / 'gameplay.salvage.mp4').exists())
            out.rename(directory / 'kept.mp4')

            def bad(cmd, **kw):
                return type('R', (), {'returncode': 1})()
            with patch('orchestrator.recording.subprocess.run', side_effect=bad):
                self.assertIsNone(salvage_movie(directory, 'ffmpeg', log=lambda _: None))
            self.assertFalse((directory / 'gameplay.mp4').exists())




class GuardInsideSessionLockTests(unittest.TestCase):
    """推进前检查在 GameSession 持锁时运行（observe/act 里），检查录像状态要向桥接发指令：锁必须可重入，否则自锁
    （10-27 5A 第二代开局 observe 卡死，中断时主线程的保留录制也一起卡住）。"""

    def test_bridge_call_while_holding_the_session_lock(self):
        import threading
        from orchestrator.config import RunConfig
        from orchestrator.game_session import GameSession
        from orchestrator.metrics import Metrics
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            session = GameSession(RunConfig(agent='t', save='s', model='opus'), run, Metrics(run / 'metrics.json', 't', 'opus', 'high', 60))
            session.client = type('C', (), {'call': lambda self, cmd, **a: {'clock': {}}})()
            done = threading.Event()

            def run_guarded():
                with session.lock:                      # observe / act 持锁时调用推进前检查
                    session.bridge_call('hello')        # 检查录像状态
                done.set()
            threading.Thread(target=run_guarded, daemon=True).start()
            self.assertTrue(done.wait(2), 'bridge_call deadlocked while the session lock was held')
            session.client = None
            session.close(release=False)


if __name__ == '__main__':
    unittest.main()
