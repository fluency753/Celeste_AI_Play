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
        cls.ffprobe = str(Path(cls.ffmpeg).with_name('ffprobe' + Path(cls.ffmpeg).suffix))
        if not Path(cls.ffprobe).is_file() and shutil.which(cls.ffprobe) is None:
            raise unittest.SkipTest('ffprobe is unavailable')

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

    def test_lost_ledger_cannot_fall_back_to_root_after_attempt_started(self):
        self.media(self.root, [100, 200])
        self.recovery.prepare(self.saved)
        (self.root/'.recovery/ledger.json').unlink()
        with self.assertRaises(RuntimeError):
            self.recovery.capture_directory(self.saved)
