"""录像中途失败（独立音频出错、插件失败、OBS 断开）：主控在下一次推进前发现，收尾这一段、在同一运行目录里另起一段；
另起不了就暂停试玩、不推进录不上的帧。插件没合成成片时由主控合成。不连接真实 OBS/游戏。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.recording import (Recorder, RecordOptions, RecordingHalt, analyze_frames, next_segment_dir,
                                    salvage_movie)
from orchestrator.purecut import read_ticks


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
                (directory / 'audio.wav').write_bytes(b'')
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

    def tearDown(self):
        for p in (self.window, self.salvage, self.pure):
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

    def test_failed_segment_is_finished_salvaged_and_a_new_one_started(self):
        recorder = self.recorder()
        self.bridge.advance(5)
        self.bridge.fail()
        reason = recorder.segment_failed()
        self.assertIn('independent audio', reason)
        old = recorder.roll(reason)
        self.assertEqual(old['state'], 'failed')
        self.assertTrue(old.get('salvaged'))
        self.assertEqual(self.salvaged, ['recording'])
        self.assertEqual(recorder.opts.dir.name, 'recording_2')
        self.assertTrue(recorder.started)
        self.assertEqual([a['action'] for c, a in self.bridge.commands if c == 'record'], ['start', 'stop', 'start'])
        # 运行目录指向最新一段；旧段的会话已收尾
        self.assertEqual(Path(Recorder.saved_session(self.run)['options']['dir']).name, 'recording_2')
        self.assertEqual(Recorder.saved_session(self.run)['state'], 'recording')
        self.bridge.advance(4)
        result = recorder.stop()
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(result['frames'], 4)
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

    def test_foreign_directory_is_not_touched(self):
        recorder = self.recorder()
        self.bridge.plugin.update(state='failed', dir=str(self.run / 'other'))
        self.assertIsNone(recorder.segment_failed())

    def test_guard_rolls_and_publishes(self):
        from orchestrator.run import recording_guard
        recorder = self.recorder()
        guard = recording_guard(recorder, self.run)
        guard()                                   # 正常：什么也不做
        self.bridge.advance(2)
        self.bridge.fail()
        guard()
        self.assertEqual(recorder.opts.dir.name, 'recording_2')
        events = (self.run / 'public_events.jsonl').read_text(encoding='utf-8')
        self.assertIn('另起一段', events)

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

    def test_session_turns_halt_into_act_error(self):
        from orchestrator.game_session import ActError, GameSession
        holder = GameSession.__new__(GameSession)
        holder.before_advance = lambda: (_ for _ in ()).throw(RecordingHalt('另起一段录像失败'))
        with self.assertRaises(ActError):
            GameSession._before_advance(holder)
        holder.before_advance = lambda: (_ for _ in ()).throw(RuntimeError('别的检查出错'))
        GameSession._before_advance(holder)        # 其他错误照旧忽略


class SegmentAndCsvTests(unittest.TestCase):
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
