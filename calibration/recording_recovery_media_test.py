"""Real media recovery checks; no OBS, game, live controller or user config."""
import array
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.recording import analyze_frames, find_ffmpeg
from orchestrator.purecut import _wav_data, _wav_header, read_ticks


class RecordingRecoveryMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = find_ffmpeg()
        except RuntimeError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        from orchestrator.recording_recovery import ffprobe_path, RecoveryError
        try:
            cls.ffprobe = ffprobe_path(cls.ffmpeg)      # beside ffmpeg, else on PATH (same rule as recovery)
        except RecoveryError as exc:
            raise unittest.SkipTest('ffprobe is unavailable') from exc

    def setUp(self):
        from orchestrator import recording_recovery
        self.recovery = recording_recovery
        self.temp = tempfile.TemporaryDirectory(prefix='celeste-recovery-media-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.root = self.work/'recording'
        self.root.mkdir()
        self.saved = {
            'version': 1, 'id': '1234567890abcdef1234567890abcdef', 'state': 'failed',
            'options': {'dir': str(self.root), 'ffmpeg': self.ffmpeg,
                        'video_args': '-c:v libx264 -preset ultrafast -g 1 -bf 0',
                        'audio_source': 'game_nrt', 'audio_offset_ms': 0,
                        'marker': False, 'continuous_music': False},
            'runs': [str(self.work)],
        }
        (self.root/'session.json').write_text(json.dumps(self.saved), encoding='utf-8')

    def command(self, args):
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result.stdout

    def media(self, directory, ticks, *, start_frame=10, color='red', codec='libx264', amplitude=.125):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        codec_args = ['-c:v', codec]
        if codec == 'libx264':
            codec_args += ['-preset', 'ultrafast', '-g', '1', '-bf', '0']
        self.command([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                      '-f', 'lavfi', '-i', f'color=c={color}:s=64x48:r=60',
                      '-frames:v', str(len(ticks)), *codec_args, '-pix_fmt', 'yuv420p',
                      str(directory/'video.mkv')])
        full = array.array('f', [amplitude, -amplitude]*800*len(ticks)).tobytes()
        play = array.array('f', [amplitude, -amplitude]*800*sum(t >= 0 for t in ticks)).tobytes()
        (directory/'audio.wav').write_bytes(_wav_header(len(full)) + full)
        (directory/'audio_play.wav').write_bytes(_wav_header(len(play)) + play)
        rows = []
        game_frame = start_frame
        for index, tick in enumerate(ticks):
            if tick >= 0:
                game_frame += 1
            rows.append({'index': index, 'game_frame': game_frame,
                         'chapter_ticks': tick, 'obs_ts_ns': (index + 1)*16666667})
        with (directory/'frames.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['index', 'game_frame', 'chapter_ticks', 'obs_ts_ns'])
            writer.writeheader()
            writer.writerows(rows)
        (directory/'segments.csv').write_text(
            'segment,first_index,start_ts_ns,measured_ms,offset_ms,method\n'
            '0,0,16666667,0,0,fixed\n', encoding='utf-8')
        return {'full': full, 'play': play, 'ticks': ticks, 'frames': len(ticks),
                'raw': self.decode(directory/'video.mkv')}

    def decode(self, path):
        return self.command([self.ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:v:0',
                             '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])

    def frames(self, path):
        result = json.loads(self.command([self.ffprobe, '-v', 'error', '-count_frames',
                                         '-select_streams', 'v:0', '-show_entries',
                                         'stream=nb_read_frames', '-of', 'json', str(path)]))
        return int(result['streams'][0]['nb_read_frames'])

    def wav(self, name):
        path = self.root/name
        offset, size = _wav_data(path)
        with path.open('rb') as stream:
            stream.seek(offset)
            return stream.read(size)

    def fingerprints(self):
        return {name: hashlib.sha256((self.root/name).read_bytes()).hexdigest()
                for name in ('video.mkv', 'audio.wav', 'audio_play.wav', 'frames.csv', 'segments.csv')}

    def assert_backup_kept(self, fingerprints):
        hashes = {hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (self.root/'.recovery').rglob('*') if path.is_file()}
        self.assertTrue(set(fingerprints.values()).issubset(hashes), 'Recovery discarded original source bytes')

    def test_no_ledger_finalize_is_a_no_op(self):
        self.media(self.root, [100, 200])
        before = self.fingerprints()
        self.assertIsNone(self.recovery.finalize(self.saved))
        self.assertEqual(self.fingerprints(), before)
        self.assertEqual(Path(self.recovery.capture_directory(self.saved)), self.root)

    def test_chapter_loader_recovery_infers_context_from_validated_prefix(self):
        from orchestrator.recording import Recorder
        import copy
        self.media(self.root,[100,200,300])
        self.saved['options'].update(chapter_only=True,chapter_context=False)
        hello={'record_audio_nrt':True,'record_audio_tracks':2,'record_audio_active':False,
               'clock':{'connected':True,'lockstep':False,'plugin':{'state':'finished','frames':3,
                        'dir':str(self.root),'audio_nrt':True,'audio_tracks':2}}}
        commands=[]
        def call(cmd,**args):
            commands.append((cmd,args))
            if cmd=='record' and args['action']=='start':
                hello['record_audio_active']=True;hello['clock']['lockstep']=True
                hello['clock']['plugin'].update(state='recording',recording=True,dir=args['dir'],frames=0)
            return copy.deepcopy(hello)
        with patch('orchestrator.recording.gamewindow.ensure_visible'):
            recorder=Recorder.recover(call,self.saved,self.work,lambda _:None)
        self.assertTrue(recorder.opts.chapter_context)
        start=next(args for cmd,args in commands if cmd=='record' and args['action']=='start')
        self.assertTrue(start['chapter_context'])

    def test_clean_encoder_receipt_skips_decode_and_keeps_exact_media(self):
        base=self.media(self.root,[100,-1,200,300])
        self.recovery.record_closed_capture(self.saved,self.root,{'state':'finished','frames':4})
        with patch.object(self.recovery,'_probe',wraps=self.recovery._probe) as probe:
            prepared=self.recovery.prepare(self.saved)
        self.assertTrue(probe.call_args_list)
        self.assertTrue(all(call.kwargs.get('decode') is False for call in probe.call_args_list))
        self.assertEqual(prepared['prefix_frames'],4)
        tail=self.media(prepared['active_capture'],[400,500],color='blue')
        final=self.recovery.finalize(self.saved)
        self.assertEqual(final['frames'],6)
        self.assertEqual(self.decode(self.root/'video.mkv'),base['raw']+tail['raw'])
        self.assertEqual(self.wav('audio.wav'),base['full']+tail['full'])
        self.assertEqual(self.wav('audio_play.wav'),base['play']+tail['play'])

    def test_foreign_receipt_falls_back_to_full_decode(self):
        self.media(self.root,[100,200,300])
        self.recovery.record_closed_capture(self.saved,self.root,{'state':'finished','frames':3})
        path=self.root/self.recovery.CLOSED_RECEIPT
        receipt=json.loads(path.read_text());receipt['session_id']='other'
        path.write_text(json.dumps(receipt))
        with patch.object(self.recovery,'_probe',wraps=self.recovery._probe) as probe:
            prepared=self.recovery.prepare(self.saved)
        self.assertEqual(prepared['prefix_frames'],3)
        self.assertTrue(any(call.kwargs.get('decode',True) for call in probe.call_args_list))

    def test_changed_media_invalidates_clean_receipt(self):
        self.media(self.root,[100,200])
        self.recovery.record_closed_capture(self.saved,self.root,{'state':'finished','frames':2})
        with (self.root/'segments.csv').open('a') as stream:stream.write('\n')
        with patch.object(self.recovery,'_probe',wraps=self.recovery._probe) as probe:
            self.recovery.prepare(self.saved)
        self.assertTrue(any(call.kwargs.get('decode',True) for call in probe.call_args_list))

    def test_receipt_does_not_hide_missing_video_packets(self):
        self.media(self.root,[100,200,300])
        # Encoder claims three frames, but the actual video only contains two.
        video=self.root/'video.mkv'
        self.command([self.ffmpeg,'-v','error','-y','-f','lavfi','-i','color=c=red:s=64x48:r=60',
                      '-frames:v','2','-c:v','libx264','-g','1','-bf','0',str(video)])
        self.recovery.record_closed_capture(self.saved,self.root,{'state':'finished','frames':3})
        with patch.object(self.recovery,'_probe',wraps=self.recovery._probe) as probe:
            prepared=self.recovery.prepare(self.saved)
        self.assertEqual(prepared['prefix_frames'],2)
        self.assertTrue(any(call.kwargs.get('decode',True) for call in probe.call_args_list))

    def test_two_recoveries_keep_identity_and_publish_complete_dual_track_media(self):
        base = self.media(self.root, [100, -1, 200, 300])
        original = self.fingerprints()
        first = self.recovery.prepare(self.saved)
        self.assertEqual(first['prefix_frames'], 4)
        self.assertEqual(self.fingerprints(), original)
        first_path = Path(first['active_capture'])
        self.assertNotEqual(first_path, self.root)
        self.assertTrue(first_path.is_relative_to(self.root/'.recovery'))
        self.assertEqual(Path(self.recovery.capture_directory(self.saved)), first_path)
        tail1 = self.media(first_path, [400, -1, 500], start_frame=13, color='blue', amplitude=.25)
        merged = self.recovery.finalize(self.saved)
        self.assertEqual((merged['frames'], merged['replays']), (7, 2))
        second = self.recovery.prepare(self.saved)
        self.assertEqual(second['prefix_frames'], 7)
        self.assertNotEqual(second['active_capture'], first['active_capture'])
        tail2 = self.media(second['active_capture'], [600, 700], start_frame=15, color='green', amplitude=.375)
        final = self.recovery.finalize(self.saved)
        self.assertEqual((final['frames'], final['replays']), (9, 2))
        self.assertTrue(final['recovery_history'])
        self.assertTrue(final['boundaries'])
        self.assertEqual(self.frames(self.root/'video.mkv'), 9)
        self.assertEqual(self.decode(self.root/'video.mkv'), base['raw'] + tail1['raw'] + tail2['raw'])
        self.assertEqual(self.wav('audio.wav'), base['full'] + tail1['full'] + tail2['full'])
        self.assertEqual(self.wav('audio_play.wav'), base['play'] + tail1['play'] + tail2['play'])
        self.assertEqual(read_ticks(self.root/'frames.csv'), base['ticks'] + tail1['ticks'] + tail2['ticks'])
        with (self.root/'frames.csv').open(newline='', encoding='utf-8') as stream:
            self.assertEqual([int(row['index']) for row in csv.DictReader(stream)], list(range(9)))
        self.assertEqual(json.loads((self.root/'session.json').read_text())['id'], self.saved['id'])
        self.assert_backup_kept(original)

    def test_partial_publication_retry_never_appends_a_chunk_twice(self):
        self.media(self.root, [100, 200])
        original = self.fingerprints()
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [300, 400], start_frame=12, color='blue')
        replace = self.recovery.os.replace
        failed = False
        def locked_audio(source, destination):
            nonlocal failed
            if Path(destination) == self.root/'audio.wav' and not failed:
                failed = True
                raise PermissionError('Injected audio publication failure')
            return replace(source, destination)
        with patch.object(self.recovery.os, 'replace', side_effect=locked_audio):
            with self.assertRaises((PermissionError, RuntimeError)):
                self.recovery.finalize(self.saved)
        self.assertTrue(failed)
        result = self.recovery.finalize(self.saved)
        self.assertEqual(result['frames'], 4)
        self.assertEqual(self.recovery.finalize(self.saved)['frames'], 4)
        self.assertEqual(self.frames(self.root/'video.mkv'), 4)
        self.assertEqual(len(read_ticks(self.root/'frames.csv')), 4)
        self.assertEqual(len(self.wav('audio.wav')), 4*6400)
        self.assert_backup_kept(original)

    def test_codec_mismatch_rejects_candidate_and_preserves_published_prefix(self):
        self.media(self.root, [100, 200])
        prepared = self.recovery.prepare(self.saved)
        before = self.fingerprints()
        self.media(prepared['active_capture'], [300, 400], color='blue', codec='mpeg4')
        with self.assertRaises(RuntimeError):
            self.recovery.finalize(self.saved)
        self.assertEqual(self.fingerprints(), before)

    def test_invalid_ledger_or_wrong_session_does_not_fall_back_to_root(self):
        self.media(self.root, [100, 200])
        self.recovery.prepare(self.saved)
        original = self.fingerprints()
        with self.assertRaises(RuntimeError):
            self.recovery.capture_directory({**self.saved, 'id': 'another-session'})
        ledger = self.root/'.recovery/ledger.json'
        ledger.write_text('{invalid', encoding='utf-8')
        for method in (self.recovery.capture_directory, self.recovery.prepare, self.recovery.finalize):
            with self.subTest(method=method.__name__), self.assertRaises(RuntimeError):
                method(self.saved)
        self.assertEqual(self.fingerprints(), original)

    def test_ledger_capture_path_cannot_escape_recovery_directory(self):
        self.media(self.root, [100, 200])
        self.recovery.prepare(self.saved)
        ledger = self.root/'.recovery/ledger.json'
        original = self.fingerprints()
        good = json.loads(ledger.read_text(encoding='utf-8'))
        for escape in ('../../outside-capture', str(self.work/'outside-capture')):
            with self.subTest(path=escape):
                bad = {**good, 'chunks': [*good['chunks'], escape], 'active_capture': escape}
                ledger.write_text(json.dumps(bad), encoding='utf-8')
                for method in (self.recovery.capture_directory, self.recovery.prepare, self.recovery.finalize):
                    with self.assertRaises(RuntimeError):
                        method(self.saved)
        self.assertEqual(self.fingerprints(), original)

    def test_tail_recovery_uses_common_prefix_and_never_modifies_damaged_originals(self):
        self.media(self.root, [100, -1, 200, 300, 400])
        for name in ('audio.wav', 'audio_play.wav'):
            path = self.root/name
            path.write_bytes(path.read_bytes()[:-13])
        with (self.root/'frames.csv').open('a', encoding='utf-8') as stream:
            stream.write('5,15,')
        original = self.fingerprints()
        prepared = self.recovery.prepare(self.saved)
        self.assertEqual(prepared['prefix_frames'], 4)
        self.assertEqual(self.fingerprints(), original)
        self.media(prepared['active_capture'], [500, 600], start_frame=13, color='blue')
        result = self.recovery.finalize(self.saved)
        self.assertEqual((result['frames'], result['replays']), (6, 1))
        self.assertTrue(result['recovery_history'])
        base_report = next(report for report in result['recovery_history'] if report['source'] == 'base')
        self.assertEqual(base_report['unused_video_frames'], 1)
        self.assertEqual(base_report['unused_full_pcm_bytes'], 6400 - 13)
        self.assertEqual(base_report['unused_play_pcm_bytes'], 6400 - 13)
        self.assertEqual(base_report['unusable_csv_tail_rows'], 1)
        self.assertEqual(base_report['unusable_csv_tail_bytes'], len(b'5,15,'))
        self.assertEqual(self.frames(self.root/'video.mkv'), 6)
        self.assertEqual(len(self.wav('audio.wav')), 6*6400)
        self.assertEqual(len(self.wav('audio_play.wav')), 5*6400)
        self.assertEqual(read_ticks(self.root/'frames.csv'), [100, -1, 200, 300, 500, 600])
        self.assert_backup_kept(original)

    def test_truncated_numeric_csv_tail_is_not_a_complete_frame(self):
        self.media(self.root, [100, 200, 300])
        csv_path = self.root/'frames.csv'
        raw = csv_path.read_bytes()
        # Every plugin row ends in a newline. Cutting numeric timestamp bytes
        # can still parse as int, but does not form a committed row.
        csv_path.write_bytes(raw[:-4])
        original = self.fingerprints()
        prepared = self.recovery.prepare(self.saved)
        self.assertEqual(prepared['prefix_frames'], 2)
        self.assertEqual(self.fingerprints(), original)
        self.assert_backup_kept(original)

    def test_empty_capture_attempt_does_not_erase_or_duplicate_prefix(self):
        self.media(self.root, [100, -1, 200])
        prepared = self.recovery.prepare(self.saved)
        attempt = Path(prepared['active_capture'])
        (attempt/'frames.csv').write_text('index,game_frame,chapter_ticks,obs_ts_ns\n', encoding='utf-8')
        (attempt/'audio.wav').write_bytes(_wav_header(0))
        (attempt/'audio_play.wav').write_bytes(_wav_header(0))
        result = self.recovery.finalize(self.saved)
        self.assertEqual((result['frames'], result['replays']), (3, 1))
        self.assertEqual(self.frames(self.root/'video.mkv'), 3)
        again = self.recovery.prepare(self.saved)
        self.assertEqual(again['prefix_frames'], 3)
        self.assertNotEqual(again['active_capture'], prepared['active_capture'])

    def test_zero_video_attempt_keeps_old_prefix_and_reports_unrecorded_tail(self):
        self.media(self.root, [100, -1, 200])
        original = self.fingerprints()
        prepared = self.recovery.prepare(self.saved)
        failed_capture = Path(prepared['active_capture'])
        self.media(failed_capture, [300, 400], color='blue')
        (failed_capture/'video.mkv').write_bytes(b'')
        source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in failed_capture.iterdir() if path.is_file()}
        result = self.recovery.finalize(self.saved)
        self.assertEqual((result['frames'], result['replays']), (3, 1))
        skipped = next(report for report in result['recovery_history'] if report['source'] == prepared['attempt_id'])
        self.assertEqual(skipped['unused_csv_rows'], 2)
        self.assertEqual(skipped['available_video_frames'], 0)
        self.assertTrue(skipped['reason'])
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in failed_capture.iterdir() if path.is_file()}, source_hashes)
        self.assert_backup_kept(original)
        retry = self.recovery.prepare(self.saved)
        self.media(retry['active_capture'], [500, 600], color='green')
        final = self.recovery.finalize(self.saved)
        self.assertEqual((final['frames'], final['replays']), (5, 1))
        self.assertEqual(self.frames(self.root/'video.mkv'), 5)
        self.assertEqual(json.loads((self.root/'session.json').read_text())['id'], self.saved['id'])

    def test_nonempty_unparseable_video_is_not_silently_skipped(self):
        self.media(self.root, [100, 200])
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [300, 400], color='blue')
        (Path(prepared['active_capture'])/'video.mkv').write_bytes(b'nonempty damaged Matroska bytes')
        current = self.fingerprints()
        with self.assertRaises(RuntimeError):
            self.recovery.finalize(self.saved)
        self.assertEqual(self.fingerprints(), current)

    def test_game_frame_reset_after_leading_replays_remains_visible(self):
        self.media(self.root, [100, 200], start_frame=100)
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [-1, -1, 300], start_frame=0, color='blue')
        result = self.recovery.finalize(self.saved)
        check = analyze_frames(self.root/'frames.csv', boundaries=result['boundaries'])
        self.assertEqual(check['missing_frames'], 0)
        self.assertEqual(check['repeats'], 0)
        self.assertEqual(check['frame_resets'], [{'index': 4, 'from': 102, 'to': 1}])

    def test_real_positive_gap_after_leading_replays_is_not_hidden(self):
        self.media(self.root, [100, 200], start_frame=10)
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [-1, -1, 300], start_frame=20, color='blue')
        result = self.recovery.finalize(self.saved)
        check = analyze_frames(self.root/'frames.csv', boundaries=result['boundaries'])
        self.assertEqual(check['missing_frames'], 8)
        self.assertFalse(check.get('frame_resets'))

    def test_publish_journal_cannot_write_a_filename_outside_recording(self):
        self.media(self.root, [100, 200])
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [300], color='blue')
        replace = self.recovery.os.replace
        def interrupt_publish(source, destination):
            if Path(destination) == self.root/'audio.wav':
                raise PermissionError('leave a publication journal')
            return replace(source, destination)
        with patch.object(self.recovery.os, 'replace', side_effect=interrupt_publish):
            with self.assertRaises((PermissionError, RuntimeError)):
                self.recovery.finalize(self.saved)
        ledger_path = self.root/'.recovery/ledger.json'
        ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
        outside = self.work/'victim.txt'
        outside.write_bytes(b'untouched personal file')
        malicious = self.root/'.recovery/victim.txt'
        malicious.write_bytes(b'file copied through a corrupt journal')
        ledger['transaction']['files']['../victim.txt'] = hashlib.sha256(malicious.read_bytes()).hexdigest()
        ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.recovery.finalize(self.saved)
        self.assertEqual(outside.read_bytes(), b'untouched personal file')

    def test_orphan_base_cannot_hide_later_growth_of_original_capture(self):
        self.media(self.root, [100, 200])
        write = self.recovery._write
        def lose_initial_ledger(path, value):
            if Path(path) == self.root/'.recovery/ledger.json':
                raise PermissionError('Initial recovery ledger could not be saved')
            return write(path, value)
        with patch.object(self.recovery, '_write', side_effect=lose_initial_ledger):
            with self.assertRaises((PermissionError, RuntimeError)):
                self.recovery.prepare(self.saved)
        self.assertTrue((self.root/'.recovery/base').is_dir())
        self.assertFalse((self.root/'.recovery/ledger.json').exists())
        # The original live recorder can be adopted after that failed prepare.
        # Its later bytes must not be replaced with the older orphan snapshot.
        self.media(self.root, [100, 200, 300], color='blue')
        current = self.fingerprints()
        with self.assertRaises(RuntimeError):
            self.recovery.prepare(self.saved)
        self.assertEqual(self.fingerprints(), current)

    def test_existing_recovery_lock_rejects_concurrent_prepare(self):
        self.media(self.root, [100, 200])
        original = self.fingerprints()
        directory = self.root/'.recovery'
        directory.mkdir()
        lock = directory/'recovery.lock'
        with lock.open('x', encoding='utf-8') as stream:
            stream.write('0')
            stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(RuntimeError):
                    self.recovery.prepare(self.saved)
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
        self.assertEqual(self.fingerprints(), original)

    @unittest.skipUnless(os.name == 'nt', 'Uses actual Windows sharing protection')
    def test_source_still_open_for_writing_is_not_recovered(self):
        self.media(self.root, [100, 200])
        original = self.fingerprints()
        with (self.root/'video.mkv').open('r+b'):
            with self.assertRaises(RuntimeError):
                self.recovery.prepare(self.saved)
        self.assertEqual(self.fingerprints(), original)
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [300, 400], color='blue')
        with (Path(prepared['active_capture'])/'video.mkv').open('r+b'):
            with self.assertRaises(RuntimeError):
                self.recovery.finalize(self.saved)
        self.assertEqual(self.fingerprints(), original)
        self.assertEqual(self.recovery.finalize(self.saved)['frames'], 4)

    def test_committed_media_is_repaired_from_same_generation_without_reappending(self):
        self.media(self.root, [100, -1, 200])
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [300, 400], start_frame=12, color='blue')
        result = self.recovery.finalize(self.saved)
        original = self.fingerprints()
        (self.root/'audio.wav').write_bytes(b'broken')
        repaired = self.recovery.finalize(self.saved)
        self.assertEqual(repaired['generation'], result['generation'])
        self.assertEqual(repaired['frames'], 5)
        self.assertEqual(self.fingerprints(), original)
        movie = self.root/'gameplay.mp4'
        movie.write_bytes(b'already generated')
        self.recovery.finalize(self.saved)
        self.assertEqual(movie.read_bytes(), b'already generated')

    def test_committed_media_with_missing_candidate_refuses_without_touching_other_files(self):
        self.media(self.root, [100, 200])
        prepared = self.recovery.prepare(self.saved)
        self.media(prepared['active_capture'], [300], start_frame=12, color='blue')
        result = self.recovery.finalize(self.saved)
        (self.root/'audio.wav').write_bytes(b'broken')
        (self.root/'.recovery'/result['generation']/'audio.wav').unlink()
        before = self.fingerprints()
        with self.assertRaises(RuntimeError):
            self.recovery.finalize(self.saved)
        self.assertEqual(self.fingerprints(), before)

    # Production encoding: x264 veryfast reorders B-frames, so a crash leaves
    # presentation holes at the tail of the decode-order packet stream.
    PRODUCTION = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-g', '120']

    def numbered(self, directory, frames, chunk, truncate=None):
        """Every frame shows (chunk, number) as luma blocks; PCM and CSV rows are per-frame distinct."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        pattern = (f"geq=lum='16+20*if(lt(X,W/3),{chunk},if(lt(X,2*W/3),trunc(N/10),mod(N,10)))':cb=128:cr=128")
        self.command([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                      '-i', 'color=c=black:s=192x64:r=60', '-vf', pattern, '-frames:v', str(frames),
                      *self.PRODUCTION, '-pix_fmt', 'yuv420p', str(directory/'video.mkv')])
        if truncate is not None:
            data = (directory/'video.mkv').read_bytes()
            (directory/'video.mkv').write_bytes(data[:int(len(data)*truncate)])
        blocks = [array.array('f', [chunk + i/1000, -(chunk + i/1000)]*800).tobytes() for i in range(frames)]
        for name in ('audio.wav', 'audio_play.wav'):
            (directory/name).write_bytes(_wav_header(len(blocks)*6400) + b''.join(blocks))
        with (directory/'frames.csv').open('w', newline='', encoding='utf-8') as stream:
            stream.write('index,game_frame,chapter_ticks,obs_ts_ns\n')
            for i in range(frames):
                stream.write(f'{i},{chunk*1000 + i},{i},{(i + 1)*16666667}\n')
        (directory/'segments.csv').write_text('segment,first_index,start_ts_ns,measured_ms,offset_ms,method\n'
                                              '0,0,16666667,0,0,fixed\n', encoding='utf-8')
        return blocks

    def identities(self, path):
        raw = self.command([self.ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:v:0', '-fps_mode', 'passthrough',
                            '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'])
        size, result = 192*64*3//2, []
        for start in range(0, len(raw) - size + 1, size):
            frame = raw[start:start + size]
            def digit(x0):
                values = [frame[y*192 + x] for y in range(16, 48) for x in range(x0, x0 + 32)]
                return round((sum(values)/len(values) - 16)/20)
            result.append((digit(16), digit(80)*10 + digit(144)))
        return result

    def test_bframe_crash_prefix_is_frame_exact_and_splices_cleanly(self):
        for truncate in (.55, .7, .8, .9):
            with self.subTest(truncate=truncate):
                shutil.rmtree(self.root/'.recovery', ignore_errors=True)
                old = self.numbered(self.root, 100, 1, truncate)
                prepared = self.recovery.prepare(self.saved)
                count = prepared['prefix_frames']
                self.assertGreater(count, 0)
                normalized = self.root/'.recovery/normalized/base'
                self.assertEqual(self.identities(normalized/'video.mkv'), [(1, i) for i in range(count)])
                self.assertEqual(len(read_ticks(normalized/'frames.csv')), count)
                new = self.numbered(prepared['active_capture'], 20, 2)
                result = self.recovery.finalize(self.saved)
                self.assertEqual(result['frames'], count + 20)
                # Exactly old 0..count-1 then new 0..19: no lost, repeated or interleaved frame.
                self.assertEqual(self.identities(self.root/'video.mkv'),
                                 [(1, i) for i in range(count)] + [(2, i) for i in range(20)])
                self.assertEqual(self.wav('audio.wav'), b''.join(old[:count] + new))
                self.assertEqual(self.wav('audio_play.wav'), b''.join(old[:count] + new))
                with (self.root/'frames.csv').open(newline='', encoding='utf-8') as stream:
                    games = [int(row['game_frame']) for row in csv.DictReader(stream)]
                self.assertEqual(games, [1000 + i for i in range(count)] + [2000 + i for i in range(20)])
                report = next(r for r in result['recovery_history'] if r['source'] == 'base')
                self.assertTrue(report['video_ended_prematurely'])
                self.assertLessEqual(count, report['contiguous_video_frames'])

    def test_complete_bframe_capture_keeps_every_frame(self):
        self.numbered(self.root, 90, 1)
        prepared = self.recovery.prepare(self.saved)
        self.assertEqual(prepared['prefix_frames'], 90)
        self.numbered(prepared['active_capture'], 30, 2)
        self.recovery.finalize(self.saved)
        self.assertEqual(self.identities(self.root/'video.mkv'), [(1, i) for i in range(90)] + [(2, i) for i in range(30)])

    def test_clean_bframe_receipt_keeps_splice_exact_without_decode(self):
        old=self.numbered(self.root,90,1)
        self.recovery.record_closed_capture(self.saved,self.root,{'state':'finished','frames':90})
        with patch.object(self.recovery,'_probe',wraps=self.recovery._probe) as probe:
            prepared=self.recovery.prepare(self.saved)
        self.assertTrue(all(call.kwargs.get('decode') is False for call in probe.call_args_list))
        new=self.numbered(prepared['active_capture'],30,2)
        self.recovery.finalize(self.saved)
        self.assertEqual(self.identities(self.root/'video.mkv'),[(1,i) for i in range(90)]+[(2,i) for i in range(30)])
        self.assertEqual(self.wav('audio.wav'),b''.join(old+new))
        self.assertEqual(self.wav('audio_play.wav'),b''.join(old+new))

    def test_lost_ledger_cannot_fall_back_to_root_after_attempt_started(self):
        self.media(self.root, [100, 200])
        self.recovery.prepare(self.saved)
        (self.root/'.recovery/ledger.json').unlink()
        with self.assertRaises(RuntimeError):
            self.recovery.capture_directory(self.saved)


class VideoPrefixSelectionTests(unittest.TestCase):
    """Decode-order cut points on synthetic packet lists (no codec needed)."""
    def prefix(self, packets, frames=None, limit=10**9, tail=False):
        from orchestrator.recording_recovery import video_prefix
        return video_prefix(packets, packets if frames is None else frames, limit, tail_suspect=tail)[0]

    def test_cut_lands_on_mini_gop_boundaries(self):
        order = [0, 4, 2, 1, 3, 8, 6, 5, 7, 12, 10, 9, 11]
        self.assertEqual(self.prefix(order), 13)
        self.assertEqual(self.prefix(order[:6]), 5)              # P8 without its B-frames
        self.assertEqual(self.prefix(order[:3]), 1)              # I0 P4 B2: only frame 0 is complete
        self.assertEqual(self.prefix(order, limit=7), 5)         # CSV/PCM stop inside a mini-GOP
        self.assertEqual(self.prefix(order, limit=9), 9)

    def test_undecodable_or_partial_tail_is_never_used(self):
        order = [0, 4, 2, 1, 3, 8, 6, 5, 7]
        self.assertEqual(self.prefix(order, frames=[0, 1, 2, 3, 4, 6, 7, 8]), 5)   # frame 5 did not decode
        self.assertEqual(self.prefix(order, tail=True), 5)                          # last packet may be partial
        self.assertEqual(self.prefix([0, 1, 2], tail=True), 2)

    def test_duplicate_or_negative_timestamps_stop_the_prefix(self):
        self.assertEqual(self.prefix([0, 1, 1, 2]), 2)
        self.assertEqual(self.prefix([0, -1, 1]), 1)
        self.assertEqual(self.prefix([]), 0)


class FfprobeLocationTests(unittest.TestCase):
    def test_beside_then_path(self):
        from orchestrator.recording_recovery import ffprobe_path, RecoveryError
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = Path(folder)/'ffmpeg'
            ffmpeg.write_bytes(b'')
            with patch('orchestrator.recording_recovery.shutil.which', return_value='/opt/bin/ffprobe') as which:
                self.assertEqual(ffprobe_path(str(ffmpeg)), '/opt/bin/ffprobe')
                which.assert_called_with('ffprobe')
            with patch('orchestrator.recording_recovery.shutil.which', return_value=None):
                with self.assertRaises(RecoveryError):
                    ffprobe_path(str(ffmpeg))
            (Path(folder)/'ffprobe').write_bytes(b'')
            self.assertEqual(ffprobe_path(str(ffmpeg)), str(Path(folder)/'ffprobe'))
